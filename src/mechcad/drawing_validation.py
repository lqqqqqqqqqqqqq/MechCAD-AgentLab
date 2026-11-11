"""Deterministic nominal SVG-to-BRep orthographic contour association.

Supported: static vector linework in three identifiable g views, affine SVG
transforms, standard path/line/arc/curve primitives. No raster, use, CSS-driven
geometry, clip/mask/filter, animation or hidden linework. Compare unordered
visible contours, not path strings or the reference generator's feature tree.
"""
from copy import deepcopy
import math
import os
import re
import xml.etree.ElementTree as ET
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import FreeCAD as App
import Part
import TechDraw
import numpy as np
from scipy.ndimage import distance_transform_edt
from PySide import QtCore, QtGui, QtSvg

PIXELS_PER_MM = 8.0
CONTOUR_TOLERANCE_MM = 0.25
_application = None


class DrawingUnsupported(Exception):
    pass


def local(node):
    return node.tag.rsplit("}", 1)[-1]


def render_document(group):
    root = ET.Element("svg", xmlns="http://www.w3.org/2000/svg")
    outer = ET.SubElement(root, "g", id="mechcad_measured_view")
    outer.append(deepcopy(group))
    for n in outer.iter():
        n.attrib.pop("id", None)
        n.attrib.pop("class", None)
        # Only supported visible linework reaches this function. Stroke styles
        # are normalized so colour and authored pen width cannot game matching.
        for k in ("style", "fill", "stroke", "stroke-width"):
            n.attrib.pop(k, None)
        n.set("fill", "none"); n.set("stroke", "black"); n.set("stroke-width", "0.0001")
    outer.set("id", "mechcad_measured_view")
    return root


def renderer(root):
    r = QtSvg.QSvgRenderer(QtCore.QByteArray(ET.tostring(root, encoding="utf-8")))
    if not r.isValid():
        raise DrawingUnsupported("Qt cannot reliably parse the supported vector view")
    return r


def bounds(root):
    b = renderer(root).boundsOnElement("mechcad_measured_view")
    if not all(math.isfinite(v) for v in (b.x(), b.y(), b.width(), b.height())) or min(b.width(), b.height()) <= 0:
        raise ValueError("Empty or nonfinite view contour")
    return b


def transform_matrix(text):
    matrix = np.eye(3)
    remainder = re.sub(r"([a-zA-Z]+)\s*\(([^)]*)\)", "", text)
    if remainder.strip(" ,\t\r\n"):
        raise DrawingUnsupported("Unparsed affine transform")
    for name, args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", text):
        v = [float(x) for x in re.split(r"[ ,]+", args.strip()) if x]
        if not all(math.isfinite(x) for x in v): raise ValueError("Nonfinite transform")
        m = np.eye(3)
        if name=="translate" and len(v) in (1,2):
            m[:2,2] = [v[0],v[1] if len(v)==2 else 0]
        elif name=="scale" and len(v) in (1,2):
            m[0,0],m[1,1] = v[0],v[-1]
        elif name=="rotate" and len(v) in (1,3):
            angle = math.radians(v[0]); c,s = math.cos(angle),math.sin(angle)
            m[:2,:2] = [[c,-s],[s,c]]
            if len(v)==3:
                pivot = np.array(v[1:]); m[:2,2] = pivot-m[:2,:2]@pivot
        elif name=="matrix" and len(v)==6:
            m = np.array([[v[0],v[2],v[4]],[v[1],v[3],v[5]],[0,0,1]])
        else: raise DrawingUnsupported("Unsupported affine transform: "+name)
        matrix = matrix@m
    return matrix


def mask(root, rect, model_scale, width, height):
    # Scale uniformly; never independently stretch width and height to fit.
    root = deepcopy(root)
    units_per_pixel = model_scale / PIXELS_PER_MM
    root.set("viewBox", f"{rect.center().x()-width*units_per_pixel/2} {rect.center().y()-height*units_per_pixel/2} {width*units_per_pixel} {height*units_per_pixel}")
    root.set("width", str(width)); root.set("height", str(height))
    def pen_width(n, parent):
        matrix = parent@transform_matrix(n.get("transform", ""))
        stretch = np.linalg.svd(matrix[:2,:2], compute_uv=False)
        if stretch[-1]<=1e-12 or stretch[0]/stretch[-1]>100:
            raise DrawingUnsupported("Degenerate/extreme transform cannot be raster-associated reliably")
        n.set("stroke-width", str(1.25*units_per_pixel/stretch[0]))
        for child in n: pen_width(child, matrix)
    pen_width(root, np.eye(3))
    img = QtGui.QImage(width, height, QtGui.QImage.Format_ARGB32)
    img.fill(QtGui.QColor(0,0,0,0))
    painter = QtGui.QPainter(img)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
    renderer(root).render(painter)
    painter.end()
    return np.frombuffer(img.constBits(), dtype=np.uint8).reshape(height, img.bytesPerLine())[:, :width*4].reshape(height,width,4)[:,:,3].copy() > 0


def validate_linework(root, selected):
    forbidden = {"image","use","foreignObject","script","clipPath","mask","filter","animate","animateTransform","set","symbol"}
    if any(local(n) in forbidden for n in root.iter()):
        raise DrawingUnsupported("Raster/external/instanced/clipped/filtered/animated SVG is not supported")
    # Text-only stylesheet is allowed; never silently discard geometry CSS.
    for n in root.iter():
        if local(n) == "style":
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", n.text or ""):
                if not all(s.strip() in {"text", ".view-title"} for s in selector.split(",")):
                    raise DrawingUnsupported("Only text-only CSS selectors are supported")
    parents = {c:p for p in root.iter() for c in p}
    for view in selected.values():
        nodes = list(view.iter())
        a = view
        while a in parents:
            a = parents[a]; nodes.append(a)
        for n in nodes:
            if local(n) not in {"svg","g","path","line","polyline","polygon","circle","ellipse","rect"}:
                raise DrawingUnsupported("Unsupported element inside a vector view: "+local(n))
            for key, value in n.attrib.items():
                if key in {"clip-path","mask","filter","class","stroke-dasharray","stroke-dashoffset","vector-effect","transform-origin"} or "href" in key:
                    raise DrawingUnsupported("Unsupported view styling or references")
                if key in {"display","visibility"} and value.strip() in {"none","hidden","collapse"}:
                    raise ValueError("Submitted projection is hidden")
                if key in {"opacity","stroke-opacity"} and (not math.isfinite(float(value)) or float(value) != 1):
                    raise DrawingUnsupported("Transparent/nonfinite linework is not supported")
            if "style" in n.attrib:
                declarations = dict(s.split(":",1) for s in n.attrib["style"].split(";") if ":" in s)
                if any(k.strip() not in {"fill","stroke","stroke-width","stroke-linecap","stroke-linejoin","font-family","font-size","font-weight"} for k in declarations):
                    raise DrawingUnsupported("Unsupported inline SVG style")
            # Confirm outlines are visible before canonicalizing paint.
            if local(n) in {"path","line","polyline","polygon","circle","ellipse","rect"}:
                inherited = {}
                chain = []; a = n
                while True:
                    chain.append(a)
                    if a not in parents: break
                    a = parents[a]
                for ancestor in reversed(chain):
                    inherited.update(ancestor.attrib)
                    inherited.update({k.strip():v.strip() for k,v in
                                      (s.split(":",1) for s in ancestor.get("style","").split(";") if ":" in s)})
                if inherited.get("stroke", "none") in {"none","transparent"}:
                    raise DrawingUnsupported("Projection must contain visible stroked linework")
                color = QtGui.QColor(inherited.get("stroke", "none"))
                # QColor does not parse SVG rgb() syntax on every Qt build.
                rgb = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", inherited.get("stroke", ""))
                if rgb: color = QtGui.QColor(*(int(v) for v in rgb.groups()))
                if not color.isValid() or color.alpha()!=255 or color.lightnessF()>0.8:
                    raise DrawingUnsupported("Projection paint contrast is outside the supported linework profile")
                if inherited.get("fill", "black") not in {"none","transparent"}:
                    raise DrawingUnsupported("Filled silhouettes are outside the linework profile")
                try: pen = float(inherited.get("stroke-width", "1").removesuffix("px"))
                except ValueError: raise DrawingUnsupported("Unsupported pen-width units")
                if not math.isfinite(pen) or pen <= 0:
                    raise ValueError("Projection has zero-width invisible strokes")


def extract(root):
    views = {}
    for n in root.iter():
        name = n.get("data-view", n.get("id", "")).upper()
        if local(n) == "g" and name in {"TOP","FRONT","SIDE"}:
            if name in views: raise ValueError("Duplicate view identity: "+name)
            views[name] = n
    if not views:
        # Backward-compatible public P1 contract: label followed by its group.
        for parent in root.iter():
            children = list(parent)
            for i,n in enumerate(children[:-1]):
                name = "".join(n.itertext()).strip()
                if local(n)=="text" and name in {"TOP","FRONT","SIDE"} and local(children[i+1])=="g":
                    if name in views: raise ValueError("Duplicate view label: "+name)
                    views[name] = children[i+1]
    if set(views) != {"TOP","FRONT","SIDE"}:
        paths=[n.get("d","") for n in root.iter() if local(n)=="path"]
        number=r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
        segment=rf"\s*M\s*{number}[ ,]+{number}\s*L\s*{number}[ ,]+{number}\s*"
        if 0<len(paths)<=3 and all(re.fullmatch(segment,s,re.I) for s in paths):
            raise ValueError("At most three single straight segments cannot represent three closed-solid orthographic views")
        raise DrawingUnsupported("Cannot reliably associate unique TOP/FRONT/SIDE vector groups")
    return views


def wrapped(root, group):
    parents = {c:p for p in root.iter() for c in p}
    current = deepcopy(group)
    parent = parents.get(group)
    while parent is not None and parent is not root:
        ancestor = ET.Element("g", dict(parent.attrib)); ancestor.append(current)
        current = ancestor; parent = parents.get(parent)
    return current


def evaluate_drawing(path, parts):
    global _application
    _application = _application or QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    data = path.read_bytes()
    if len(data)>2_000_000 or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise DrawingUnsupported("SVG size/entity profile is not supported")
    root = ET.fromstring(data)
    if sum(1 for _ in root.iter())>5000:
        raise DrawingUnsupported("SVG node budget exceeded")
    if any(local(n) in {"image","use","foreignObject","script","clipPath","mask","filter","animate","animateTransform","set","symbol"} for n in root.iter()):
        raise DrawingUnsupported("SVG representation requires an unsupported raster/reference/clipping/animation feature")
    selected = extract(root)
    validate_linework(root, selected)
    compound = Part.makeCompound([p.Shape for p in parts])
    b = compound.BoundBox
    dims = f"{b.XLength:.3f} x {b.YLength:.3f} x {b.ZLength:.3f} mm"
    texts = " ".join("".join(n.itertext()) for n in root.iter() if local(n)=="text")
    declared = re.search(r"Projection scale\s*([0-9.]+)\s*:\s*([0-9.]+)", texts, re.I)
    declared_scale = float(declared[1])/float(declared[2]) if declared else None
    unit_scale = None
    width = re.fullmatch(r"\s*([0-9.]+)\s*(mm|cm|in|px)?\s*", root.get("width", ""))
    vb = [float(v) for v in re.split(r"[ ,]+",root.get("viewBox", "").strip()) if v]
    if width and len(vb)==4 and vb[2]>0:
        unit_scale = float(width[1])*{"mm":1,"cm":10,"in":25.4,"px":25.4/96,None:25.4/96}[width[2]]/vb[2]
    if declared_scale is not None and unit_scale is None:
        raise DrawingUnsupported("Declared physical drawing scale requires supported width units and viewBox")
    observations = []
    for label, direction in (("TOP",(0,0,1)),("FRONT",(0,-1,0)),("SIDE",(1,0,0))):
        candidate = render_document(wrapped(root, selected[label])); cb = bounds(candidate)
        raw = TechDraw.projectToSVG(compound, App.Vector(*direction))
        attempts = []
        for rotation in (0,90,180,270):
            view = ET.fromstring(f'<g transform="rotate({rotation})">{raw}</g>')
            expected = render_document(view); eb = bounds(expected)
            scale = cb.width()/eb.width()
            aspect_error = abs(cb.height()/scale-eb.height())
            physical_scale = scale*unit_scale if unit_scale is not None else None
            scale_ok = declared_scale is None or abs(physical_scale-declared_scale)<=0.01*declared_scale+1e-9
            if aspect_error>CONTOUR_TOLERANCE_MM or not scale_ok:
                attempts.append({"rotation":rotation,"height_error_mm":aspect_error,"measured_scale":physical_scale,"scale_ok":scale_ok,"pass":False})
                continue
            w = math.ceil(eb.width()*PIXELS_PER_MM)+12
            h = math.ceil(eb.height()*PIXELS_PER_MM)+12
            if max(w,h)>4096:
                raise DrawingUnsupported("Projection exceeds declared raster association budget")
            cm = mask(candidate,cb,scale,w,h); em = mask(expected,eb,1,w,h)
            if not cm.any() or not em.any():
                attempts.append({"rotation":rotation,"pass":False,"reason":"Empty rendered contour"}); continue
            extra = float(distance_transform_edt(~em)[cm].max())/PIXELS_PER_MM
            missing = float(distance_transform_edt(~cm)[em].max())/PIXELS_PER_MM
            ok = max(extra,missing)<=CONTOUR_TOLERANCE_MM
            attempts.append({"rotation":rotation,"height_error_mm":aspect_error,"measured_scale":physical_scale,
                             "candidate_to_native_mm":extra,"native_to_candidate_mm":missing,
                             "candidate_pixels":int(cm.sum()),"native_pixels":int(em.sum()),"pass":ok})
            if ok: break
        observations.append({"view":label,"pass":any(a["pass"] for a in attempts),"attempts":attempts})
    ok = dims in texts and all(v["pass"] for v in observations)
    return ok, {"views":observations,"expected_envelope":dims,"dimension_text_matches":dims in texts,
                "declared_scale":declared_scale}, {"method":"Saved native BRep -> independent TechDraw orthographic HLR -> bidirectional contour distances",
                "pixels_per_mm":PIXELS_PER_MM,"maximum_contour_distance_mm":CONTOUR_TOLERANCE_MM,
                "permitted_registration":"Translation, uniform scale and quarter-turn rotation; no nonuniform warp",
                "declared_scale_relative_tolerance":0.01,"scope":"Nominal vector-view geometry association; sheet composition and manufacturing annotations unassessed"}
