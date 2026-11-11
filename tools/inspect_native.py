"""Standalone public geometry observation; no hidden task rule or verdict."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
from native_support import read,preflight,within,sha

def main():
    p=argparse.ArgumentParser();p.add_argument('--submission',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    manifest_path=Path(a.submission).resolve();m=read(manifest_path);native=within(manifest_path.parent,m['native']);preflight(native)
    import FreeCAD as A
    import Part
    before=sha(native);doc=A.openDocument(str(native))
    try:
        parts=[]
        for role,name in m['roles'].items():
            obj=doc.getObject(name);cavities=[]
            for solid in obj.Shape.Solids:
                shells=sorted([(abs(Part.makeSolid(s).Volume),s) for s in solid.Shells],key=lambda x:x[0],reverse=True)
                for volume,shell in shells[1:]:
                    b=shell.BoundBox;cavities.append(dict(volume_mm3=volume,bounds_mm=[b.XMin,b.XMax,b.YMin,b.YMax,b.ZMin,b.ZMax]))
            parts.append(dict(role=role,name=name,native_volume_mm3=obj.Shape.Volume,closed_inner_shells=cavities))
    finally:A.closeDocument(doc.Name)
    assert before==sha(native)
    result=dict(observation='Closed material cavities in saved native solids',parts=parts,native_sha256=before,
                original_unchanged=True,formal_engineering_verdict=None,source='Public geometric observation; no frozen grading input')
    target=Path(a.out)
    if target.exists():raise FileExistsError(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')

if __name__=='__main__':main()
