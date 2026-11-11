"""Read submitted artifacts and enforce task contracts in a disposable copy.

Native feature preflight limits executable content; this is not an OS sandbox.
No model generation module or candidate source program is imported here.
"""
from pathlib import Path
from time import perf_counter
import csv
import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import FreeCAD as App
import Part

from .common import contained, environment, now, read_json, sha256, write_json
from .measurements import measure
from .drawing_validation import evaluate_drawing, DrawingUnsupported
from . import __version__

SUPPORTED_NATIVE_TYPES = {
    "Spreadsheet::Sheet", "Part::Box", "Part::Cylinder", "Part::Cone", "Part::Sphere",
    "Part::Cut", "Part::Fuse", "Part::Common", "Part::MultiFuse", "Part::Compound",
    "Part::Feature", "Part::Fillet", "Part::Chamfer", "PartDesign::Body", "PartDesign::Pad",
    "PartDesign::Pocket", "Sketcher::SketchObject", "App::DocumentObjectGroup",
    "App::Origin", "App::Line", "App::Plane", "App::Point",
}


class UnsupportedArtifact(Exception):
    pass


def preflight(path):
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("Document.xml")
            if info.file_size > 25_000_000 or sum(i.file_size for i in archive.infolist()) > 250_000_000:
                raise UnsupportedArtifact("Native archive exceeds the declared preflight size limit")
            data = archive.read(info)
            if b"<!ENTITY" in data or b"<!DOCTYPE" in data:
                raise UnsupportedArtifact("XML entity or document declarations are outside native support")
            xml = ET.fromstring(data)
            objects = [(o.attrib.get("name"), o.attrib.get("type")) for o in xml.findall(".//Objects/Object")]
            unknown = [(name, kind) for name, kind in objects if kind not in SUPPORTED_NATIVE_TYPES]
            if unknown:
                raise UnsupportedArtifact(f"Native feature types not approved for recompute: {unknown}")
            if any("XLink" in p.attrib.get("type", "") or "Python" in p.attrib.get("type", "")
                   for p in xml.iter("Property")):
                raise UnsupportedArtifact("Python proxy or external cross-document link property")
            return {"native_types": sorted({kind for _, kind in objects}), "object_count": len(objects)}
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as error:
        raise ValueError("Invalid native CAD archive: " + str(error)) from error


def terminal_solids(doc):
    candidates = [o for o in doc.Objects if hasattr(o, "Shape") and not o.Shape.isNull() and o.Shape.Volume > 1e-9]
    # InList includes scalar ExpressionEngine dependencies. A jaw may read a
    # base's Length property without consuming the physical base. Only native
    # link properties establish feature-history consumption.
    consumed = set()
    def links(value):
        if hasattr(value, "Document") and hasattr(value, "Name"):
            yield value
        elif isinstance(value, (tuple, list)):
            for item in value:
                yield from links(item)
    for obj in candidates:
        for prop in obj.PropertiesList:
            if obj.getTypeIdOfProperty(prop).startswith("App::PropertyLink"):
                for target in links(getattr(obj, prop)):
                    # Body features contain a native ownership backlink to
                    # their containing Body. It is not a geometric consumer.
                    owned = target.TypeId == "PartDesign::Body" and obj in target.Group
                    if target.Name != obj.Name and not owned:
                        consumed.add(target.Name)
    return [obj for obj in candidates if obj.Name not in consumed]


def resolve(doc, manifest, task):
    mapping = manifest.get("roles", {})
    missing = [r for r in task["required_roles"] if r not in mapping]
    if missing:
        raise ValueError("Missing required physical roles: " + repr(missing))
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Distinct physical roles may not alias the same native object")
    roles = {}
    for role, name in mapping.items():
        obj = doc.getObject(name)
        if obj is None or not hasattr(obj, "Shape") or obj.Shape.isNull():
            raise ValueError(f"Role {role} has no nonempty candidate Shape")
        roles[role] = obj
    parts = terminal_solids(doc)
    final_names = {o.Name for o in parts}
    mapped_names = {o.Name for o in roles.values()}
    if final_names != mapped_names:
        raise ValueError(f"Final solid completeness mismatch: unmapped={sorted(final_names-mapped_names)}, nonfinal={sorted(mapped_names-final_names)}")
    return roles, parts


def recompute(doc):
    for obj in doc.Objects:
        if hasattr(obj, "touch"):
            obj.touch()
    doc.recompute(None, True, True)
    failures = []
    for obj in doc.Objects:
        state = list(obj.State)
        if any(s in {"Invalid", "Error"} for s in state):
            failures.append({"object": obj.Name, "state": state, "message": obj.getStatusString()})
    if failures:
        raise ValueError("Native recompute failed: " + json.dumps(failures))


def set_driver(doc, spec, value):
    obj = doc.getObject(spec["object"])
    if obj is None:
        raise ValueError("Missing driver object")
    if spec["kind"] == "spreadsheet":
        if obj.TypeId != "Spreadsheet::Sheet" or not re.fullmatch(r"[A-Z]+[1-9][0-9]*", spec["cell"]):
            raise ValueError("Invalid native spreadsheet driver mapping")
        obj.set(spec["cell"], f"{value:g} mm")
    elif spec["kind"] == "property":
        prop = spec["property"]
        if prop not in obj.PropertiesList or obj.getTypeIdOfProperty(prop) not in {"App::PropertyLength", "App::PropertyDistance"}:
            raise UnsupportedArtifact("Only direct native length/distance properties can be drivers")
        setattr(obj, prop, f"{value:g} mm")
    else:
        raise UnsupportedArtifact("Unknown parameter driver representation")


def evaluate(task, submission_path, report_path, run_context=None):
    start = perf_counter()
    path = Path(submission_path).resolve()
    checks = []
    artifacts = {}
    # Trusted caller metadata only. Candidate manifests cannot self-declare
    # that developer mutations are independent model or formal benchmark runs.
    context = dict(run_context or {"origin": "development_sample", "phase": "development"})
    if context.get("origin") not in {"independent_agent_run", "controller_mutation", "development_sample", "formal_test"}:
        raise ValueError("Unknown trusted run origin")
    if context["origin"] in {"independent_agent_run", "formal_test"} and not context.get("run_id"):
        raise ValueError("Agent/formal provenance requires an external run_id")

    def append(rule_id, status, observation, criterion=None, elapsed=0.0, critical=True):
        checks.append({"rule_id": rule_id, "status": status, "critical": critical,
            "observation": observation, "criterion": criterion or {}, "elapsed_s": elapsed,
            "evidence": {"submission": str(path), "rule_version": task["version"]}})

    def check_rule(rule, doc, manifest, roles, parts, pristine, scratch):
        before = perf_counter()
        try:
            if rule["kind"] != "parameter_probe":
                passed, observation, criterion = measure(rule, roles, parts)
                return "PASS" if passed else "FAIL", observation, criterion
            driver = manifest.get("drivers", {}).get(rule["driver"])
            if not driver:
                return "FAIL", {"missing_driver": rule["driver"]}, {}
            cases = []
            for index, case in enumerate(rule["cases"]):
                child = None
                case_path = scratch / (rule["driver"] + str(index) + ".FCStd")
                shutil.copy2(pristine, case_path)
                try:
                    child = App.openDocument(str(case_path))
                    set_driver(child, driver, case["value_mm"])
                    recompute(child)
                    child.save()
                    App.closeDocument(child.Name)
                    child = App.openDocument(str(case_path))
                    recompute(child)
                    cr, cp = resolve(child, manifest, task)
                    values = []
                    for invariant in rule.get("invariant_roles", []):
                        reference_shape = roles[invariant].Shape
                        current_shape = cr[invariant].Shape
                        difference = float(reference_shape.cut(current_shape).Volume + current_shape.cut(reference_shape).Volume)
                        values.append({"rule_id": "invariant."+invariant, "status": "PASS" if difference <= 1e-5 else "FAIL", "observation": {"symmetric_difference_mm3": difference}, "criterion": {"maximum_mm3": 1e-5}})
                    for assertion in case["assertions"]:
                        ok, observation, criterion = measure(assertion, cr, cp)
                        values.append({"rule_id": assertion["rule_id"], "status": "PASS" if ok else "FAIL", "observation": observation, "criterion": criterion})
                    cases.append({"value_mm": case["value_mm"], "status": "PASS" if all(v["status"] == "PASS" for v in values) else "FAIL", "saved_and_reopened": True, "checks": values})
                finally:
                    if child:
                        App.closeDocument(child.Name)
            return ("PASS" if all(c["status"] == "PASS" for c in cases) else "FAIL", {"driver": driver, "cases": cases}, {"must_recompute_and_persist": True})
        except UnsupportedArtifact as error:
            return "UNSUPPORTED", {"reason": str(error)}, {}
        except NotImplementedError as error:
            return "UNSUPPORTED", {"reason": str(error)}, {}
        except (ValueError, KeyError) as error:
            return "FAIL", {"reason": str(error)}, {}
        finally:
            check_rule.last_elapsed = perf_counter()-before

    def finish():
        statuses = [c["status"] for c in checks if c["critical"]]
        overall = "ERROR" if "ERROR" in statuses else "FAIL" if "FAIL" in statuses else "UNSUPPORTED" if "UNSUPPORTED" in statuses else "PASS" if statuses else "ERROR"
        result = {"schema_version": 2, "scorer_version": __version__, "created_utc": now(), "task_id": task["task_id"], "task_version": task["version"],
            "grading_visibility": "private",
            "task_sha256": sha256(Path(task["_path"])), "overall": overall,
            "strict_success": bool(statuses and all(s == "PASS" for s in statuses)), "checks": checks,
            "artifacts_sha256": artifacts, "environment": environment(), "elapsed_s": perf_counter()-start,
            "motion_coverage": task.get("motion_coverage"), "original_files_modified": False,
            "process_boundary": context.get("process_boundary", "Native worker; caller has not supplied an OS sandbox attestation"),
            "provenance": context,
            "agent_trial": context["origin"] in {"independent_agent_run", "formal_test"},
            "formal_benchmark_episode": context["origin"] == "formal_test"}
        write_json(report_path, result)
        return result

    if not path.is_file():
        append("input.submission_exists", "FAIL", {"missing": str(path)})
        return finish()
    try:
        manifest = read_json(path)
        if manifest.get("task_id") != task["task_id"]:
            raise ValueError("Submission task_id does not match contract")
        native = contained(path.parent, manifest["native"])
        if not native.is_file():
            raise ValueError("Required native CAD file is missing")
        for key in ["native", "step", "bom", "drawing"]:
            if key in manifest:
                p = contained(path.parent, manifest[key])
                if p.is_file():
                    artifacts[key] = sha256(p)
        native_preflight = preflight(native)
    except UnsupportedArtifact as error:
        append("input.native_supported", "UNSUPPORTED", {"reason": str(error)})
        return finish()
    except (ValueError, KeyError, OSError) as error:
        append("input.artifact_contract", "FAIL", {"reason": str(error)})
        return finish()
    append("input.native_supported", "PASS", native_preflight)
    with tempfile.TemporaryDirectory(prefix="mechcad_eval_") as temp:
        scratch = Path(temp)
        candidate = scratch / "candidate.FCStd"
        shutil.copy2(native, candidate)
        doc = None
        try:
            doc = App.openDocument(str(candidate))
            recompute(doc)
            roles, parts = resolve(doc, manifest, task)
            invalid = [{"object": o.Name, "valid": bool(o.Shape.isValid()), "solid_count": len(o.Shape.Solids), "volume_mm3": float(o.Shape.Volume)} for o in parts]
            append("geometry.valid_solids", "PASS" if all(r["valid"] and r["solid_count"] > 0 and r["volume_mm3"] > 0 for r in invalid) else "FAIL", {"parts": invalid})
        except (ValueError, OSError) as error:
            append("geometry.load_recompute_roles", "FAIL", {"reason": str(error)})
            if doc:
                App.closeDocument(doc.Name)
            return finish()
        try:
            for rule in task["rules"]:
                try:
                    status, observation, criterion = check_rule(rule, doc, manifest, roles, parts, native, scratch)
                    append(rule["rule_id"], status, observation, criterion, check_rule.last_elapsed, rule.get("critical", True))
                except Exception as error:
                    append(rule["rule_id"], "ERROR", {"exception": type(error).__name__, "message": str(error)})
            # File-existence failures are candidate FAIL, not evaluator ERROR.
            for key in ["step", "bom", "drawing"]:
                artifact = contained(path.parent, manifest.get(key, "__missing__"))
                if not artifact.is_file():
                    append("delivery."+key, "FAIL", {"missing": str(artifact)})
                    continue
                try:
                    if key == "step":
                        step = Part.read(str(artifact))
                        expected = Part.makeCompound([p.Shape for p in parts])
                        symmetric_difference = float(expected.cut(step).Volume + step.cut(expected).Volume)
                        ok = step.isValid() and len(step.Solids) > 0 and symmetric_difference <= 1e-4
                        append("delivery.step", "PASS" if ok else "FAIL", {"valid": bool(step.isValid()), "solids": len(step.Solids), "symmetric_difference_mm3": symmetric_difference}, {"max_symmetric_difference_mm3": 1e-4, "position_sensitive": True})
                    elif key == "bom":
                        with artifact.open(encoding="utf-8-sig", newline="") as handle:
                            rows = list(csv.DictReader(handle))
                        listed = {row["role"]: row for row in rows}
                        ok = len(rows) == len(roles) and set(listed) == set(roles)
                        for role, obj in roles.items():
                            row = listed.get(role, {})
                            ok = ok and row.get("object") == obj.Name and float(row.get("quantity", 0)) == 1 and abs(float(row.get("volume_mm3", -1))-obj.Shape.Volume) < 1e-4
                        append("delivery.bom", "PASS" if ok else "FAIL", {"rows": rows}, {"mapping_and_quantity_match_native": True, "volume_tolerance_mm3": 1e-4})
                    else:
                        ok, observation, criterion = evaluate_drawing(artifact, parts)
                        append("delivery.drawing", "PASS" if ok else "FAIL", observation, criterion)
                except DrawingUnsupported as error:
                    append("delivery."+key, "UNSUPPORTED", {"reason": str(error)})
                except (ValueError, KeyError, OSError, ET.ParseError, Part.OCCError) as error:
                    append("delivery."+key, "FAIL", {"reason": str(error)})
        finally:
            App.closeDocument(doc.Name)
    # Re-hash originals after all perturbation probes.
    unchanged = all(sha256(contained(path.parent, manifest[k])) == digest for k, digest in artifacts.items())
    append("audit.originals_unchanged", "PASS" if unchanged else "ERROR", {"unchanged": unchanged})
    return finish()
