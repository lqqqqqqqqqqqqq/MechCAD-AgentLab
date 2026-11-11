import * as THREE from './vendor/three/three.module.js';
import {OrbitControls} from './vendor/three/OrbitControls.js';
import {buildMeshGroup,visibleBounds,disposeGroup} from './components/mesh_scene.js';
const stage=document.querySelector('#stage'),parts=document.querySelector('#parts'),status=document.querySelector('#status'),select=document.querySelector('#case');
let group=null,serial=0;
const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(38,1,.1,100000);camera.up.set(0,0,1);
let renderer;
try{renderer=new THREE.WebGLRenderer({antialias:true});}catch(e){status.textContent='此浏览器无法创建WebGL；请使用支持WebGL的Chrome/Edge，或下载FCStd。';throw e;}
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setClearColor(0xf5f7f2);stage.append(renderer.domElement);
renderer.domElement.setAttribute('aria-label','真实CAD三维模型');
scene.add(new THREE.HemisphereLight(0xffffff,0x657264,2.8));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(150,-150,250);scene.add(light);
const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.dampingFactor=.12;
function fit(){if(!group)return;const box=visibleBounds(group);if(box.isEmpty())return;const center=box.getCenter(new THREE.Vector3());const radius=Math.max(box.getSize(new THREE.Vector3()).length()/2,1);const d=radius/Math.sin(THREE.MathUtils.degToRad(camera.fov/2))*1.25;camera.position.copy(center).add(new THREE.Vector3(1.3,-1.7,1.1).normalize().multiplyScalar(d));camera.near=Math.max(.01,d/1000);camera.far=d*100;camera.updateProjectionMatrix();controls.target.copy(center);controls.update();}
const resize=new ResizeObserver(()=>{const w=stage.clientWidth,h=stage.clientHeight;renderer.setSize(w,h);camera.aspect=w/Math.max(h,1);camera.updateProjectionMatrix();});resize.observe(stage);
document.querySelector('#fit').onclick=fit;document.querySelector('#all').onclick=()=>{for(const node of group?.children||[])node.visible=true;for(const check of parts.querySelectorAll('input'))check.checked=true;fit();};
const cases=await fetch('cases.json').then(r=>{if(!r.ok)throw new Error('案例索引加载失败');return r.json();});
for(const row of cases){const option=document.createElement('option');option.value=row.id;option.textContent=row.title;select.append(option);}
async function show(){const token=++serial;const row=cases.find(x=>x.id===select.value);status.textContent='正在加载真实派生模型…';const data=await fetch(row.mesh).then(r=>{if(!r.ok)throw new Error('模型加载失败');return r.json();});if(token!==serial)return;if(group){scene.remove(group);disposeGroup(group);}group=buildMeshGroup(data);scene.add(group);parts.replaceChildren();for(const item of group.children){const label=document.createElement('label');label.className='part';const check=document.createElement('input');check.type='checkbox';check.checked=true;check.onchange=()=>{item.visible=check.checked;};const text=document.createElement('span');text.textContent=item.userData.role||item.name;const name=document.createElement('small');name.textContent=item.name;text.append(name);label.append(check,text);parts.append(label);}document.querySelector('#title').textContent=row.title;document.querySelector('#description').textContent=row.explanation;document.querySelector('#result').textContent='已记录工程结果：'+row.recorded_engineering_result;status.textContent=`${data.parts.length} 个实际最终零件 · ${data.triangle_count.toLocaleString()} 个三角形 · 0.15mm 派生容差 · 仅作显示`;renderer.domElement.dataset.case=row.id;fit();}
select.onchange=()=>show().catch(e=>{status.textContent=e.message;});await show();
function animate(){requestAnimationFrame(animate);controls.update();renderer.render(scene,camera);}animate();
