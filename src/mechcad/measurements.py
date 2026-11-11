"""Deterministic measurements of candidate BRep; no generator dependency."""
from itertools import combinations
import math
import FreeCAD as App
import Part


def bounding(shape, metric):
    b = shape.BoundBox
    table = {"x_length": b.XLength, "y_length": b.YLength, "z_length": b.ZLength,
             "x_min": b.XMin, "x_max": b.XMax, "y_min": b.YMin, "y_max": b.YMax,
             "z_min": b.ZMin, "z_max": b.ZMax, "center_x": (b.XMin+b.XMax)/2,
             "center_y": (b.YMin+b.YMax)/2, "center_z": (b.ZMin+b.ZMax)/2}
    if metric not in table:
        raise NotImplementedError("Unknown bbox metric: " + metric)
    return float(table[metric])


def cylinder_surfaces(shape):
    result = []
    for index, face in enumerate(shape.Faces, 1):
        surface = face.Surface
        if not isinstance(surface, Part.Cylinder):
            continue
        u0, u1, v0, v1 = face.ParameterRange
        u, v = (u0+u1)/2, (v0+v1)/2
        point = face.valueAt(u, v)
        axis = surface.Axis
        relative = point - surface.Center
        radial = relative - axis * relative.dot(axis)
        dot = face.normalAt(u, v).dot(radial)
        result.append({"face": f"Face{index}", "radius_mm": float(surface.Radius),
            "center": list(surface.Center), "axis": list(axis), "concave": bool(dot < 0),
            "area_mm2": float(face.Area)})
    return result


def planar_contact_area(a, b, epsilon=1e-6):
    area = 0.0
    evidence = []
    for ai, af in enumerate(a.Faces, 1):
        if not isinstance(af.Surface, Part.Plane):
            continue
        an = af.normalAt(*[(r[0]+r[1])/2 for r in [af.ParameterRange[:2], af.ParameterRange[2:]]])
        for bi, bf in enumerate(b.Faces, 1):
            if not isinstance(bf.Surface, Part.Plane):
                continue
            bn = bf.normalAt(*[(r[0]+r[1])/2 for r in [bf.ParameterRange[:2], bf.ParameterRange[2:]]])
            if abs(an.dot(bn)+1) > epsilon:
                continue
            if abs((af.CenterOfMass-bf.CenterOfMass).dot(an)) > epsilon:
                continue
            common = af.common(bf)
            if common.Area > epsilon:
                area += common.Area
                evidence.append({"a_face": f"Face{ai}", "b_face": f"Face{bi}", "area_mm2": float(common.Area)})
    return float(area), evidence


def measure(rule, roles, all_parts):
    """Return (passed, observation, criterion). Unsupported kinds raise."""
    kind = rule["kind"]
    shape = lambda role: roles[role].Shape
    if kind == "task_interfaces":
        from .engineering_checks import inspect_interfaces
        return inspect_interfaces(rule, roles, all_parts)
    if kind == "occupied_box":
        lo, hi = rule["minimum"], rule["maximum"]
        inset = rule["boundary_tolerance_mm"]
        # The mandatory core accounts for the disclosed external length
        # tolerance. No candidate volume or topology defines the required core.
        core = Part.makeBox(*(hi[i]-lo[i]-2*inset for i in range(3)), App.Vector(*(v+inset for v in lo)))
        missing = core.cut(shape(rule["role"]))
        if not missing.isNull() and not missing.isValid():
            raise ValueError("Invalid occupied-region Boolean result")
        volume = sum(abs(s.Volume) for s in missing.Solids)
        witnesses = [{"volume_mm3": s.Volume, "center_mm": list(s.CenterOfMass),
                      "bounds_mm": [s.BoundBox.XMin,s.BoundBox.XMax,s.BoundBox.YMin,s.BoundBox.YMax,s.BoundBox.ZMin,s.BoundBox.ZMax]}
                     for s in missing.Solids]
        cavities = []
        for solid in shape(rule["role"]).Solids:
            shell_volumes = sorted((abs(Part.makeSolid(shell).Volume) for shell in solid.Shells), reverse=True)
            cavities.extend(shell_volumes[1:])
        actual_box_missing = 0.0
        if rule.get("require_full_candidate_box"):
            b = shape(rule["role"]).BoundBox
            actual_box = Part.makeBox(b.XLength,b.YLength,b.ZLength,App.Vector(b.XMin,b.YMin,b.ZMin))
            remainder = actual_box.cut(shape(rule["role"]))
            actual_box_missing = sum(abs(s.Volume) for s in remainder.Solids)
        # External tolerance does not excuse a sealed internal cavity, including
        # one entirely inside the external tolerance band.
        return max(volume,actual_box_missing) <= rule["volume_tolerance_mm3"] and all(v <= rule["volume_tolerance_mm3"] for v in cavities), {"missing_core_mm3": volume, "missing_regions": witnesses,
            "missing_within_actual_cuboid_mm3": actual_box_missing,
            "closed_internal_cavity_mm3": cavities,
            "actual_volume_mm3": shape(rule["role"]).Volume}, {"minimum": lo, "maximum": hi,
            "boundary_tolerance_mm": inset, "maximum_missing_core_mm3": rule["volume_tolerance_mm3"],
            "require_full_candidate_box":rule.get("require_full_candidate_box",False),
            "method": "Analytic contract region minus actual BRep; no reference-model dependency"}
    if kind == "through_void":
        solid = shape(rule["role"])
        axis = App.Vector(*rule["axis"]); axis.normalize()
        origin = App.Vector(*rule["center"])
        candidates = []
        # Locate the actual allowed bore axis and radius. This avoids rejecting
        # a legal radius/position tolerance stack or an enlarged end chamfer.
        for f in cylinder_surfaces(solid):
            fa = App.Vector(*f["axis"])
            delta = App.Vector(*f["center"])-origin
            offset = (delta-axis*delta.dot(axis)).Length
            if (f["concave"] and abs(abs(fa.dot(axis))-1) <= 1e-6
                    and offset <= rule["tolerance_mm"]+1e-9
                    and abs(f["radius_mm"]-rule["radius_mm"]) <= rule.get("radius_tolerance_mm",rule["tolerance_mm"])+1e-9):
                center = App.Vector(*f["center"])
                box = solid.BoundBox
                spans = [(App.Vector(x,y,z)-center).dot(axis)
                         for x in (box.XMin,box.XMax) for y in (box.YMin,box.YMax) for z in (box.ZMin,box.ZMax)]
                margin = rule["endpoint_margin_mm"]
                radius = f["radius_mm"]-rule["radial_epsilon_mm"]
                probe = Part.makeCylinder(radius, max(spans)-min(spans)+2*margin,
                                          center+axis*(min(spans)-margin), axis)
                blocked = solid.common(probe)
                if not blocked.isNull() and not blocked.isValid():
                    raise ValueError("Invalid full-depth bore Boolean result")
                candidates.append({"face": f["face"], "measured_radius_mm": f["radius_mm"],
                                   "measured_axis_origin": list(center), "axis_offset_mm": offset,
                                   "probe_radius_mm": radius, "probe_length_mm": max(spans)-min(spans)+2*margin,
                                   "blocked_mm3": sum(abs(s.Volume) for s in blocked.Solids)})
        ok = any(c["blocked_mm3"] <= rule["volume_tolerance_mm3"] for c in candidates)
        return ok, {"candidate_bores": candidates}, {"axis": rule["axis"], "center": rule["center"],
            "radius_mm": rule["radius_mm"], "tolerance_mm": rule["tolerance_mm"],
            "radius_tolerance_mm":rule.get("radius_tolerance_mm",rule["tolerance_mm"]),
            "maximum_blocked_mm3": rule["volume_tolerance_mm3"], "radial_epsilon_mm": rule["radial_epsilon_mm"],
            "scope": "Actual allowed cylindrical bore swept beyond both full part bounds; other parts are not subtracted"}
    if kind == "bbox":
        value = bounding(shape(rule["role"]), rule["metric"])
        target, tolerance = rule["expected"], rule["tolerance_mm"]
        return abs(value-target) <= tolerance, {"value": value, "unit": "mm", "role": rule["role"], "metric": rule["metric"]}, {"expected": target, "tolerance_mm": tolerance}
    if kind == "opening":
        axis = rule["axis"]
        left = functional_face(shape(rule["left"]), rule, 1) if "contact_window" in rule else bounding(shape(rule["left"]), axis+"_max")
        right = functional_face(shape(rule["right"]), rule, -1) if "contact_window" in rule else bounding(shape(rule["right"]), axis+"_min")
        value = right-left
        return abs(value-rule["expected"]) <= rule["tolerance_mm"], {"value": value, "unit": "mm", "left_face_coordinate": left, "right_face_coordinate": right}, {"expected": rule["expected"], "tolerance_mm": rule["tolerance_mm"]}
    if kind == "pair_center":
        axis = rule["axis"]
        left = functional_face(shape(rule["left"]), rule, 1) if "contact_window" in rule else bounding(shape(rule["left"]), axis+"_max")
        right = functional_face(shape(rule["right"]), rule, -1) if "contact_window" in rule else bounding(shape(rule["right"]), axis+"_min")
        value = (left+right)/2
        return abs(value-rule["expected"]) <= rule["tolerance_mm"], {"value": value, "unit": "mm"}, {"expected": rule["expected"], "tolerance_mm": rule["tolerance_mm"]}
    if kind == "distance":
        value = float(shape(rule["a"]).distToShape(shape(rule["b"]))[0])
        return abs(value-rule["expected"]) <= rule["tolerance_mm"], {"value": value, "unit": "mm", "parts": [rule["a"], rule["b"]]}, {"expected": rule["expected"], "tolerance_mm": rule["tolerance_mm"]}
    if kind == "cylinder_surface":
        axis = App.Vector(*rule["axis"])
        axis.normalize()
        center = App.Vector(*rule["center"])
        tolerance = rule["tolerance_mm"]
        axis_tolerance = rule.get("axis_tolerance_mm",tolerance)
        radius_tolerance = rule.get("radius_tolerance_mm",tolerance)
        surfaces = cylinder_surfaces(shape(rule["role"]))
        matches = []
        for face in surfaces:
            fa = App.Vector(*face["axis"])
            delta = App.Vector(*face["center"])-center
            offset = (delta-axis*delta.dot(axis)).Length
            if abs(abs(fa.dot(axis))-1) < 1e-6 and offset <= axis_tolerance+1e-9 and abs(face["radius_mm"]-rule["radius_mm"]) <= radius_tolerance+1e-9:
                if rule.get("concave") is None or face["concave"] == rule["concave"]:
                    matches.append({**face, "axis_offset_mm": float(offset)})
        return bool(matches), {"matches": matches, "candidate_cylinders": surfaces, "unit": "mm"}, {**{k: rule[k] for k in ["axis", "center", "radius_mm", "concave", "tolerance_mm"]},"axis_tolerance_mm":axis_tolerance,"radius_tolerance_mm":radius_tolerance,"comparison_epsilon_mm":1e-9}
    if kind == "non_interference":
        excluded = {roles[r].Name for r in rule.get("exclude", [])}
        pairs = []
        for a, b in combinations([p for p in all_parts if p.Name not in excluded], 2):
            volume = float(a.Shape.common(b.Shape).Volume)
            pairs.append({"a": a.Name, "b": b.Name, "intersection_mm3": volume})
        tolerance = rule["volume_tolerance_mm3"]
        return all(p["intersection_mm3"] <= tolerance for p in pairs), {"pairs": pairs, "excluded_roles": rule.get("exclude", []), "unit": "mm3"}, {"maximum_intersection_mm3": tolerance, "contact_without_volume_is_allowed": True}
    if kind == "contact":
        a, b = shape(rule["a"]), shape(rule["b"])
        distance, volume = float(a.distToShape(b)[0]), float(a.common(b).Volume)
        area, faces = planar_contact_area(a, b)
        ok = distance <= rule["distance_tolerance_mm"] and volume <= rule["volume_tolerance_mm3"] and area+1e-5 >= rule["minimum_area_mm2"]
        return ok, {"distance_mm": distance, "intersection_mm3": volume, "contact_area_mm2": area, "face_evidence": faces}, {k: rule[k] for k in ["distance_tolerance_mm", "volume_tolerance_mm3", "minimum_area_mm2"]}
    raise NotImplementedError("Unsupported measurement kind: " + kind)


def functional_face(shape, rule, direction):
    """Locate the actual planar jaw interface inside the contract YZ window.

    Aggregate coplanar split faces; nonfunctional protrusions outside this
    region must not determine the opening. No feature names are inspected.
    """
    if rule["axis"] != "x":
        raise NotImplementedError("Functional contact window currently supports X translation")
    y0,y1,z0,z1 = rule["contact_window"]
    groups = {}
    for face in shape.Faces:
        if not isinstance(face.Surface, Part.Plane):
            continue
        u0,u1,v0,v1=face.ParameterRange
        normal=face.normalAt((u0+u1)/2,(v0+v1)/2)
        if normal.x*direction < 1-1e-6:
            continue
        x=face.CenterOfMass.x
        patch=Part.Face(Part.makePolygon([App.Vector(x,y,z) for y,z in ((y0,z0),(y1,z0),(y1,z1),(y0,z1),(y0,z0))]))
        key=round(x,7)
        groups[key]=groups.get(key,0.0)+face.common(patch).Area
    eligible=[x for x,area in groups.items() if area+1e-5 >= rule.get("minimum_interface_area_mm2",1600)]
    if not eligible:
        raise ValueError("No complete planar functional contact interface in the contract window: "+repr(groups))
    return max(eligible) if direction>0 else min(eligible)

