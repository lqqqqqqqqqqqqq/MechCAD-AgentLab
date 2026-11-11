import * as THREE from '../vendor/three/three.module.js';
export function buildMeshGroup(data) {
  if(data.units!=='mm'||!Array.isArray(data.parts)||!data.parts.length)throw new Error('无可用网格零件');
  const group=new THREE.Group();let triangles=0;
  for(const part of data.parts){
    if(!Array.isArray(part.vertices)||!Array.isArray(part.triangles))throw new Error('网格格式错误');
    triangles+=part.triangles.length;if(triangles>400000)throw new Error('网格超出显示预算');
    if(part.vertices.some(v=>v.length!==3||v.some(x=>!Number.isFinite(x))))throw new Error('网格包含无效坐标');
    if(part.triangles.some(t=>t.length!==3||t.some(i=>!Number.isInteger(i)||i<0||i>=part.vertices.length)))throw new Error('网格索引错误');
    const geometry=new THREE.BufferGeometry();
    geometry.setAttribute('position',new THREE.Float32BufferAttribute(part.vertices.flat(),3));geometry.setIndex(part.triangles.flat());
    geometry.computeVertexNormals();geometry.computeBoundingBox();
    const material=new THREE.MeshStandardMaterial({color:new THREE.Color(...(part.color||[.65,.71,.73])),roughness:.85,metalness:.05,flatShading:true,side:THREE.DoubleSide});
    const item=new THREE.Mesh(geometry,material);item.name=part.name;
    item.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry,26),new THREE.LineBasicMaterial({color:0x52645b,transparent:true,opacity:.45})));
    item.userData={role:part.role,label:part.label,color_source:part.color_source};group.add(item);
  }return group;
}
export function visibleBounds(group){const box=new THREE.Box3();for(const child of group.children)if(child.visible)box.expandByObject(child);return box;}
export function disposeGroup(group){group.traverse(obj=>{obj.geometry?.dispose();obj.material?.dispose();});}
