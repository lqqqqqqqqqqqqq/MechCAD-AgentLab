"""Public native-file safety and representation helpers in a read-only tool bundle."""
from pathlib import Path
import hashlib
import json
import zipfile
import xml.etree.ElementTree as ET

APPROVED = {'Spreadsheet::Sheet','Part::Box','Part::Cylinder','Part::Cone','Part::Sphere','Part::Cut','Part::Fuse',
 'Part::Common','Part::MultiFuse','Part::Compound','Part::Feature','Part::Fillet','Part::Chamfer','PartDesign::Body',
 'PartDesign::Pad','PartDesign::Pocket','Sketcher::SketchObject','App::DocumentObjectGroup','App::Origin',
 'App::Line','App::Plane','App::Point'}

class Unsupported(Exception):
    pass

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf8')

def within(base,name):
    if not isinstance(name,str) or '\\' in name or ':' in name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise Unsupported('Invalid relative native artifact path')
    p=(base/name).resolve()
    if not p.is_relative_to(base.resolve()):raise Unsupported('Artifact outside input root')
    return p

def preflight(path):
    with zipfile.ZipFile(path) as z:
        if sum(x.file_size for x in z.infolist())>250_000_000:raise Unsupported('Native archive resource limit')
        data=z.read('Document.xml')
        if len(data)>25_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise Unsupported('Unsafe native XML')
        tree=ET.fromstring(data)
        types=sorted({x.get('type') for x in tree.findall('.//Objects/Object')})
        if set(types)-APPROVED:raise Unsupported('Native types outside declared support: '+repr(sorted(set(types)-APPROVED)))
        if any('Python' in x.get('type','') or 'XLink' in x.get('type','') for x in tree.iter('Property')):
            raise Unsupported('Native executable proxy or external document links')
        return dict(types=types,has_gui_metadata='GuiDocument.xml' in z.namelist())

def terminal_solids(doc):
    candidates=[x for x in doc.Objects if hasattr(x,'Shape') and not x.Shape.isNull() and x.Shape.Volume>1e-9]
    consumed=set()
    def targets(v):
        if hasattr(v,'Name') and hasattr(v,'Document'):yield v
        elif isinstance(v,(list,tuple)):
            for item in v:yield from targets(item)
    for x in candidates:
        for prop in x.PropertiesList:
            if x.getTypeIdOfProperty(prop).startswith('App::PropertyLink'):
                for target in targets(getattr(x,prop)):
                    owns=target.TypeId=='PartDesign::Body' and x in target.Group
                    if target.Name!=x.Name and not owns:consumed.add(target.Name)
    return [x for x in candidates if x.Name not in consumed]

def saved_visibility(path):
    result={}
    with zipfile.ZipFile(path) as z:
        if 'GuiDocument.xml' not in z.namelist():return result
        data=z.read('GuiDocument.xml')
        if len(data)>25_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise Unsupported('Unsafe GUI metadata')
        tree=ET.fromstring(data)
        for v in tree.findall('.//ViewProvider'):
            node=v.find("./Properties/Property[@name='Visibility']/Bool")
            if node is not None:result[v.get('name')]=node.get('value')=='true'
    return result

def is_visible(obj, flags, visited=None):
    visited=set() if visited is None else visited
    if obj.Name in visited:return True
    visited.add(obj.Name)
    if flags.get(obj.Name) is False:return False
    # Container visibility applies, but scalar expression consumers do not.
    for parent in obj.InList:
        if getattr(parent,'TypeId','')=='App::DocumentObjectGroup' and not is_visible(parent,flags,visited):return False
    return True

def force(doc):
    for obj in doc.Objects:
        if hasattr(obj,'touch'):obj.touch()
    doc.recompute(None,True,True)
    bad=[dict(name=o.Name,state=list(o.State)) for o in doc.Objects if any(x in ('Invalid','Error') for x in o.State)]
    if bad:raise ValueError('Native recompute error: '+json.dumps(bad))

def resolve_roles(doc,manifest):
    mapping=manifest.get('roles',{})
    if not isinstance(mapping,dict) or len(set(mapping.values()))!=len(mapping):raise Unsupported('Distinct role mapping required')
    actual={o.Name:o for o in terminal_solids(doc)}
    if set(actual)!=set(mapping.values()):raise Unsupported('Role declaration does not describe complete terminal solids')
    return {role:actual[name] for role,name in mapping.items()}
