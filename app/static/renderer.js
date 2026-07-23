const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));

function mat4Perspective(fov, aspect, near, far) {
  const f = 1 / Math.tan(fov / 2), nf = 1 / (near - far);
  return new Float32Array([
    f/aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0
  ]);
}
function mat4Identity() { return new Float32Array([1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]); }
function mat4Multiply(a,b) {
  const out = new Float32Array(16);
  for(let c=0;c<4;c++) for(let r=0;r<4;r++) {
    out[c*4+r]=a[0*4+r]*b[c*4+0]+a[1*4+r]*b[c*4+1]+a[2*4+r]*b[c*4+2]+a[3*4+r]*b[c*4+3];
  }
  return out;
}
function mat4Translate(x,y,z) {
  const m=mat4Identity(); m[12]=x;m[13]=y;m[14]=z;return m;
}
function mat4RotateX(a) {
  const c=Math.cos(a),s=Math.sin(a); return new Float32Array([1,0,0,0,0,c,s,0,0,-s,c,0,0,0,0,1]);
}
function mat4RotateY(a) {
  const c=Math.cos(a),s=Math.sin(a); return new Float32Array([c,0,-s,0,0,1,0,0,s,0,c,0,0,0,0,1]);
}
function mat4Scale(s) { return new Float32Array([s,0,0,0,0,s,0,0,0,0,s,0,0,0,0,1]); }

function compile(gl, type, source) {
  const shader=gl.createShader(type); gl.shaderSource(shader,source); gl.compileShader(shader);
  if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
  return shader;
}
function program(gl, vs, fs) {
  const p=gl.createProgram(); gl.attachShader(p,compile(gl,gl.VERTEX_SHADER,vs));gl.attachShader(p,compile(gl,gl.FRAGMENT_SHADER,fs));gl.linkProgram(p);
  if(!gl.getProgramParameter(p,gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p)); return p;
}

export class ExactHeadRenderer {
  constructor(canvas) {
    this.canvas=canvas; this.gl=canvas.getContext('webgl2',{antialias:true,alpha:true,preserveDrawingBuffer:true});
    if(!this.gl) throw new Error('WebGL 2 est nécessaire.');
    this.theta=null; this.targetTheta=null; this.animStart=0; this.animDuration=0; this.animFrom=null;
    this.yaw=0; this.pitch=-0.02; this.zoom=1; this.autoTurn=0; this.drag=false;
  }
  async initialize() {
    const [runtimeResponse, controlsResponse] = await Promise.all([
      fetch('/static/assets/fc26_exact_runtime_lod0.bin'),
      fetch('/static/assets/shape_controls.json')
    ]);
    if(!runtimeResponse.ok || !controlsResponse.ok) throw new Error('Assets FC26 introuvables.');
    this.controls=await controlsResponse.json();
    this.axisMeta=[];
    this.controls.forEach((control,controlIndex)=>control.axes.forEach((axis,axisIndex)=>this.axisMeta.push({...axis,control,controlIndex,axisIndex})));
    this.axisMorphIndices=new Uint32Array(this.axisMeta.map(axis=>axis.morph_index));
    this.parseRuntime(await runtimeResponse.arrayBuffer());
    this.theta=new Float32Array(this.axisMeta.length); this.targetTheta=new Float32Array(this.axisMeta.length);
    this.buildGL(); this.bindControls(); this.resize();
    addEventListener('resize',()=>this.resize());
    requestAnimationFrame(t=>this.frame(t));
    return {vertices:this.vertexCount,triangles:this.faceCount,morphs:this.morphCount,axes:this.axisMeta.length,controls:this.controls.length};
  }
  parseRuntime(buffer) {
    const view=new DataView(buffer); const magic=String.fromCharCode(...new Uint8Array(buffer,0,4));
    if(magic!=='FCM1') throw new Error('Format renderer FC26 invalide.');
    let o=4;
    this.vertexCount=view.getUint32(o,true);o+=4;this.faceCount=view.getUint32(o,true);o+=4;this.morphCount=view.getUint32(o,true);o+=4;this.pairCount=view.getUint32(o,true);o+=4;this.headVertexCount=view.getUint32(o,true);o+=4;
    this.basePositions=new Float32Array(buffer,o,this.vertexCount*3).slice();o+=this.vertexCount*3*4;
    this.faces=new Uint32Array(buffer,o,this.faceCount*3).slice();o+=this.faceCount*3*4;
    this.offsets=new Uint32Array(buffer,o,this.morphCount+1).slice();o+=(this.morphCount+1)*4;
    this.deltaIndices=new Uint16Array(buffer,o,this.pairCount).slice();o+=this.pairCount*2;
    this.deltas=new Float32Array(buffer,o,this.pairCount*3).slice();
    const head=[],eyes=[];
    for(let i=0;i<this.faces.length;i+=3){const target=(this.faces[i]>=this.headVertexCount&&this.faces[i+1]>=this.headVertexCount&&this.faces[i+2]>=this.headVertexCount)?eyes:head;target.push(this.faces[i],this.faces[i+1],this.faces[i+2]);}
    this.headFaces=new Uint32Array(head);this.eyeFaces=new Uint32Array(eyes);
    this.positions=this.basePositions.slice();this.normals=new Float32Array(this.positions.length);
  }
  buildGL() {
    const gl=this.gl;
    this.p=program(gl,`#version 300 es
precision highp float;layout(location=0)in vec3 aPosition;layout(location=1)in vec3 aNormal;uniform mat4 uMVP;uniform mat4 uModel;out vec3 vNormal;out vec3 vWorld;void main(){vec4 world=uModel*vec4(aPosition,1.0);vWorld=world.xyz;vNormal=mat3(uModel)*aNormal;gl_Position=uMVP*vec4(aPosition,1.0);}`,
`#version 300 es
precision highp float;in vec3 vNormal;in vec3 vWorld;uniform vec3 uColor;uniform float uEye;out vec4 outColor;void main(){vec3 n=normalize(vNormal);vec3 l=normalize(vec3(-0.55,0.8,0.75));float diffuse=max(dot(n,l),0.0);float fill=max(dot(n,normalize(vec3(0.65,0.25,0.45))),0.0)*0.28;float rim=pow(1.0-max(dot(n,normalize(vec3(0.0,0.05,1.0))),0.0),2.4);vec3 c=uColor*(0.28+0.72*diffuse+fill)+vec3(0.16,0.21,0.24)*rim;if(uEye>0.5)c=mix(c,vec3(0.91,0.93,0.91),0.72);outColor=vec4(c,1.0);}`);
    this.posBuffer=gl.createBuffer();this.normalBuffer=gl.createBuffer();
    this.headIndex=gl.createBuffer();gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,this.headIndex);gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,this.headFaces,gl.STATIC_DRAW);
    this.eyeIndex=gl.createBuffer();gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,this.eyeIndex);gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,this.eyeFaces,gl.STATIC_DRAW);
    this.uMVP=gl.getUniformLocation(this.p,'uMVP');this.uModel=gl.getUniformLocation(this.p,'uModel');this.uColor=gl.getUniformLocation(this.p,'uColor');this.uEye=gl.getUniformLocation(this.p,'uEye');
    gl.enable(gl.DEPTH_TEST);gl.disable(gl.CULL_FACE);
    this.rebuildGeometry();
  }
  recomputeNormals() {
    const n=this.normals;n.fill(0);const p=this.positions,f=this.faces;
    for(let i=0;i<f.length;i+=3){const ia=f[i]*3,ib=f[i+1]*3,ic=f[i+2]*3;const abx=p[ib]-p[ia],aby=p[ib+1]-p[ia+1],abz=p[ib+2]-p[ia+2],acx=p[ic]-p[ia],acy=p[ic+1]-p[ia+1],acz=p[ic+2]-p[ia+2];const x=aby*acz-abz*acy,y=abz*acx-abx*acz,z=abx*acy-aby*acx;n[ia]+=x;n[ia+1]+=y;n[ia+2]+=z;n[ib]+=x;n[ib+1]+=y;n[ib+2]+=z;n[ic]+=x;n[ic+1]+=y;n[ic+2]+=z;}
    for(let i=0;i<n.length;i+=3){const l=Math.hypot(n[i],n[i+1],n[i+2])||1;n[i]/=l;n[i+1]/=l;n[i+2]/=l;}
  }
  rebuildGeometry() {
    this.positions.set(this.basePositions);
    for(let axis=0;axis<this.theta.length;axis++){const value=this.theta[axis];if(Math.abs(value)<1e-6)continue;const morph=this.axisMorphIndices[axis],start=this.offsets[morph],end=this.offsets[morph+1];for(let j=start;j<end;j++){const v=this.deltaIndices[j]*3,d=j*3;this.positions[v]+=this.deltas[d]*value;this.positions[v+1]+=this.deltas[d+1]*value;this.positions[v+2]+=this.deltas[d+2]*value;}}
    this.recomputeNormals();const gl=this.gl;gl.bindBuffer(gl.ARRAY_BUFFER,this.posBuffer);gl.bufferData(gl.ARRAY_BUFFER,this.positions,gl.DYNAMIC_DRAW);gl.bindBuffer(gl.ARRAY_BUFFER,this.normalBuffer);gl.bufferData(gl.ARRAY_BUFFER,this.normals,gl.DYNAMIC_DRAW);
  }
  setTheta(values, duration=520) {
    const next=values instanceof Float32Array?values:new Float32Array(values);if(next.length!==this.theta.length)throw new Error('Vecteur morph invalide.');
    if(duration<=0){this.theta.set(next);this.targetTheta.set(next);this.rebuildGeometry();return;}
    this.animFrom=this.theta.slice();this.targetTheta.set(next);this.animStart=performance.now();this.animDuration=duration;
  }
  setSparse(pairs,duration=480){const next=new Float32Array(this.theta.length);pairs.forEach(([i,v])=>{if(i>=0&&i<next.length)next[i]=v});this.setTheta(next,duration);}
  setAxis(index,value){this.theta[index]=clamp(value,-1,1);this.targetTheta[index]=this.theta[index];this.animDuration=0;this.rebuildGeometry();}
  view(name){if(name==='front'){this.yaw=0;this.pitch=-.02}else if(name==='three'){this.yaw=-.62;this.pitch=-.03}else if(name==='profile'){this.yaw=-1.52;this.pitch=-.02}else{this.yaw=.15;this.pitch=-.08}this.autoTurn=0;}
  bindControls(){const c=this.canvas;c.addEventListener('pointerdown',e=>{this.drag=true;this.lastX=e.clientX;this.lastY=e.clientY;c.setPointerCapture(e.pointerId)});c.addEventListener('pointermove',e=>{if(!this.drag)return;this.yaw+=(e.clientX-this.lastX)*.008;this.pitch=clamp(this.pitch+(e.clientY-this.lastY)*.006,-.7,.7);this.lastX=e.clientX;this.lastY=e.clientY});c.addEventListener('pointerup',()=>this.drag=false);c.addEventListener('wheel',e=>{e.preventDefault();this.zoom=clamp(this.zoom*Math.exp(-e.deltaY*.001),.65,1.65)},{passive:false});}
  resize(){const dpr=Math.min(devicePixelRatio||1,2),w=Math.max(1,this.canvas.clientWidth),h=Math.max(1,this.canvas.clientHeight);if(this.canvas.width!==Math.floor(w*dpr)||this.canvas.height!==Math.floor(h*dpr)){this.canvas.width=Math.floor(w*dpr);this.canvas.height=Math.floor(h*dpr);this.gl.viewport(0,0,this.canvas.width,this.canvas.height);}}
  frame(time){
    if(this.animDuration>0){const t=clamp((time-this.animStart)/this.animDuration,0,1),ease=1-Math.pow(1-t,3);for(let i=0;i<this.theta.length;i++)this.theta[i]=this.animFrom[i]+(this.targetTheta[i]-this.animFrom[i])*ease;this.rebuildGeometry();if(t>=1)this.animDuration=0;}
    this.draw();requestAnimationFrame(t=>this.frame(t));
  }
  draw(){const gl=this.gl;this.resize();gl.clearColor(0.025,0.032,0.044,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(this.p);const aspect=this.canvas.width/this.canvas.height,projection=mat4Perspective(.58,aspect,.05,10);let model=mat4Translate(0,0,-.95/this.zoom);model=mat4Multiply(model,mat4RotateX(this.pitch));model=mat4Multiply(model,mat4RotateY(this.yaw));model=mat4Multiply(model,mat4Scale(1.50));model=mat4Multiply(model,mat4Translate(0,-1.68,-.045));const mvp=mat4Multiply(projection,model);gl.uniformMatrix4fv(this.uMVP,false,mvp);gl.uniformMatrix4fv(this.uModel,false,model);gl.bindBuffer(gl.ARRAY_BUFFER,this.posBuffer);gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,this.normalBuffer);gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,3,gl.FLOAT,false,0,0);gl.uniform3f(this.uColor,.55,.34,.25);gl.uniform1f(this.uEye,0);gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,this.headIndex);gl.drawElements(gl.TRIANGLES,this.headFaces.length,gl.UNSIGNED_INT,0);gl.uniform3f(this.uColor,.9,.92,.9);gl.uniform1f(this.uEye,1);gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,this.eyeIndex);gl.drawElements(gl.TRIANGLES,this.eyeFaces.length,gl.UNSIGNED_INT,0);}
}
