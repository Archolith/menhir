(function(){
"use strict";
const byId=id=>document.getElementById(id);
const qs=(s,root=document)=>root.querySelector(s);
const qsa=(s,root=document)=>Array.from(root.querySelectorAll(s));
const node=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
const data={
 identity:{
  id:"6fa3-view-identity",title:"Stable entity identity",kind:"DERIVED VIEW",status:"current",date:"Learned Sep 2026",sourceType:"Architecture decision",sources:2,
  summary:"Stable UUIDs prevent accidental entity duplication across ingestion and reconciliation.",
  inline:["Menhir uses ","stable UUIDs"," as canonical identifiers shared across repeated observations"," to prevent entities from being duplicated during ingestion."],
  explanation:"The same conceptual entity may be observed in multiple sessions. A stable UUID makes it possible to merge evidence onto the canonical record without replacing its identity. This is an illustrative derived View: inspect its inputs to establish what the source actually asserted.",
  relation:[["merge","SUPPORTS → canonical merge strategy"],["receipt","MENTIONED IN ← episode"],["legacy","SUPERSEDES → prior approach"]]
 },
 merge:{
  id:"058d-view-merge",title:"Canonical merge rules",kind:"DERIVED VIEW",status:"current",date:"Learned Aug 2026",sourceType:"Architecture decision",sources:2,
  summary:"The merge process associates repeated observations with a single canonical identifier.",
  inline:["Menhir uses a ","canonical identifier"," to reconcile repeated entity sightings across sessions"," instead of creating new identities for each observation."],
  explanation:"This example rule is a present interpretation of architecture discussions. The important distinction is between the interpreted rule and the observations which led to it.",
  relation:[["identity","SUPPORTS → stable identity"],["receipt","GROUNDED IN → ingestion receipt"],["legacy","SUPERSEDES → earlier merge approach"]]
 },
 receipt:{
  id:"889f-episode-receipt",title:"Ingestion source receipt",kind:"EPISODIC SOURCE",status:"current",date:"Recorded Sep 2026",sourceType:"Ingestion episode",sources:1,
  summary:"An ingestion receipt records its own identity and links to a resolved episode when enrichment finishes.",
  inline:["An ingestion ","receipt UUID"," records the original write anchor separately from the resolved episode identifier"," so provenance remains traceable after enrichment."],
  explanation:"Menhir's own ingestion receipt UUID may differ from the Graphiti episode UUID. The inspector should show both only when present, never infer their relationship from similar titles.",
  relation:[["identity","MENTIONS → identity view"],["merge","INFORMS → canonical merge rule"]]
 },
 legacy:{
  id:"114e-old-policy",title:"Previous merge approach",kind:"HISTORICAL VIEW",status:"historical",date:"Learned Jul 2026",sourceType:"Superseded design",sources:0,
  summary:"An earlier exploratory merge approach was replaced by a canonical-UUID rule.",
  inline:["The ","previous approach"," did not yet consistently anchor entities to one canonical UUID"," and is shown for historical inspection only."],
  explanation:"Historical records can help explain why the current rule exists. They are not evidence that a superseded design remains active. No primary supporting episode is supplied in this fixture.",
  relation:[["identity","SUPERSEDED BY → stable identity"],["merge","REPLACED BY → canonical merge"]]
 },
 evidence:{
  id:"ca32-source-architecture",title:"Architecture design note",kind:"SOURCE RECORD",status:"current",date:"Recorded Sep 2026",sourceType:"Document source",sources:1,
  summary:"The original architecture note describes identity resolution as a separate concern from observing an entity.",
  inline:["The source ","design note"," records a decision about canonical identities and observations"," that a derived View can cite directly."],
  explanation:"This fixture imitates an original document source. A real implementation must preserve document identity, version, path, quote and the content visibility policy.",
  relation:[["identity","SUPPORTS → stable entity identity"],["merge","INFORMS → canonical merge"]]
 },
 policy:{
  id:"20c1-view-governance",title:"Admission and provenance policy",kind:"POLICY VIEW",status:"current",date:"Learned Oct 2026",sourceType:"Governance decision",sources:1,
  summary:"A claim's authority must be distinguishable from the evidence used to support it.",
  inline:["Menhir keeps ","claim authority"," separate from the input evidence and the source's claimed authority"," so derived statements never masquerade as eyewitness records."],
  explanation:"The authority attached to a materialized View must be explained through explicit provenance and currentness, not assumed from graph proximity or from an apparently confident summary.",
  relation:[["identity","GOVERNS → identity View"],["evidence","REFERENCES → source record"]]
 }
};
const order=["identity","merge","receipt","legacy","evidence","policy"];
const stages={
 claim:{
  kind:"DERIVED VIEW",state:"CURRENT",title:"Canonical identity",description:"This is the interpreted current claim. It is not itself the original observation or a verbatim source excerpt.",
  quote:"Stable UUIDs prevent accidental entity duplication across ingestion and reconciliation.",
  properties:[["Record UUID","6fa3-view-identity"],["Authority","Derived current View"],["Learned at","Sep 2026 (example)"],["Contributors","2 linked source records"]]
 },
 episode:{
  kind:"EPISODE",state:"SOURCE",title:"Ingestion receipt",description:"An ingestion record captures an observed event. Its own UUID remains distinct from any resolved Graphiti episode UUID.",
  quote:"Resolved identity observed during enrichment. Associated receipt UUID and source reference retained separately.",
  properties:[["Receipt UUID","889f-episode-receipt"],["Resolved episode","example-resolved-episode"],["Recorded at","Sep 2026 (example)"],["Relation","MENTIONS → entity"]]
 },
 source:{
  kind:"ORIGINAL SOURCE",state:"AVAILABLE",title:"Architecture note",description:"The underlying written source supports the derived interpretation. Its quotation and identity should remain inspectable under the user's permissions.",
  quote:"The identity of an entity is independent of its observation and must remain stable across ingestion.",
  properties:[["Source kind","Design document"],["Source ID","ca32-source-architecture"],["Version","Illustrative fixture"],["Reference","Architecture source excerpt"]]
 }
};
const learnedEvents=[
 {when:"Sep 2026 · learned",title:"Current rule recorded",detail:"Stable canonical UUIDs are recognized as the operative identity rule.",status:"current"},
 {when:"Aug 2026 · learned",title:"Revised merge design",detail:"The canonical merge strategy supersedes the former exploratory approach.",status:"current"},
 {when:"Jul 2026 · learned",title:"Previous approach documented",detail:"An earlier technique is kept for review as a historical design.",status:"historical"}
];
const worldEvents=[
 {when:"World time unavailable",title:"Identity rule applies",detail:"No trustworthy effective date was supplied in this synthetic record; an ingestion timestamp must not stand in.",status:"current"},
 {when:"World time unavailable",title:"Prior policy replaced",detail:"The historical status is known in the fixture, but no verified world-time replacement date is supplied.",status:"historical"}
];
let page="browse",memory="identity",filter="all",search="",stage="claim",timeBasis="learned",timeIndex=0,depth=1,historical=false,graphFocus="identity";
const clean=str=>String(str??"");
const set=(id,txt)=>{const e=byId(id);if(e)e.textContent=clean(txt)};
function route(next,selected){
 if(selected && data[selected])memory=selected;
 location.hash=next+(next==="inspect"?"/"+memory:"");
 if(location.hash==="#"+next+(next==="inspect"?"/"+memory:""))renderRoute();
}
function renderRoute(){
 const hash=decodeURIComponent(location.hash.replace(/^#/,"")||"browse");
 const [p,m]=hash.split("/");
 page=["browse","inspect","evidence","timeline","graph"].includes(p)?p:"browse";
 if(m&&data[m])memory=m;
 qsa(".page").forEach(e=>e.classList.toggle("active",e.dataset.page===page));
 qsa("[data-nav]").forEach(e=>{const active=e.dataset.nav===page;e.classList.toggle("active",active);if(active)e.setAttribute("aria-current","page");else e.removeAttribute("aria-current")});
 byId("mobilePage").value=page;
 if(page==="browse")renderBrowse();
 if(page==="inspect")renderReader();
 if(page==="evidence")renderStage();
 if(page==="timeline")renderTimeline();
 if(page==="graph")renderGraph();
 window.scrollTo({top:0,behavior:"instant"});
}
function renderBrowse(){
 qsa("[data-filter]").forEach(b=>{b.classList.toggle("active",b.dataset.filter===filter);b.setAttribute("aria-pressed",String(b.dataset.filter===filter))});
 const results=byId("memoryResults");results.replaceChildren();
 const list=order.filter(id=>(filter==="all"||data[id].status===filter)&&((data[id].title+" "+data[id].summary+" "+data[id].kind).toLowerCase().includes(search)));
 if(list.length===0){results.append(node("div","empty-state","No matching fixture records. Try a different search or filter."));return}
 list.forEach(id=>{
  const d=data[id],item=node("button","memory-row");
  item.type="button";item.setAttribute("aria-label","Inspect "+d.title);
  const symbol=node("div","mark"+(id==="receipt"?" episodic":id==="legacy"?" history":""),id==="receipt"?"◉":id==="legacy"?"◷":"◇");
  const info=node("div","memory-row-info");info.append(node("strong",null,d.title),node("small",null,d.summary));
  const flag=node("span","status-inline",d.status==="historical"?"Historical":d.sources+" sources");
  item.append(symbol,info,flag,node("span","arrow","↗"));
  item.addEventListener("click",()=>route("inspect",id));results.append(item);
 })
}
function makeInline(d){
 const target=byId("readerStatement");target.replaceChildren();
 const btn=node("button","inline-expand",d.inline[1]+" +");btn.type="button";btn.setAttribute("aria-expanded","false");btn.setAttribute("aria-controls","inlineInsertion");
 const extra=node("span","inline-insertion",d.inline[2]);extra.id="inlineInsertion";extra.hidden=true;
 btn.onclick=()=>{extra.hidden=!extra.hidden;btn.setAttribute("aria-expanded",String(!extra.hidden));btn.textContent=d.inline[1]+(extra.hidden?" +":" −")};
 target.append(document.createTextNode(d.inline[0]),btn,extra,document.createTextNode(d.inline[3]));
}
function renderReader(){
 const d=data[memory];set("readerCrumb",d.title);set("readerKind","◇ "+d.kind);set("readerTitle",d.title);
 const badge=byId("readerState");badge.textContent=d.status==="historical"?"HISTORICAL":"CURRENT";
 badge.className="state-tag "+d.status;
 set("readerSources",d.sources===0?"No source available":d.sources+" source"+(d.sources!==1?"s":"")+" linked");
 set("readerDate",d.date);set("readerSourceType",d.sourceType);set("readerIdShort",d.id);
 makeInline(d);
 set("readerExplanation",d.explanation);
 const explanation=byId("readerExplanation");explanation.hidden=true;byId("readerExpand").setAttribute("aria-expanded","false");
 qsa("[data-disclosure]").forEach(b=>{b.setAttribute("aria-expanded","false");byId(b.dataset.disclosure).hidden=true;byId(b.dataset.disclosure).replaceChildren()});
}
function disclosureContents(which){
 const d=data[memory],target=byId(which);target.replaceChildren();
 if(which==="inspectorEvidence"){
  if(d.sources===0){target.append(node("p",null,"No source is available in this example. Historical records must not appear fully grounded without evidence."));return}
  for(let i=0;i<d.sources;i++){
   const card=node("div","source-card"),title=i===0?"Source episode receipt":"Architecture design note",short=i===0?"Original episode and its resolved reference remain distinct.":"A source document with a stable reference and excerpt.";
   card.append(node("strong",null,title),node("p",null,short));target.append(card);
  }
  const action=node("button",null,"Inspect full provenance →");action.onclick=()=>route("evidence");target.append(action);
 }else if(which==="inspectorHistory"){
  target.append(node("p",null,d.status==="historical"?"This view is superseded and is presented only for inspection.":"A prior version exists. The current view supersedes the earlier interpretation."));
  const action=node("button",null,"Compare history versions →");action.onclick=()=>route("timeline");target.append(action);
 }else if(which==="inspectorRelations"){
  d.relation.forEach(([id,label])=>{const card=node("div","source-card");card.append(node("strong",null,data[id].title),node("p",null,label));const link=node("button",null,"Follow →");link.onclick=()=>route("inspect",id);card.append(link);target.append(card)});
  const action=node("button",null,"Explore local graph →");action.onclick=()=>route("graph");target.append(action);
 }
}
function renderStage(){
 const d=stages[stage];set("stageType","◇ "+d.kind);set("stageState",d.state);set("stageTitle",d.title);set("stageDescription",d.description);set("stageQuote",d.quote);
 qsa("[data-stage]").forEach(b=>{b.classList.toggle("selected",b.dataset.stage===stage);b.setAttribute("aria-pressed",String(b.dataset.stage===stage))});
 const props=byId("stageProperties");props.replaceChildren();d.properties.forEach(([key,value])=>{const group=node("div");group.append(node("div","property-label",key),node("div","property-value",value));props.append(group)});
}
function renderTimeline(){
 const events=timeBasis==="world"?worldEvents:learnedEvents;
 if(timeIndex>=events.length)timeIndex=0;
 qsa("[data-time]").forEach(b=>{const active=b.dataset.time===timeBasis;b.classList.toggle("selected",active);b.setAttribute("aria-pressed",String(active))});
 const holder=byId("timelineEvents");holder.replaceChildren();
 events.forEach((ev,i)=>{
   const b=node("button","timeline-event"+(timeIndex===i?" active":""));b.type="button";b.setAttribute("aria-pressed",String(timeIndex===i));
   const body=node("div");body.append(node("small",null,ev.when),node("strong",null,ev.title),node("span",null,ev.detail));
   b.append(body);b.onclick=()=>{timeIndex=i;renderTimeline()};holder.append(b);
 });
 const current=events[timeIndex];
 set("timeDetailsCaption",timeBasis==="world"?"WORLD-TIME STATUS":"SELECTED MEMORY STATE");
 set("timeDetailsTitle",current.title);set("timeDetailsCopy",current.detail);
 set("timeDetailsTag",current.status==="current"?"CURRENT":"SUPERSEDED");
 byId("timeDetailsTag").className="state-tag "+current.status;
 set("timeDetailsMeta",current.when+" · "+(timeBasis==="world"?"not inferred from recorded_at":"observed in fixture"));
}
const graphNodes=[
 {id:"identity",label:"Stable identity",subtitle:"Current View",x:49,y:44,kind:"focal",info:"A current derived View, grounded in source records.",relation:"Current focal memory"},
 {id:"merge",label:"Merge strategy",subtitle:"Derived View",x:25,y:25,kind:"",info:"The canonical identity decision supported by the focal record.",relation:"SUPPORTS → current decision"},
 {id:"receipt",label:"Episode receipt",subtitle:"Source",x:77,y:27,kind:"episode",info:"An ingestion receipt linked to identity and enrichment provenance.",relation:"MENTIONED IN ← episode"},
 {id:"evidence",label:"Design note",subtitle:"Primary source",x:72,y:75,kind:"source",info:"An underlying architecture note that supports the derived rule.",relation:"SUPPORTED BY ← document"},
 {id:"legacy",label:"Prior approach",subtitle:"Historical",x:20,y:76,kind:"historical",info:"Superseded historical memory, visible only under an authorized audit policy.",relation:"SUPERSEDES → historical record"},
 {id:"policy",label:"Governance",subtitle:"Policy View",x:45,y:88,kind:"",info:"A governance decision: keep claim authority separate from source evidence.",relation:"GOVERNED BY ← policy"}
];
const lines=[["identity","merge"],["identity","receipt"],["identity","evidence"],["identity","legacy"],["merge","policy"],["legacy","policy"]];
const graphLookup=Object.fromEntries(graphNodes.map(x=>[x.id,x]));
function visibleNodes(){return graphNodes.filter(n=>n.id!=="policy"||depth===2).filter(n=>n.id!=="legacy"||historical)}
function selectGraph(id){graphFocus=id;const n=graphLookup[id];if(!n)return;set("graphFocusTitle",data[id]?.title||n.label);set("graphFocusCopy",n.info);set("graphRelationType",n.relation);set("graphFocusIcon",n.kind==="episode"?"◉":n.kind==="historical"?"◷":"◇");qsa(".graph-node").forEach(b=>b.classList.toggle("selected",b.dataset.node===id))}
function renderGraph(){
 qsa("[data-depth]").forEach(b=>{const selected=Number(b.dataset.depth)===depth;b.classList.toggle("selected",selected);b.setAttribute("aria-pressed",String(selected))});
 byId("showHistorical").checked=historical;
 let nodes=visibleNodes();if(!nodes.some(n=>n.id===graphFocus))graphFocus="identity";
 const shape=byId("graphLines");shape.replaceChildren();
 for(const [from,to] of lines){const a=nodes.find(n=>n.id===from),b=nodes.find(n=>n.id===to);if(!a||!b)continue;
  const l=document.createElementNS("http://www.w3.org/2000/svg","line");
  l.setAttribute("x1",String(a.x*7.4));l.setAttribute("y1",String(a.y*4.6));l.setAttribute("x2",String(b.x*7.4));l.setAttribute("y2",String(b.y*4.6));l.setAttribute("stroke",b.kind==="historical"?"#8c7ca9":"#5f92a3");l.setAttribute("stroke-width","1.55");l.setAttribute("opacity",".77");shape.append(l);
 }
 const group=byId("graphNodes");group.replaceChildren();
 for(const n of nodes){const b=node("button","graph-node "+n.kind+(graphFocus===n.id?" selected":""),n.label);b.type="button";b.dataset.node=n.id;b.style.left=n.x+"%";b.style.top=n.y+"%";b.append(node("small",null,n.subtitle));b.onclick=()=>selectGraph(n.id);group.append(b)}
 selectGraph(graphFocus);
}
function init(){
 qsa("[data-go]").forEach(b=>b.addEventListener("click",()=>route(b.dataset.go)));
 qsa("[data-memory]").forEach(b=>b.addEventListener("click",()=>route("inspect",b.dataset.memory)));
 qsa("[data-filter]").forEach(b=>b.addEventListener("click",()=>{filter=b.dataset.filter;renderBrowse()}));
 byId("mobilePage").addEventListener("change",e=>route(e.target.value));
 byId("readerExpand").addEventListener("click",e=>{const panel=byId("readerExplanation");panel.hidden=!panel.hidden;e.currentTarget.setAttribute("aria-expanded",String(!panel.hidden));qs("span",e.currentTarget).textContent=panel.hidden?"Expand explanation":"Collapse explanation"});
 qsa("[data-disclosure]").forEach(b=>b.addEventListener("click",()=>{const box=byId(b.dataset.disclosure),expand=box.hidden;box.hidden=!expand;b.setAttribute("aria-expanded",String(expand));if(expand)disclosureContents(b.dataset.disclosure)}));
 qsa("[data-stage]").forEach(b=>b.addEventListener("click",()=>{stage=b.dataset.stage;renderStage()}));
 qsa("[data-time]").forEach(b=>b.addEventListener("click",()=>{timeBasis=b.dataset.time;timeIndex=0;renderTimeline()}));
 qsa("[data-depth]").forEach(b=>b.addEventListener("click",()=>{depth=Number(b.dataset.depth);renderGraph()}));
 byId("showHistorical").addEventListener("change",e=>{historical=e.target.checked;renderGraph()});
 byId("resetGraph").addEventListener("click",()=>{graphFocus="identity";depth=1;historical=false;renderGraph()});
 byId("graphOpenMemory").addEventListener("click",()=>route("inspect",graphFocus));
 const searchInput=byId("globalSearch");searchInput.addEventListener("input",e=>{search=e.target.value.toLowerCase().trim();if(page!=="browse")route("browse");else renderBrowse()});
 document.addEventListener("keydown",e=>{if(e.key==="/"&&document.activeElement!==searchInput&&!["INPUT","TEXTAREA"].includes(document.activeElement?.tagName)){e.preventDefault();searchInput.focus()}if(e.key==="Escape"&&document.activeElement===searchInput)searchInput.blur()});
 byId("copyId").addEventListener("click",async e=>{const id=data[memory].id;try{await navigator.clipboard.writeText(id);e.currentTarget.textContent="✓ Copied";window.setTimeout(()=>e.currentTarget.textContent="⌁ Copy ID",1400)}catch{e.currentTarget.textContent=id}});
 window.addEventListener("hashchange",renderRoute);
 renderRoute();
}
init();
})();