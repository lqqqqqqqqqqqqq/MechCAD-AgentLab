"""Evidence-bounded feedback, candidate labels, and tabular exports."""
from pathlib import Path
import csv
import json

from .common import now, read_json, sha256, write_json


def category(rule_id):
    for prefix, label in [("input.", "FILE_OR_SUPPORT"), ("geometry.", "GEOMETRY_OR_RECOMPUTE"),
        ("dimension.", "DIMENSION"), ("surface.", "FEATURE_LOCATION_OR_FIT"), ("fit.", "CLEARANCE"),
        ("contact.", "CONTACT"), ("function.", "FUNCTION"), ("assembly.", "INTERFERENCE"),
        ("parametric.", "PARAMETRIC_REBUILD"), ("delivery.", "DELIVERY"), ("worker.", "EXECUTION"), ("audit.", "AUDIT")]:
        if rule_id.startswith(prefix):
            return label
    return "MISSING_MATERIAL" if rule_id.startswith("material.") else "BLOCKED_REQUIRED_VOID" if rule_id.startswith("void.") else "UNCLASSIFIED"


def feedback(report):
    """Development/public checker output only; never feed hidden final reports."""
    provenance=report.get("provenance",{})
    phase=provenance.get("phase", "")
    # Explicit public checker outputs have their own small disclosure contract.
    # Unknown/new assessment phases must fail closed; a substring check silently
    # allowed P4 runtime and P5 readiness private reports to become feedback.
    public_phase=phase in {"development","public_preflight","public_development_checker"}
    if (report.get("formal_benchmark_episode") or report.get("grading_visibility")=="private"
            or provenance.get("grading_visibility")=="private" or not public_phase):
        raise ValueError("Private/frozen grading is not public repair feedback")
    failures = []
    for item in report["checks"]:
        if item["status"] == "PASS":
            continue
        failures.append({"rule_id": item["rule_id"], "status": item["status"], "category": category(item["rule_id"]),
            "severity": "critical" if item.get("critical", True) else "advisory",
            "observation": item["observation"], "criterion": item.get("criterion", {}),
            "evidence": item.get("evidence", {})})
    return {"schema_version": 1, "task_id": report["task_id"], "overall": report["overall"],
        "origin": "public_development_checker", "failures": failures,
        "diagnosis_hypotheses": [], "contains_human_verified_root_cause": False,
        "instruction": "Use only this evidence and the public contract to revise a new artifact. Preserve the original submission."}


def export_reports(paths, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for path in paths:
        report = read_json(path)
        submission_path=next((c.get("evidence",{}).get("submission") for c in report["checks"] if c.get("evidence",{}).get("submission")),None)
        submission=read_json(submission_path) if submission_path and Path(submission_path).is_file() else None
        root=Path(__file__).resolve().parents[2]
        candidates=[root/"tasks"/report["task_id"]/"task.json"]
        candidates+=list(root.glob("experiments/*/frozen/**/tasks/"+report["task_id"]+"/task.json"))
        task_file=next((p for p in candidates if p.is_file() and sha256(p)==report.get("task_sha256")),None)
        contract=read_json(task_file) if task_file else None
        provenance=report.get("provenance",{"origin":"legacy_unspecified","phase":"legacy","source_metadata_unresolved":True})
        run_file=Path(path).parent/"run.json"
        run=read_json(run_file) if run_file.is_file() else None
        records.append({"schema_version": 2, "record_kind": provenance["origin"]+"_evaluation", "task_id": report["task_id"],
            "report_path": str(Path(path).resolve()), "report_sha256": sha256(path), "overall": report["overall"],
            "task_prompt":contract.get("prompt") if contract else None,"task_contract_path":str(task_file) if task_file else None,
            "task_contract_sha256":report.get("task_sha256"),"contract_evidence_status":"hash_matched" if contract else "missing_matching_contract",
            "submission_path":submission_path,"submission":submission,"repair_run":run,
            "trajectory_path":str(Path(path).parent/"trajectory.jsonl") if run else None,
            "trace_evidence_status":("immutable_before_snapshot_present" if (Path(path).parent/"evaluation_before.json").is_file() else "legacy_missing_immutable_before_snapshot") if run else None,
            "strict_success": report["strict_success"], "observations": [{"rule_id": c["rule_id"], "measurement": c["observation"]} for c in report["checks"]],
            "judgments": [{"rule_id": c["rule_id"], "status": c["status"], "category": category(c["rule_id"]), "criterion": c.get("criterion", {})} for c in report["checks"]],
            "diagnosis_hypotheses": [], "agent_trial":report.get("agent_trial"),"provenance":provenance,
            "new_agent_execution":provenance.get("new_agent_execution",False),
            "grading_visibility":report.get("grading_visibility","private" if provenance.get("phase") not in {"development","public_preflight","public_development_checker"} else "legacy_development"),
            "runtime_execution":report.get("runtime_execution"),
            "formal_benchmark_episode":report.get("formal_benchmark_episode",False),"human_review": "pending",
            "environment": report.get("environment", {}), "artifacts_sha256": report.get("artifacts_sha256", {})})
    with (out/"records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+"\n")
    with (out/"checks.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["task_id", "report_path", "rule_id", "status", "category"])
        writer.writeheader()
        for record in records:
            for check in record["judgments"]:
                writer.writerow({"task_id": record["task_id"], "report_path": record["report_path"], **{k:check[k] for k in ["rule_id", "status", "category"]}})
    write_json(out/"manifest.json", {"created_utc": now(), "records": len(records),"repair_results":sum(r["repair_run"] is not None for r in records),
        "record_kind":"provenance-separated evaluations","agent_artifact_score_rows":sum(r["agent_trial"] is True for r in records),
        "new_agent_executions":sum(r["new_agent_execution"] is True for r in records),"training_truth_labels":0})
    return {"records": len(records), "out": str(out)}

