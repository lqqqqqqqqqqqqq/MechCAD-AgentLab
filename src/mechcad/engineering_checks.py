"""Task-specific functional interfaces; actual BRep, no reference generator.

Each witness is retained in the rule observation. Finite pose coverage and
analytic plane/cylinder recognition are explicit limits of this adapter.
"""
import FreeCAD as App
import Part
from .measurements import measure, planar_contact_area, cylinder_surfaces


def inspect_interfaces(rule, roles, parts):
    checks=[]
    def add(name, ok, evidence, criterion):
        checks.append(dict(check=name,status="PASS" if ok else "FAIL",observation=evidence,criterion=criterion))
    def shape(role):return roles[role].Shape
    def box(lo,hi):return Part.makeBox(*(hi[i]-lo[i] for i in range(3)),App.Vector(*lo))
    def bounds(role,want):
        b=shape(role).BoundBox;actual=[b.XMin,b.XMax,b.YMin,b.YMax,b.ZMin,b.ZMax]
        add("bounds."+role,all(abs(a-v)<=.05+1e-9 for a,v in zip(actual,want)),actual,dict(expected=want,tolerance_mm=.05))
    def occupied(name,role,core):
        missing=core.cut(shape(role));volume=sum(abs(s.Volume) for s in missing.Solids)
        add(name,volume<=1e-5,dict(missing_mm3=volume,required_volume_mm3=core.Volume,
            regions=[dict(volume_mm3=s.Volume,center_mm=list(s.CenterOfMass)) for s in missing.Solids]),dict(maximum_missing_mm3=1e-5))
    def connected(role):
        solids=shape(role).Solids
        merged=solids[0]
        for s in solids[1:]:merged=merged.fuse(s)
        add("connected."+role,len(merged.Solids)==1,dict(native_solids=len(solids),union_solids=len(merged.Solids)),dict(material_components=1))
    def bore(role,axis,center,radius,tol=.025):
        spec=dict(kind="through_void",role=role,axis=axis,center=center,radius_mm=radius,tolerance_mm=.05,
            radius_tolerance_mm=tol,endpoint_margin_mm=.1,radial_epsilon_mm=1e-6,volume_tolerance_mm3=1e-5)
        ok,evidence,criterion=measure(spec,roles,parts)
        add("through."+role+"."+str(center),ok,evidence,criterion)
    def material_box(name,role,lo,hi,holes=()):
        core=box([v+.05 for v in lo],[v-.05 for v in hi])
        for radius,length,origin,axis in holes:
            core=core.cut(Part.makeCylinder(radius,length,App.Vector(*origin),App.Vector(*axis)))
        occupied(name,role,core)
        # An allowed through-hole envelope is a geometric tolerance allowance,
        # not permission for disconnected missing material beside that hole.
        # Inspect inner shells in the full functional slab before subtracting
        # optional-hole allowances; a genuine through-hole has no inner shell.
        region=box(lo,hi);cavities=[]
        for solid in shape(role).Solids:
            inner=[s for s in solid.Shells if not s.isSame(solid.OuterShell)]
            for shell in inner:
                cavity=Part.makeSolid(shell)
                if cavity.Volume<0:cavity.reverse()
                common=cavity.common(region)
                volume=sum(abs(s.Volume) for s in common.Solids)
                if volume>0:
                    cavities.append(dict(volume_in_functional_region_mm3=volume,inner_shell_volume_mm3=abs(cavity.Volume),center_mm=list(cavity.CenterOfMass)))
        add(name+'.sealed_voids',all(c['volume_in_functional_region_mm3']<=1e-5 for c in cavities),
            dict(cavities=cavities,functional_minimum_mm=lo,functional_maximum_mm=hi),dict(maximum_closed_missing_material_mm3=1e-5))
    def optional_holes(role,lo,hi,centers,depth):
        # An optional feature means absent OR a compliant through hole, not
        # arbitrary missing material inside an unchecked exception cylinder.
        mandatory=box([v+.05 for v in lo],[v-.05 for v in hi]);solid=shape(role)
        for x,y in centers:
            allowed=Part.makeCylinder(2.64,depth,App.Vector(x,y,lo[2]))
            missing=allowed.common(mandatory).cut(solid).Volume
            candidates=[]
            if missing>1e-5:
                for f in cylinder_surfaces(solid):
                    c=App.Vector(*f['center']);axis=App.Vector(*f['axis'])
                    if not f['concave'] or abs(abs(axis.z)-1)>1e-6 or ((c.x-x)**2+(c.y-y)**2)**.5>.05+1e-9 or abs(f['radius_mm']-2.5)>.025+1e-9:continue
                    # Only the platform/base functional slab, not an arbitrary
                    # lower carriage body, defines this optional-hole depth.
                    probe=Part.makeCylinder(f['radius_mm']-1e-6,depth,App.Vector(c.x,c.y,lo[2]))
                    blocked=solid.common(probe).Volume
                    candidates.append(dict(actual_radius_mm=f['radius_mm'],actual_axis_mm=list(c),blocked_mm3=blocked))
            add('optional_hole.'+role+'.'+str((x,y)),missing<=1e-5 or any(c['blocked_mm3']<=1e-5 for c in candidates),
                dict(missing_in_optional_envelope_mm3=missing,candidates=candidates),
                dict(absent_or_valid_through=True,diameter_mm=5,diameter_tolerance_mm=.05,axis_tolerance_mm=.05,slab_z=[lo[2],hi[2]],maximum_blocked_mm3=1e-5))
    if rule["task"]=="SLIDE-001":
        pos=rule["position_mm"]
        bounds("base",[0,160,0,80,0,10])
        base_holes=[(3.08,10,(x,y,0),(0,0,1)) for x in (14,146) for y in (5,75)]
        material_box("base.material","base",[0,0,0],[160,80,10],base_holes)
        for x in (14,146):
            for y in (5,75):bore("base",[0,0,1],[x,y,0],3)
        for role,x0 in (("left_support",8),("right_support",142)):
            bounds(role,[x0,x0+10,10,70,10,38])
            holes=[(4.08,10,(x0,y,26),(1,0,0)) for y in (22,58)]
            material_box("support.material."+role,role,[x0,10,10],[x0+10,70,38],holes)
            for y in (22,58):bore(role,[1,0,0],[x0,y,26],4,.025)
        for role,y in (("front_rod",22),("rear_rod",58)):
            bounds(role,[8,152,y-4,y+4,22,30])
            occupied("rod.material."+role,role,Part.makeCylinder(3.975-1e-6,143.9,App.Vector(8.05,y,26),App.Vector(1,0,0)))
        s=shape("carriage");top=[]
        for f in s.Faces:
            if not isinstance(f.Surface,Part.Plane):continue
            u0,u1,v0,v1=f.ParameterRange
            if f.normalAt((u0+u1)/2,(v0+v1)/2).z>1-1e-6 and abs(f.CenterOfMass.z-44)<=.05+1e-9:top.append(f)
        if top:
            b=Part.makeCompound(top).BoundBox;actual=[b.XMin,b.XMax,b.YMin,b.YMax,b.ZMin,b.ZMax]
        else:actual=[]
        want=[pos-22.5,pos+22.5,8,72,44,44]
        add("platform.functional_footprint",bool(actual) and all(abs(a-v)<=.05+1e-9 for a,v in zip(actual,want)),actual,dict(expected=want,tolerance_mm=.05))
        # Mount holes are optional. This is a maximum permitted void envelope;
        # the four full holes are checked if their presence is declared later.
        holes=[(2.64,6,(x,y,38),(0,0,1)) for x in (pos-15,pos+15) for y in (18,62)]
        material_box("platform.full_thickness","carriage",[pos-22.5,8,38],[pos+22.5,72,44],holes)
        optional_holes('carriage',[pos-22.5,8,38],[pos+22.5,72,44],[(x,y) for x in (pos-15,pos+15) for y in (18,62)],6)
        connected("carriage")
        for y in (22,58):bore("carriage",[1,0,0],[pos,y,26],4.2,.02)
    elif rule["task"]=="BRKT-001":
        pitch,height=rule["pitch_mm"],rule["height_mm"]
        bounds("fixed_bracket",[0,80,0,60,0,86]);bounds("moving_plate",[10,70,33,39,height-20,height+20])
        base_holes=[(2.64,6,(x,y,0),(0,0,1)) for x in (10,70) for y in (10,25)]
        material_box("base.material","fixed_bracket",[0,0,0],[80,60,6],base_holes)
        optional_holes('fixed_bracket',[0,0,0],[80,60,6],[(x,y) for x in (10,70) for y in (10,25)],6)
        # Exclude rectangular slot envelopes from the mandatory upright core.
        upright=box([8.05,40.05,6.05],[71.95,45.95,85.95])
        for x in (18,62):upright=upright.cut(box([x-2.58,40,28.9],[x+2.58,46,59.1]))
        occupied("upright.material","fixed_bracket",upright)
        for x in (18,62):
            core=box([x-2.475,39.9,31.5],[x+2.475,46.1,56.5])
            for z in (31.5,56.5):core=core.fuse(Part.makeCylinder(2.475,6.2,App.Vector(x,39.9,z),App.Vector(0,1,0)))
            obstruction=shape("fixed_bracket").common(core)
            widths=[];lengths=[]
            for y in (40.05,43,45.95):
                widths.append(Part.makeLine(App.Vector(x-8,y,44),App.Vector(x+8,y,44)).cut(shape("fixed_bracket")).Length)
                lengths.append(Part.makeLine(App.Vector(x,y,25),App.Vector(x,y,63)).cut(shape("fixed_bracket")).Length)
            add("slot.full_void."+str(x),obstruction.Volume<=1e-5 and all(abs(v-5)<=.05+1e-9 for v in widths) and all(abs(v-30)<=.05+1e-9 for v in lengths),
                dict(blocked_mm3=obstruction.Volume,widths_mm=widths,lengths_mm=lengths),dict(width_mm=5,length_mm=30,tolerance_mm=.05,maximum_blocked_mm3=1e-5,profiles=["stadium","rectangle"]))
        centers=[(x,z) for x in (40-pitch/2,40+pitch/2) for z in (height-pitch/2,height+pitch/2)]+[(18,height),(62,height)]
        holes=[(2.58,6,(x,33,z),(0,1,0)) for x,z in centers]
        material_box("plate.material","moving_plate",[10,33,height-20],[70,39,height+20],holes)
        connected("fixed_bracket");connected("moving_plate")
        for x,z in centers:bore("moving_plate",[0,1,0],[x,33,z],2.5)
        for role,x in (("left_bolt",18),("right_bolt",62)):
            connected(role)
            area,faces=planar_contact_area(shape(role),shape("moving_plate"))
            penetration=shape(role).common(shape("moving_plate")).Volume
            add("head.contact."+role,area>=1-1e-5 and penetration<=1e-5,dict(area_mm2=area,intersection_mm3=penetration,faces=faces),dict(minimum_area_mm2=1,maximum_intersection_mm3=1e-5))
            occupied("shaft.material."+role,role,Part.makeCylinder(1.975-1e-6,14.9,App.Vector(x,33.05,height),App.Vector(0,1,0)))
    else:raise NotImplementedError("No engineering adapter for "+str(rule["task"]))
    return all(c["status"]=="PASS" for c in checks),dict(checks=checks),dict(task=rule["task"],method="contract spatial material/void and actual functional interface checks",finite_pose_coverage=True)
