// 中央库关系图（Obsidian 式力导向图）。从原 gui.html 平移，仅调整模块引用。
import { S, openSkill } from "./state.js";
import { esc, color } from "./util.js";

const GRAPH={selected:null, hover:null, view:{x:0,y:0,k:1}, nodes:[], edges:[], nmap:{}, adj:{}, bound:false, alpha:0, raf:null, nodeScale:0.4, glow:0.55, nodeAlpha:0.85};

/* ---------- 中央库关系图（Obsidian 式） ---------- */
const EDGE_COLOR={member:"#3d59a1",dist:"#7aa2f7",mcp:"#bb9af7",version:"#e0af68",
  linked:"#9ece6a",trial:"#7dcfff",conflict:"#e0af68",broken:"#f7768e",
  copy_broken:"#f7768e",copy_drift:"#ff9e64",store_drift:"#e0af68",source:"#565f89"};
const EDGE_LABEL={member:"分组成员",dist:"分发来源",mcp:"MCP 目标",version:"formal ↔ ul",
  linked:"已投影",trial:"试用 UL",conflict:"冲突",broken:"断链",copy_broken:"copy 断链",
  copy_drift:"copy 漂移",store_drift:"已投影（中央库漂移）",source:"导入来源"};
const MAPPED={linked:1,copy:1,trial:1,conflict:1,broken:1,copy_broken:1,copy_drift:1,store_drift:1};
const CAT_PALETTE=["#7aa2f7","#9ece6a","#bb9af7","#7dcfff","#e0af68","#f7768e","#73daca","#ff9e64","#2ac3de","#c0caf5","#9d7cd8","#e0de71"];
function catColor(name){
  let h=2166136261;
  for(const ch of String(name||"")) h=Math.imul(h^ch.charCodeAt(0),16777619);
  return CAT_PALETTE[(h>>>0)%CAT_PALETTE.length];
}

function sliderPct(id, fallback, min, max){
  const el=document.getElementById(id);
  const pct=el?Number(el.value):fallback;
  return Number.isFinite(pct)?Math.max(min, Math.min(max, pct)):fallback;
}
function setSliderLabel(id, pct){
  const el=document.getElementById(id);
  if(el) el.textContent=pct+"%";
}
function applyGraphStyle(){
  const size=sliderPct("graphSize",40,10,200);
  const glow=sliderPct("graphGlow",55,0,100);
  const alpha=sliderPct("graphAlpha",85,5,100);
  GRAPH.nodeScale=size/100;
  GRAPH.glow=glow/100;
  GRAPH.nodeAlpha=alpha/100;
  setSliderLabel("graphSizeVal", size);
  setSliderLabel("graphGlowVal", glow);
  setSliderLabel("graphAlphaVal", alpha);
  GRAPH.nodes.forEach(n=>{ if(n.baseR) n.r=n.baseR*GRAPH.nodeScale; });
}
function applyNodeScale(){ applyGraphStyle(); }
function hexRgba(hex, a){
  const h=String(hex||"#c0caf5").replace("#","");
  const n=h.length===3?h.split("").map(c=>c+c).join(""):h.padEnd(6,"0");
  const r=parseInt(n.slice(0,2),16)||192, g=parseInt(n.slice(2,4),16)||202, b=parseInt(n.slice(4,6),16)||245;
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, a))})`;
}

function graphFilters(){
  return {
    q:(document.getElementById("graphQ").value||"").trim().toLowerCase(),
    orphans:document.getElementById("graphOrphans").checked,
    groups:document.getElementById("graphGroups").checked,
    version:document.getElementById("graphVersion").checked,
    mcp:document.getElementById("graphMcp").checked,
    local:document.getElementById("graphLocal").checked
  };
}

function buildGraphModel(){
  const f=graphFilters();
  const nodes=[], edges=[], map={};
  const scale=GRAPH.nodeScale||0.4;
  const add=n=>{
    n.baseR=n.baseR||n.r; n.r=n.baseR*scale;
    if(!map[n.id]){ map[n.id]=n; nodes.push(n);} return map[n.id];
  };
  const link=(a,b,kind)=>{
    if(!map[a]||!map[b]||a===b) return;
    edges.push({source:a,target:b,kind});
  };
  (S.data.agents||[]).forEach(a=>add({
    id:"a:"+a.name, type:"agent", label:a.name, color:color(a.name),
    baseR:16, r:16, charge:280, detail:`${a.mode}${a.nested?" · nested":""}`
  }));
  if(f.mcp) (S.data.mcp_servers||[]).forEach(s=>{
    add({id:"m:"+s.id, type:"mcp", label:s.label||s.id, color:"#bb9af7", baseR:11, r:11, charge:140,
      detail:(s.transport||"")+" · "+((s.agents||[]).join(", ")||"无目标")});
    (s.agents||[]).forEach(ag=>link("m:"+s.id,"a:"+ag,"mcp"));
  });
  const inGroup={};
  if(f.groups) Object.entries(S.data.groups||{}).forEach(([gid,g])=>{
    const members=g.members||[];
    add({id:"g:"+gid, type:"group", label:g.name||gid, color:"#7aa2f7", baseR:12, r:12, charge:160,
      count:members.length, detail:members.length+" 个成员"});
    members.forEach(sid=>{ (inGroup[sid]=inGroup[sid]||[]).push(gid); });
  });
  const mappedSids=new Set();
  (S.data.skills||[]).forEach(s=>{
    const mapped=Object.entries(s.states||{}).filter(([,st])=>MAPPED[st]);
    if(mapped.length) mappedSids.add(s.id);
  });
  (S.data.skills||[]).forEach(s=>{
    const qhit=!f.q||(s.name+" "+s.id+" "+(s.desc||"")+" "+(s.category||"")).toLowerCase().includes(f.q);
    if(!qhit) return;
    const mapped=Object.entries(s.states||{}).filter(([,st])=>MAPPED[st]);
    if(!f.orphans && !mapped.length && !(f.groups&&inGroup[s.id])) return;
    add({id:"s:"+s.id, type:"skill", sid:s.id, label:s.name, category:s.category||"未分类",
      color: (s.risks&&s.risks.length)?"#f7768e":catColor(s.category||s.name),
      baseR: mapped.length? 6+Math.min(4,mapped.length):5, r:5, charge:90, risky:!!(s.risks&&s.risks.length),
      channel:s.channel||"formal",
      detail:(s.channel||"formal")+" · "+(mapped.length?mapped.map(([ag,st])=>ag+"="+st).join("、"):"未投影")
        +((s.risks||[]).length?" · "+s.risks.join(","):"")});
    mapped.forEach(([ag,st])=>link("s:"+s.id,"a:"+ag,st==="copy"?"linked":st));
    if(f.groups&&inGroup[s.id]) inGroup[s.id].forEach(gid=>link("g:"+gid,"s:"+s.id,"member"));
  });
  if(f.version) (S.data.skills||[]).forEach(s=>{
    if(s.channel==="formal"&&s.ul_sid&&map["s:"+s.id]&&map["s:"+s.ul_sid])
      link("s:"+s.id,"s:"+s.ul_sid,"version");
  });
  if(f.groups) Object.entries(S.data.distribution_sources||{}).forEach(([ag,src])=>{
    (src.groups||[]).forEach(gid=>link("g:"+gid,"a:"+ag,"dist"));
    (src.sids||[]).forEach(sid=>link("s:"+sid,"a:"+ag,"dist"));
  });
  if(f.local&&GRAPH.selected){
    const keep=new Set([GRAPH.selected]);
    edges.forEach(e=>{ if(e.source===GRAPH.selected) keep.add(e.target); if(e.target===GRAPH.selected) keep.add(e.source); });
    return {nodes:nodes.filter(n=>keep.has(n.id)), edges:edges.filter(e=>keep.has(e.source)&&keep.has(e.target))};
  }
  return {nodes,edges};
}

function placeGraphNodes(nodes,W,H,prev){
  const cx=W/2, cy=H/2;
  const agents=nodes.filter(n=>n.type==="agent");
  const skills=nodes.filter(n=>n.type==="skill");
  const others=nodes.filter(n=>n.type!=="agent"&&n.type!=="skill");
  agents.forEach((n,i)=>{
    if(prev[n.id]){ n.x=prev[n.id].x; n.y=prev[n.id].y; n.pin=prev[n.id].pin; return; }
    const a=-Math.PI/2+2*Math.PI*i/Math.max(agents.length,1);
    n.x=cx+Math.cos(a)*Math.min(W,H)*0.34;
    n.y=cy+Math.sin(a)*Math.min(W,H)*0.34;
  });
  skills.forEach((n,i)=>{
    if(prev[n.id]){ n.x=prev[n.id].x; n.y=prev[n.id].y; n.pin=prev[n.id].pin; return; }
    const a=2*Math.PI*(i+0.13)/Math.max(skills.length,1);
    const rad=Math.min(W,H)*(0.08+0.22*(i%7)/7);
    n.x=cx+Math.cos(a)*rad+(Math.random()-0.5)*8;
    n.y=cy+Math.sin(a)*rad+(Math.random()-0.5)*8;
  });
  others.forEach((n,i,arr)=>{
    if(prev[n.id]){ n.x=prev[n.id].x; n.y=prev[n.id].y; n.pin=prev[n.id].pin; return; }
    const a=2*Math.PI*i/Math.max(arr.length,1);
    n.x=cx+Math.cos(a)*Math.min(W,H)*0.18;
    n.y=cy+Math.sin(a)*Math.min(W,H)*0.18;
  });
}

function graphStep(W,H){
  const nodes=GRAPH.nodes, edges=GRAPH.edges;
  const n=nodes.length; if(!n) return;
  const alpha=GRAPH.alpha;
  for(let i=0;i<n;i++){
    for(let j=i+1;j<n;j++){
      const a=nodes[i], b=nodes[j];
      let dx=b.x-a.x, dy=b.y-a.y, d2=dx*dx+dy*dy||0.01;
      const dist=Math.sqrt(d2);
      const minD=a.r+b.r+8;
      let f=((a.charge+b.charge)/2)/d2;
      if(dist<minD) f+= (minD-dist)*0.08;
      dx=dx/dist*f*alpha; dy=dy/dist*f*alpha;
      if(!a.pin){ a.x-=dx; a.y-=dy; }
      if(!b.pin){ b.x+=dx; b.y+=dy; }
    }
  }
  const idx=GRAPH.idx;
  for(const e of edges){
    const a=nodes[idx[e.source]], b=nodes[idx[e.target]]; if(!a||!b) continue;
    let dx=b.x-a.x, dy=b.y-a.y, dist=Math.hypot(dx,dy)||1;
    const rest=e.kind==="version"?42:e.kind==="member"?56:e.kind==="mcp"?70:108;
    const f=(dist-rest)*0.035*alpha, fx=dx/dist*f, fy=dy/dist*f;
    if(!a.pin){ a.x+=fx; a.y+=fy; }
    if(!b.pin){ b.x-=fx; b.y-=fy; }
  }
  for(const n of nodes){
    if(n.pin) continue;
    n.x+=(W/2-n.x)*0.015*alpha;
    n.y+=(H/2-n.y)*0.015*alpha;
  }
}

function graphSize(){
  const canvas=document.getElementById("graphCanvas");
  const wrap=canvas.parentElement;
  const cssW=Math.max(320, wrap.clientWidth||900);
  const cssH=Math.max(320, canvas.clientHeight||640);
  const dpr=window.devicePixelRatio||1;
  if(canvas.width!==Math.floor(cssW*dpr)||canvas.height!==Math.floor(cssH*dpr)){
    canvas.width=Math.floor(cssW*dpr); canvas.height=Math.floor(cssH*dpr);
    canvas.style.width=cssW+"px"; canvas.style.height=cssH+"px";
  }
  return {W:cssW, H:cssH, dpr};
}

function neighborSet(id){
  const s=new Set([id]);
  (GRAPH.edges||[]).forEach(e=>{ if(e.source===id) s.add(e.target); if(e.target===id) s.add(e.source); });
  return s;
}

function drawGraph(){
  const canvas=document.getElementById("graphCanvas"); if(!canvas) return;
  const {W,H,dpr}=graphSize();
  const ctx=canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#1e1e1e"; ctx.fillRect(0,0,W,H);
  const {x:vx,y:vy,k}=GRAPH.view;
  ctx.save();
  ctx.translate(vx,vy); ctx.scale(k,k);
  const focus=GRAPH.selected||GRAPH.hover;
  const neigh=focus?neighborSet(focus):null;
  const dim=!!focus;
  GRAPH.edges.forEach(e=>{
    const a=GRAPH.nmap[e.source], b=GRAPH.nmap[e.target]; if(!a||!b) return;
    const hot=neigh&&(neigh.has(e.source)&&neigh.has(e.target));
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y);
    ctx.strokeStyle=EDGE_COLOR[e.kind]||"#444";
    ctx.globalAlpha=dim?(hot?0.9:0.06):0.42;
    ctx.lineWidth=(hot?2.2:1.15)/k;
    if(e.kind==="version"||e.kind==="dist"||e.kind==="source") ctx.setLineDash([5/k,4/k]);
    else ctx.setLineDash([]);
    ctx.stroke();
  });
  ctx.setLineDash([]);
  const glow=GRAPH.glow??0.55;
  const fillA=GRAPH.nodeAlpha??0.85;
  GRAPH.nodes.forEach(n=>{
    const hot=!dim||neigh.has(n.id);
    const dimA=hot?1:0.12;
    const col=n.color||"#c0caf5";
    const selected=GRAPH.selected===n.id;
    const glowBoost=selected||GRAPH.hover===n.id?1.25:1;
    if(glow>0.01&&hot){
      const halo=n.r*(1.6+glow*2.8*glowBoost);
      const grad=ctx.createRadialGradient(n.x,n.y,Math.max(0.2,n.r*0.15), n.x,n.y,halo);
      grad.addColorStop(0, hexRgba(col, 0.65*glow*fillA*dimA));
      grad.addColorStop(0.35, hexRgba(col, 0.28*glow*fillA*dimA));
      grad.addColorStop(1, hexRgba(col, 0));
      ctx.globalAlpha=1;
      ctx.fillStyle=grad;
      ctx.beginPath(); ctx.arc(n.x,n.y,halo,0,Math.PI*2); ctx.fill();
    }
    ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,Math.PI*2);
    ctx.fillStyle=col;
    ctx.globalAlpha=fillA*dimA;
    if(glow>0.01&&hot){
      ctx.shadowColor=hexRgba(col, 0.95*glow);
      ctx.shadowBlur=Math.max(2, n.r*(2.2+glow*6)*glowBoost);
    }else{
      ctx.shadowColor="transparent"; ctx.shadowBlur=0;
    }
    ctx.fill();
    ctx.shadowColor="transparent"; ctx.shadowBlur=0;
    if(selected){
      ctx.strokeStyle="#fff"; ctx.lineWidth=2/k; ctx.globalAlpha=Math.max(0.35, fillA); ctx.stroke();
    }
    const showLabel=n.type==="agent"||n.type==="group"||n.type==="mcp"||selected||GRAPH.hover===n.id;
    if(showLabel&&hot){
      ctx.font="12px -apple-system,'PingFang SC',sans-serif";
      ctx.textAlign="center"; ctx.textBaseline="top";
      ctx.globalAlpha=0.92;
      ctx.fillStyle="#e6e6e6";
      ctx.fillText((n.label||"").slice(0,22), n.x, n.y+n.r+4);
    }
  });
  ctx.restore();
}

function graphInfo(id){
  const el=document.getElementById("graphInfo");
  const n=GRAPH.nmap[id];
  if(!n){
    el.innerHTML=`<p class="k">每个 skill 是一个点。<br>已投影到 agent 的会连线。<br>悬停看名称 · 单击高亮邻居 · 双击打开详情</p>`;
    return;
  }
  const neigh=GRAPH.edges.filter(e=>e.source===id||e.target===id).map(e=>{
    const other=e.source===id?e.target:e.source;
    const on=GRAPH.nmap[other];
    return {kind:e.kind,label:(on&&on.label)||other,id:other,type:on&&on.type};
  });
  const typeLabel={agent:"Agent",group:"分组",skill:"Skill",mcp:"MCP"}[n.type]||n.type;
  const actions=[];
  if(n.type==="skill"&&n.sid) actions.push(`<button class="small" data-graph-open="${esc(n.sid)}">打开详情</button>`);
  el.innerHTML=`<div class="k">${esc(typeLabel)}</div><h3>${esc(n.label)}</h3>
    <p>${esc(n.detail||"")}</p>
    <div class="formrow">${actions.join("")}</div>
    <div class="k" style="margin-top:12px">连接 ${neigh.length}</div>
    <ul class="nb">${neigh.slice(0,60).map(x=>`<li><span style="color:${EDGE_COLOR[x.kind]||"#aaa"}">${esc(EDGE_LABEL[x.kind]||x.kind)}</span> · ${esc(x.label)}</li>`).join("")||"<li>无连线</li>"}</ul>`;
}

function fitCanvas(){
  const {W,H}=graphSize();
  const nodes=GRAPH.nodes; if(!nodes.length) return;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  nodes.forEach(n=>{
    minX=Math.min(minX,n.x-n.r-6); minY=Math.min(minY,n.y-n.r-18);
    maxX=Math.max(maxX,n.x+n.r+6); maxY=Math.max(maxY,n.y+n.r+18);
  });
  const bw=Math.max(40,maxX-minX), bh=Math.max(40,maxY-minY);
  const k=Math.min((W-72)/bw,(H-72)/bh,1.6);
  GRAPH.view.k=k;
  GRAPH.view.x=W/2-k*(minX+maxX)/2;
  GRAPH.view.y=H/2-k*(minY+maxY)/2;
}
function graphWake(){
  GRAPH.alpha=Math.max(GRAPH.alpha,0.7);
  if(!GRAPH.raf) GRAPH.raf=requestAnimationFrame(graphTick);
}
function graphTick(){
  const {W,H}=graphSize();
  for(let i=0;i<4;i++) graphStep(W,H);
  GRAPH.alpha*=0.93;
  drawGraph();
  if(GRAPH.alpha>0.025) GRAPH.raf=requestAnimationFrame(graphTick);
  else {
    GRAPH.raf=null;
    if(GRAPH.needFit){ fitCanvas(); GRAPH.needFit=false; drawGraph(); }
  }
}

export function renderGraph(relayout){
  if(!S.data||document.getElementById("skGraph").hidden) return;
  applyNodeScale();
  const {W,H}=graphSize();
  const prev=Object.fromEntries((GRAPH.nodes||[]).map(n=>[n.id,{x:n.x,y:n.y,pin:n.pin}]));
  const model=buildGraphModel();
  placeGraphNodes(model.nodes,W,H, relayout?{}:prev);
  if(!relayout) model.nodes.forEach(n=>{ const p=prev[n.id]; if(p){ n.x=p.x; n.y=p.y; n.pin=p.pin; }});
  GRAPH.nodes=model.nodes; GRAPH.edges=model.edges;
  GRAPH.nmap=Object.fromEntries(model.nodes.map(n=>[n.id,n]));
  GRAPH.idx=Object.fromEntries(model.nodes.map((n,i)=>[n.id,i]));
  const mapped=model.edges.filter(e=>MAPPED[e.kind]||e.kind==="linked").length;
  const stat=document.getElementById("graphStat");
  if(stat) stat.textContent=`${model.nodes.length} 个节点 · ${model.edges.length} 条边 · ${mapped} 条投影`;
  graphInfo(GRAPH.selected);
  bindGraphEvents();
  if(relayout){ GRAPH.view={x:0,y:0,k:1}; }
  const size=graphSize();
  GRAPH.alpha=1;
  for(let i=0;i<50;i++) graphStep(size.W,size.H);
  GRAPH.alpha=0.45;
  if(relayout) fitCanvas();
  drawGraph();
  graphWake();
}

function hitNode(px,py){
  const {x:vx,y:vy,k}=GRAPH.view;
  const wx=(px-vx)/k, wy=(py-vy)/k;
  for(let i=GRAPH.nodes.length-1;i>=0;i--){
    const n=GRAPH.nodes[i];
    const extra=n.type==="agent"?6:3;
    if((n.x-wx)**2+(n.y-wy)**2 <= (n.r+extra)**2) return n.id;
  }
  return null;
}

function bindGraphEvents(){
  if(GRAPH.bound) return;
  GRAPH.bound=true;
  const canvas=document.getElementById("graphCanvas");
  const hint=document.getElementById("graphHint");
  let mode=null, last={x:0,y:0}, drag=null;
  canvas.addEventListener("wheel",e=>{
    e.preventDefault();
    const rect=canvas.getBoundingClientRect();
    const mx=e.clientX-rect.left, my=e.clientY-rect.top;
    const wx=(mx-GRAPH.view.x)/GRAPH.view.k, wy=(my-GRAPH.view.y)/GRAPH.view.k;
    const nk=Math.max(0.2, Math.min(5, GRAPH.view.k*(e.deltaY<0?1.12:0.89)));
    GRAPH.view.k=nk; GRAPH.view.x=mx-wx*nk; GRAPH.view.y=my-wy*nk;
    drawGraph();
  },{passive:false});
  canvas.addEventListener("pointerdown",e=>{
    const rect=canvas.getBoundingClientRect();
    const id=hitNode(e.clientX-rect.left, e.clientY-rect.top);
    last={x:e.clientX,y:e.clientY};
    if(id){ mode="node"; drag=id; GRAPH.selected=id; graphInfo(id); drawGraph(); canvas.classList.add("dragging"); }
    else { mode="pan"; GRAPH.selected=null; graphInfo(null); drawGraph(); canvas.classList.add("dragging"); }
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove",e=>{
    const rect=canvas.getBoundingClientRect();
    const dx=e.clientX-last.x, dy=e.clientY-last.y;
    if(!mode){
      const id=hitNode(e.clientX-rect.left, e.clientY-rect.top);
      if(id!==GRAPH.hover){
        GRAPH.hover=id; drawGraph();
        if(id&&GRAPH.nmap[id]){
          hint.hidden=false; hint.textContent=GRAPH.nmap[id].label;
          hint.style.left=(e.clientX-rect.left)+"px"; hint.style.top=(e.clientY-rect.top)+"px";
        }else hint.hidden=true;
      }else if(id){
        hint.style.left=(e.clientX-rect.left)+"px"; hint.style.top=(e.clientY-rect.top)+"px";
      }
      return;
    }
    last={x:e.clientX,y:e.clientY};
    if(mode==="pan"){ GRAPH.view.x+=dx; GRAPH.view.y+=dy; drawGraph(); }
    else if(mode==="node"&&drag&&GRAPH.nmap[drag]){
      const n=GRAPH.nmap[drag]; n.pin=true;
      n.x+=dx/GRAPH.view.k; n.y+=dy/GRAPH.view.k;
      graphWake();
    }
  });
  canvas.addEventListener("pointerup",()=>{ mode=null; drag=null; canvas.classList.remove("dragging"); });
  canvas.addEventListener("pointerleave",()=>{ if(!mode){ GRAPH.hover=null; hint.hidden=true; drawGraph(); }});
  canvas.addEventListener("dblclick",e=>{
    const rect=canvas.getBoundingClientRect();
    const id=hitNode(e.clientX-rect.left, e.clientY-rect.top);
    const n=id&&GRAPH.nmap[id];
    if(n&&n.type==="skill"&&n.sid) openSkill(n.sid);
  });
  document.getElementById("graphInfo").addEventListener("click",e=>{
    const open=e.target.closest("[data-graph-open]");
    if(open) openSkill(open.dataset.graphOpen);
  });
  ["graphQ","graphOrphans","graphGroups","graphVersion","graphMcp","graphLocal"].forEach(id=>{
    const el=document.getElementById(id); if(!el) return;
    el.addEventListener(id==="graphQ"?"input":"change",()=>renderGraph(false));
  });
  ["graphSize","graphGlow","graphAlpha"].forEach(id=>{
    const el=document.getElementById(id); if(!el) return;
    el.addEventListener("input",()=>{ applyGraphStyle(); drawGraph(); });
  });
  document.getElementById("graphReset").addEventListener("click",()=>{
    GRAPH.view={x:0,y:0,k:1}; GRAPH.selected=null; GRAPH.hover=null;
    document.getElementById("graphLocal").checked=false;
    const size=document.getElementById("graphSize");
    const glow=document.getElementById("graphGlow");
    const alpha=document.getElementById("graphAlpha");
    if(size) size.value="40";
    if(glow) glow.value="55";
    if(alpha) alpha.value="85";
    GRAPH.nodes=[];
    renderGraph(true);
  });
  window.addEventListener("resize",()=>{ if(!document.getElementById("skGraph").hidden) drawGraph(); });
}
