"""Assemble the live tracker artifact, injecting the real backlog data."""

import json
import os

SC = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(SC, "tracker_data.json"), encoding="utf-8") as fh:
    data = json.load(fh)
data.setdefault("sit", [])  # an empty SIT log is a good day, not a missing key

payload = (
    json.dumps(data, ensure_ascii=False)
    .replace("<", "\\u003c")
    .replace(" ", "\\u2028")
    .replace(" ", "\\u2029")
)

HTML = r"""<title>Live Tracker — AI Algo Trading</title>
<style>
:root{
  --ground:#FAFAF8; --surface:#FFFFFF; --surface-2:#F3F4F2; --surface-3:#EAECE9;
  --line:#DDE0DC; --line-soft:#E9EBE7;
  --ink:#16191A; --ink-2:#43494A; --ink-3:#6B7273;
  --accent:#0E6F68; --accent-soft:#E2F0EE; --accent-ink:#0A544E; --accent-line:#8FC7C1; --on-accent:#FFFFFF;
  --done:#1F6F3D; --done-bg:#E3F1E7;
  --wip:#9A5B06;  --wip-bg:#F8EEDD;
  --todo:#5B6364; --todo-bg:#ECEEEB;
  --block:#A32C22;--block-bg:#F8E6E3;
  --mute:#8A9191; --mute-bg:#F0F1EF;
  --edit:#5B3FA8; --edit-bg:#EEE9F8;
  --shadow:0 1px 2px rgba(20,26,26,.06),0 6px 20px rgba(20,26,26,.07);
  --serif:Georgia,'Iowan Old Style','Palatino Linotype','Times New Roman',serif;
  --sans:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,'SF Mono','Cascadia Mono',Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#101314; --surface:#171B1C; --surface-2:#1D2223; --surface-3:#242A2B;
  --line:#2C3234; --line-soft:#232829;
  --ink:#E7EAE9; --ink-2:#B2BAB9; --ink-3:#838C8C;
  --accent:#4FC7BB; --accent-soft:#14312E; --accent-ink:#7FDBD1; --accent-line:#2A6963; --on-accent:#08211F;
  --done:#6FCB8C; --done-bg:#14301E;
  --wip:#E0A94B;  --wip-bg:#33260F;
  --todo:#9AA3A3; --todo-bg:#23292A;
  --block:#F0857A;--block-bg:#381916;
  --mute:#6E7676; --mute-bg:#1C2122;
  --edit:#B49BF0; --edit-bg:#241A3D;
  --shadow:0 1px 2px rgba(0,0,0,.45),0 6px 22px rgba(0,0,0,.35);
}}
:root[data-theme="dark"]{
  --ground:#101314; --surface:#171B1C; --surface-2:#1D2223; --surface-3:#242A2B;
  --line:#2C3234; --line-soft:#232829;
  --ink:#E7EAE9; --ink-2:#B2BAB9; --ink-3:#838C8C;
  --accent:#4FC7BB; --accent-soft:#14312E; --accent-ink:#7FDBD1; --accent-line:#2A6963; --on-accent:#08211F;
  --done:#6FCB8C; --done-bg:#14301E;
  --wip:#E0A94B;  --wip-bg:#33260F;
  --todo:#9AA3A3; --todo-bg:#23292A;
  --block:#F0857A;--block-bg:#381916;
  --mute:#6E7676; --mute-bg:#1C2122;
  --edit:#B49BF0; --edit-bg:#241A3D;
  --shadow:0 1px 2px rgba(0,0,0,.45),0 6px 22px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--ground);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
button,select,input,textarea{font-family:inherit;font-size:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

.shell{max-width:1420px;margin:0 auto;padding:0 18px 80px}

header.top{padding:26px 0 16px;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;border-bottom:2px solid var(--ink)}
.brand h1{font-family:var(--serif);font-weight:400;font-size:25px;margin:0;letter-spacing:-.01em}
.brand p{margin:3px 0 0;color:var(--ink-3);font-size:13px}
.top-actions{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.btn{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:7px 12px;font-size:13px;cursor:pointer;color:var(--ink-2);white-space:nowrap}
.btn:hover{border-color:var(--accent-line);color:var(--ink)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
.btn.danger:hover{border-color:var(--block);color:var(--block)}

.rollups{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:0;border-bottom:1px solid var(--line)}
.roll{padding:16px 18px 17px 0;border-right:1px solid var(--line-soft)}
.roll:last-child{border-right:0}
.roll .n{font-family:var(--mono);font-size:24px;font-variant-numeric:tabular-nums;letter-spacing:-.02em;display:block;line-height:1.1}
.roll .l{font-size:12px;color:var(--ink-3);margin-top:4px;display:block}
.roll.hi .n{color:var(--accent-ink)}

.meter{height:7px;border-radius:4px;background:var(--surface-3);overflow:hidden;display:flex;margin-top:9px}
.meter i{display:block;height:100%}
.meter i.d{background:var(--done)} .meter i.w{background:var(--wip)} .meter i.b{background:var(--block)}

nav.tabs{display:flex;gap:2px;margin:20px 0 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.tab{background:none;border:0;border-bottom:2px solid transparent;padding:9px 14px;cursor:pointer;color:var(--ink-3);font-size:13.5px;font-weight:550}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent)}
.tab:hover{color:var(--ink)}
.tab .ct{font-family:var(--mono);font-size:11px;color:var(--ink-3);margin-left:5px}

.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:14px 0 12px;position:sticky;top:0;background:var(--ground);z-index:20;border-bottom:1px solid var(--line-soft)}
.toolbar select,.toolbar input[type=search]{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:6px 9px;font-size:13px}
.toolbar input[type=search]{min-width:220px;flex:1 1 220px}
.toolbar label.chk{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--ink-2);cursor:pointer}
.count{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink-3);font-variant-numeric:tabular-nums}

.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface);margin-top:12px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{position:sticky;top:0;text-align:left;font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);
   padding:9px 11px;background:var(--surface-2);border-bottom:1px solid var(--line);white-space:nowrap;z-index:2}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--ink)}
th .ar{opacity:.45;font-size:9px}
td{padding:8px 11px;border-bottom:1px solid var(--line-soft);vertical-align:top}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--surface-2)}
tbody tr.sel{background:var(--accent-soft)}
tr:last-child td{border-bottom:0}
td.id{font-family:var(--mono);font-size:12.5px;color:var(--ink-3);white-space:nowrap}
td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
td.title{font-weight:500;min-width:220px}
td.wrap{color:var(--ink-3);font-size:12.5px;max-width:400px}
.edited{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--edit);margin-left:6px;vertical-align:middle}
.warn-dot{display:inline-block;font-size:12px;line-height:1;color:var(--wip);margin-left:5px;cursor:help}
.concern{border-left:3px solid var(--wip);background:var(--wip-bg);border-radius:0 5px 5px 0;padding:13px 15px;margin:0 0 18px;
  font-size:13.5px;line-height:1.62;color:var(--ink);white-space:pre-wrap}
.concern .ch{display:block;font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--wip);margin-bottom:6px}
.concern.closed{border-left-color:var(--done);background:var(--done-bg)}
.concern.closed .ch{color:var(--done)}

.chip{display:inline-block;font-family:var(--mono);font-size:10.5px;letter-spacing:.03em;padding:2px 7px;border-radius:3px;white-space:nowrap;font-variant-numeric:tabular-nums}
.s-done{color:var(--done);background:var(--done-bg)}
.s-wip{color:var(--wip);background:var(--wip-bg)}
.s-todo{color:var(--todo);background:var(--todo-bg)}
.s-block{color:var(--block);background:var(--block-bg)}
.s-mute{color:var(--mute);background:var(--mute-bg)}
.s-acc{color:var(--accent-ink);background:var(--accent-soft)}
.risk-hi{color:var(--block);font-weight:600}
.risk-md{color:var(--wip)}

.bar{display:inline-block;width:52px;height:5px;border-radius:3px;background:var(--surface-3);overflow:hidden;vertical-align:middle}
.bar i{display:block;height:100%;background:var(--accent)}

.epic-grid{display:grid;gap:9px;margin-top:14px}
.epic-row{display:grid;grid-template-columns:58px 1fr 108px 78px;gap:12px;align-items:center;
  background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:10px 13px;cursor:pointer}
.epic-row:hover{border-color:var(--accent-line)}
.epic-row .ec{font-family:var(--mono);font-size:12.5px;color:var(--accent-ink)}
.epic-row .en{font-weight:550;font-size:13.5px}
.epic-row .ep{font-size:11.5px;color:var(--ink-3);margin-top:1px}
.epic-row .em{font-family:var(--mono);font-size:11.5px;color:var(--ink-3);text-align:right;font-variant-numeric:tabular-nums}

.scrim{position:fixed;inset:0;background:rgba(10,14,14,.42);z-index:40;opacity:0;pointer-events:none;transition:opacity .16s}
.scrim.on{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(560px,100%);background:var(--surface);border-left:1px solid var(--line);
  z-index:50;transform:translateX(100%);transition:transform .2s ease;overflow-y:auto;box-shadow:var(--shadow)}
.drawer.on{transform:translateX(0)}
.dr-head{position:sticky;top:0;background:var(--surface);border-bottom:1px solid var(--line);padding:16px 20px;display:flex;gap:12px;align-items:flex-start;z-index:2}
.dr-head h3{margin:0;font-size:16px;font-weight:600;line-height:1.3}
.dr-head .sub{font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:3px}
.dr-close{margin-left:auto;background:none;border:0;font-size:22px;line-height:1;color:var(--ink-3);cursor:pointer;padding:0 4px}
.dr-close:hover{color:var(--ink)}
.dr-body{padding:18px 20px 40px}
.fld{margin-bottom:16px}
.fld label{display:block;font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);margin-bottom:5px}
.fld select,.fld input,.fld textarea{width:100%;background:var(--ground);border:1px solid var(--line);border-radius:5px;padding:7px 9px;font-size:13.5px}
.fld textarea{min-height:80px;resize:vertical;line-height:1.5}
.fld .ro{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:5px;padding:9px 11px;font-size:13px;color:var(--ink-2);white-space:pre-wrap}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:18px}
.meta-grid div{background:var(--surface-2);border-radius:5px;padding:8px 10px}
.meta-grid .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-3)}
.meta-grid .v{font-size:13px;margin-top:2px;font-weight:550}

.empty{padding:44px 20px;text-align:center;color:var(--ink-3);font-size:14px}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(10px);background:var(--ink);color:var(--ground);
  padding:10px 18px;border-radius:6px;font-size:13.5px;z-index:80;opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;max-width:90vw}
.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{background:var(--block);color:#fff}

.helpbar{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:0 6px 6px 0;padding:13px 16px;margin-top:14px;font-size:13.5px;color:var(--ink-2)}
.helpbar strong{color:var(--ink)}
@media(max-width:760px){
  .roll{border-right:0;border-bottom:1px solid var(--line-soft);padding-right:0}
  .epic-row{grid-template-columns:52px 1fr;row-gap:8px}
  .epic-row .em,.epic-row .bar{grid-column:2}
  .count{margin-left:0;width:100%}
}
</style>

<div class="shell">
  <header class="top">
    <div class="brand">
      <h1>Project Tracker</h1>
      <p>AI-Driven Algorithmic Trading Platform &middot; NSE/BSE</p>
    </div>
    <div class="top-actions">
      <button class="btn" id="themeBtn" type="button">Theme</button>
      <button class="btn" id="importBtn" type="button">Import</button>
      <button class="btn" id="csvBtn" type="button">Export CSV</button>
      <button class="btn primary" id="exportBtn" type="button">Export JSON</button>
      <input type="file" id="fileIn" accept=".json,application/json" hidden>
    </div>
  </header>

  <div class="rollups" id="rollups"></div>

  <nav class="tabs" role="tablist" id="tabs">
    <button class="tab" role="tab" data-view="stories"  aria-selected="true">Stories <span class="ct" id="ctStories"></span></button>
    <button class="tab" role="tab" data-view="epics"    aria-selected="false">Epics <span class="ct" id="ctEpics"></span></button>
    <button class="tab" role="tab" data-view="blockers" aria-selected="false">Blockers <span class="ct" id="ctBlockers"></span></button>
    <button class="tab" role="tab" data-view="qa"       aria-selected="false">QA results <span class="ct" id="ctQa"></span></button>
    <button class="tab" role="tab" data-view="security" aria-selected="false">Security <span class="ct" id="ctSec"></span></button>
    <button class="tab" role="tab" data-view="sit"      aria-selected="false">SIT defects <span class="ct" id="ctSit"></span></button>
  </nav>

  <div id="panel"></div>
</div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" aria-hidden="true" aria-label="Story detail"></aside>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script id="seed" type="application/json">__PAYLOAD__</script>
<script>
(function(){
"use strict";

var SEED = JSON.parse(document.getElementById('seed').textContent);
var KEY  = 'algotrader.tracker.v1';

var STATUSES = ['New','Business Review','Business Approved','Active','Development In-Progress',
  'Development Completed','QA In-Progress','QA-Success','QA-Fail','Closed','Removed',
  'Moved to Backlog','Blocked','On Hold'];

var GROUP = {
  'Closed':'done','QA-Success':'done',
  'Active':'wip','Development In-Progress':'wip','Development Completed':'wip',
  'QA In-Progress':'wip','Business Review':'wip','Business Approved':'wip',
  'New':'todo',
  'Blocked':'block','On Hold':'block','QA-Fail':'block',
  'Removed':'mute','Moved to Backlog':'mute'
};
function grp(s){ return GROUP[s] || 'todo'; }

/* A concern whose text opens with RESOLVED / PARTLY RESOLVED is history, not a
   live warning. It stays visible because the reasoning is worth keeping, but it
   must not be counted as outstanding or it drowns the ones that are. */
function concernIsOpen(text){
  if(!text) return false;
  return !/^\s*(RESOLVED|PARTLY RESOLVED)/i.test(text);
}

var overlay = {};
try { overlay = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch(e){ overlay = {}; }

function persist(){
  try { localStorage.setItem(KEY, JSON.stringify(overlay)); }
  catch(e){ toast('Could not save locally — storage may be full or blocked.', true); }
}
function merged(s){
  var o = overlay[s['Story ID']];
  if(!o) return s;
  var out = {}; for(var k in s) out[k] = s[k];
  for(var k2 in o) out[k2] = o[k2];
  return out;
}
function setField(id, field, value){
  var base = byId[id];
  if(!overlay[id]) overlay[id] = {};
  var baseVal = base[field] == null ? '' : String(base[field]);
  if(String(value) === baseVal){ delete overlay[id][field]; }
  else { overlay[id][field] = value; }
  if(Object.keys(overlay[id]).length === 0) delete overlay[id];
  persist();
}
function isEdited(id){ return !!overlay[id]; }

var byId = {};
SEED.stories.forEach(function(s){ byId[s['Story ID']] = s; });
function rows(){ return SEED.stories.map(merged); }

function el(tag, cls, txt){ var n=document.createElement(tag); if(cls)n.className=cls; if(txt!=null)n.textContent=txt; return n; }
function esc(v){ return v==null ? '' : String(v); }
function chip(text, g){ return el('span','chip s-'+g, text); }

var toastT;
function toast(msg, isErr){
  var t=document.getElementById('toast');
  t.textContent=msg; t.className='toast on'+(isErr?' err':'');
  clearTimeout(toastT); toastT=setTimeout(function(){ t.className='toast'+(isErr?' err':''); }, 3200);
}

var F = { q:'', epic:'', phase:'', status:'', priority:'', risk:'', type:'', changed:false, concern:false };
var SORT = { key:'Story ID', dir:1 };
var view = 'stories';
var selected = null;

function passes(s){
  if(F.epic && s.Epic !== F.epic) return false;
  if(F.phase && String(s.Phase) !== F.phase) return false;
  if(F.status){
    if(F.status.charAt(0)==='@'){ if(grp(s.Status) !== F.status.slice(1)) return false; }
    else if(s.Status !== F.status) return false;
  }
  if(F.priority && s.Priority !== F.priority) return false;
  if(F.risk && s.Risk !== F.risk) return false;
  if(F.type && s.Type !== F.type) return false;
  if(F.changed && !isEdited(s['Story ID'])) return false;
  if(F.concern && !concernIsOpen(s['Build Concerns'])) return false;
  if(F.q){
    var hay = [s['Story ID'],s['Story Title'],s['Epic Name'],s['User Story'],s.Tasks,
               s['Acceptance Criteria'],s.Notes,s.Comments,s.Dependencies,
               s['Build Concerns']].join(' ').toLowerCase();
    if(hay.indexOf(F.q.toLowerCase()) === -1) return false;
  }
  return true;
}
function filtered(){
  var out = rows().filter(passes);
  var k = SORT.key;
  out.sort(function(a,b){
    var x=a[k], y=b[k];
    if(k==='Est. Days'||k==='% Complete'||k==='Task Count'||k==='Phase'){
      x = parseFloat(x)||0; y = parseFloat(y)||0;
    } else { x = esc(x).toLowerCase(); y = esc(y).toLowerCase(); }
    return x<y ? -SORT.dir : x>y ? SORT.dir : 0;
  });
  return out;
}

function stats(list){
  var st = {n:list.length, days:0, done:0, wip:0, block:0, todo:0, mute:0, doneDays:0};
  list.forEach(function(s){
    var d = parseFloat(s['Est. Days'])||0;
    st.days += d;
    var g = grp(s.Status);
    st[g] = (st[g]||0)+1;
    if(g==='done') st.doneDays += d;
  });
  return st;
}
function renderRollups(){
  var all = rows(), st = stats(all);
  var active = st.n - st.mute;
  var pct = active ? Math.round(st.done/active*100) : 0;
  var host = document.getElementById('rollups');
  host.innerHTML='';

  function card(n,l,hi,extra){
    var d=el('div','roll'+(hi?' hi':''));
    d.appendChild(el('span','n',n));
    d.appendChild(el('span','l',l));
    if(extra) d.appendChild(extra);
    return d;
  }
  var meter=el('div','meter');
  function seg(cls,count){ if(!count) return; var i=el('i',cls); i.style.width=(count/active*100)+'%'; meter.appendChild(i); }
  seg('d',st.done); seg('w',st.wip); seg('b',st.block);

  var openConcerns = all.filter(function(s){
    return concernIsOpen(s['Build Concerns']) && grp(s.Status)!=='done';
  }).length;

  host.appendChild(card(pct+'%','Stories closed',true,meter));
  host.appendChild(card(st.done+' / '+active,'Closed of active'));
  host.appendChild(card(String(st.wip),'In flight'));
  host.appendChild(card(String(st.block),'Blocked / on hold'));
  host.appendChild(card(st.doneDays.toFixed(1)+' / '+st.days.toFixed(1),'Days delivered'));
  host.appendChild(card(String(openConcerns),'Open concerns'));
  host.appendChild(card(String(Object.keys(overlay).length),'Locally edited'));
}

function distinct(field){
  var seen={}, out=[];
  SEED.stories.forEach(function(s){ var v=s[field]; if(v!=null && v!=='' && !seen[v]){seen[v]=1; out.push(String(v));} });
  return out.sort();
}

function buildToolbar(){
  var bar = el('div','toolbar');
  var q = el('input'); q.type='search'; q.placeholder='Search id, title, tasks, acceptance, notes, concerns…'; q.value=F.q;
  q.setAttribute('aria-label','Search stories');
  q.addEventListener('input', function(){ F.q=q.value; renderPanel(true); });
  bar.appendChild(q);

  function sel(label, opts, cur, onch){
    var s=el('select'); s.setAttribute('aria-label',label);
    var o0=el('option','', label); o0.value=''; s.appendChild(o0);
    opts.forEach(function(v){
      var o=el('option','', typeof v==='string'?v:v.label);
      o.value = typeof v==='string'?v:v.value;
      if(o.value===cur) o.selected=true;
      s.appendChild(o);
    });
    s.addEventListener('change', function(){ onch(s.value); renderPanel(true); });
    return s;
  }
  bar.appendChild(sel('Epic', distinct('Epic'), F.epic, function(v){F.epic=v;}));
  bar.appendChild(sel('Phase', distinct('Phase'), F.phase, function(v){F.phase=v;}));
  bar.appendChild(sel('Status',
     [{value:'@done',label:'— Done'},{value:'@wip',label:'— In flight'},
      {value:'@todo',label:'— Not started'},{value:'@block',label:'— Blocked'}].concat(distinct('Status')),
     F.status, function(v){F.status=v;}));
  bar.appendChild(sel('Priority', distinct('Priority'), F.priority, function(v){F.priority=v;}));
  bar.appendChild(sel('Risk', distinct('Risk'), F.risk, function(v){F.risk=v;}));
  bar.appendChild(sel('Type', distinct('Type'), F.type, function(v){F.type=v;}));

  var lab=el('label','chk');
  var cb=el('input'); cb.type='checkbox'; cb.checked=F.changed;
  cb.addEventListener('change', function(){ F.changed=cb.checked; renderPanel(true); });
  lab.appendChild(cb); lab.appendChild(document.createTextNode('Edited only'));
  bar.appendChild(lab);

  var lab2=el('label','chk');
  var cb2=el('input'); cb2.type='checkbox'; cb2.checked=F.concern;
  cb2.addEventListener('change', function(){ F.concern=cb2.checked; renderPanel(true); });
  lab2.appendChild(cb2); lab2.appendChild(document.createTextNode('Open concerns'));
  bar.appendChild(lab2);

  var clr = el('button','btn','Clear');
  clr.type='button';
  clr.addEventListener('click', function(){
    F={q:'',epic:'',phase:'',status:'',priority:'',risk:'',type:'',changed:false,concern:false}; renderPanel();
  });
  bar.appendChild(clr);

  var c=el('span','count'); c.id='resultCount';
  bar.appendChild(c);
  return bar;
}

function viewStories(host){
  host.appendChild(buildToolbar());
  var list = filtered();
  document.getElementById('resultCount').textContent =
     list.length + ' of ' + SEED.stories.length + ' stories · ' +
     stats(list).days.toFixed(1) + ' days';

  if(!list.length){ host.appendChild(el('div','empty','No stories match these filters.')); return; }

  var wrap=el('div','scroll'), t=el('table');
  var cols=[['Story ID','id'],['Story Title','title'],['Epic','id'],['Phase','num'],
            ['Status',''],['Priority',''],['Risk',''],['Est. Days','num'],['% Complete','num']];
  var thead=el('thead'), tr=el('tr');
  cols.forEach(function(c){
    var th=el('th','sortable', c[0]==='% Complete'?'Progress':c[0]);
    if(SORT.key===c[0]) th.appendChild(el('span','ar', SORT.dir>0?' ▲':' ▼'));
    th.addEventListener('click', function(){
      if(SORT.key===c[0]) SORT.dir*=-1; else {SORT.key=c[0]; SORT.dir=1;}
      renderPanel(true);
    });
    tr.appendChild(th);
  });
  thead.appendChild(tr); t.appendChild(thead);

  var tb=el('tbody');
  list.forEach(function(s){
    var r=el('tr');
    if(selected===s['Story ID']) r.className='sel';
    r.addEventListener('click', function(){ openDrawer(s['Story ID']); });

    var c1=el('td','id'); c1.appendChild(document.createTextNode(s['Story ID']));
    if(isEdited(s['Story ID'])){ var d=el('span','edited'); d.title='Locally edited'; c1.appendChild(d); }
    if(concernIsOpen(s['Build Concerns'])){
      var w=el('span','warn-dot','▲'); w.title='Open build concern — click to read it'; c1.appendChild(w);
    }
    r.appendChild(c1);

    r.appendChild(el('td','title', s['Story Title']));
    r.appendChild(el('td','id', s.Epic));
    r.appendChild(el('td','num', esc(s.Phase)));

    var cs=el('td'); cs.appendChild(chip(s.Status, grp(s.Status))); r.appendChild(cs);
    r.appendChild(el('td','', esc(s.Priority)));

    var cr=el('td','');
    var rk=esc(s.Risk);
    cr.appendChild(el('span', rk==='Safety-critical'?'risk-hi':(rk==='Correctness-critical'?'risk-md':''), rk));
    r.appendChild(cr);

    r.appendChild(el('td','num', s['Est. Days']==null?'—':String(s['Est. Days'])));

    var cp=el('td','num');
    var pv = s['% Complete'];
    var pn = pv==null||pv==='' ? (grp(s.Status)==='done'?1:0) : parseFloat(pv);
    var bar=el('span','bar'); var fill=el('i'); fill.style.width=Math.round(pn*100)+'%'; bar.appendChild(fill);
    cp.appendChild(bar);
    cp.appendChild(document.createTextNode(' '+Math.round(pn*100)+'%'));
    r.appendChild(cp);

    tb.appendChild(r);
  });
  t.appendChild(tb); wrap.appendChild(t); host.appendChild(wrap);
}

function viewEpics(host){
  var groups={}, order=[];
  rows().forEach(function(s){
    if(!groups[s.Epic]){ groups[s.Epic]={code:s.Epic,name:s['Epic Name'],phase:s.Phase,list:[]}; order.push(s.Epic); }
    groups[s.Epic].list.push(s);
  });
  var note=el('div','helpbar');
  note.innerHTML='<strong>Click an epic</strong> to jump to its stories. Phases are a dependency sequence — each needs the one before it.';
  host.appendChild(note);

  var grid=el('div','epic-grid');
  order.forEach(function(k){
    var g=groups[k], st=stats(g.list);
    var active = st.n - st.mute;
    var pct = active ? Math.round(st.done/active*100) : 0;

    var row=el('div','epic-row');
    row.setAttribute('role','button'); row.tabIndex=0;
    function go(){ F={q:'',epic:g.code,phase:'',status:'',priority:'',risk:'',type:'',changed:false,concern:false}; switchView('stories'); }
    row.addEventListener('click', go);
    row.addEventListener('keydown', function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); go(); } });

    row.appendChild(el('div','ec', g.code));
    var mid=el('div');
    mid.appendChild(el('div','en', g.name));
    mid.appendChild(el('div','ep','Phase '+esc(g.phase)+' · '+st.n+' stories · '+st.days.toFixed(1)+' days'));
    row.appendChild(mid);

    var meter=el('div','meter');
    function seg(cls,c){ if(!c) return; var i=el('i',cls); i.style.width=(c/active*100)+'%'; meter.appendChild(i); }
    seg('d',st.done); seg('w',st.wip); seg('b',st.block);
    row.appendChild(meter);

    row.appendChild(el('div','em', pct+'%'));
    grid.appendChild(row);
  });
  host.appendChild(grid);
}

function simpleTable(host, list, cols, intro){
  if(intro){ var n=el('div','helpbar'); n.innerHTML=intro; host.appendChild(n); }
  if(!list.length){ host.appendChild(el('div','empty','Nothing recorded yet.')); return; }
  var wrap=el('div','scroll'), t=el('table'), thead=el('thead'), tr=el('tr');
  cols.forEach(function(c){ tr.appendChild(el('th','',c.label)); });
  thead.appendChild(tr); t.appendChild(thead);
  var tb=el('tbody');
  list.forEach(function(r){
    var row=el('tr'); row.style.cursor='default';
    cols.forEach(function(c){
      var td=el('td', c.cls||'');
      var v=r[c.key];
      if(c.chip){ if(v) td.appendChild(chip(String(v), c.chip(v))); }
      else td.textContent = v==null?'—':String(v);
      row.appendChild(td);
    });
    tb.appendChild(row);
  });
  t.appendChild(tb); wrap.appendChild(t); host.appendChild(wrap);
}

function sevGroup(v){
  v=String(v).toUpperCase();
  if(v==='HIGH') return 'block';
  if(v==='MEDIUM') return 'wip';
  if(v==='LOW') return 'todo';
  return 'mute';
}
function stateGroup(v){
  v=String(v).toLowerCase();
  if(v.indexOf('closed')>-1||v.indexOf('fixed')>-1) return 'done';
  if(v.indexOf('active')>-1) return 'block';
  if(v.indexOf('hold')>-1) return 'wip';
  return 'todo';
}
function qaGroup(v){
  v=String(v).toUpperCase();
  if(v.indexOf('FIXED')>-1) return 'wip';
  if(v.indexOf('PASS')>-1) return 'done';
  if(v.indexOf('FAIL')>-1) return 'block';
  return 'todo';
}

function renderPanel(keepScroll){
  var y = keepScroll ? window.scrollY : 0;
  var host=document.getElementById('panel');
  host.innerHTML='';
  if(view==='stories') viewStories(host);
  else if(view==='epics') viewEpics(host);
  else if(view==='blockers') simpleTable(host, SEED.blockers,
      [{key:'ID',label:'ID',cls:'id'},{key:'Blocker',label:'Blocker',cls:'title'},
       {key:'Blocks',label:'Blocks',cls:'wrap'},{key:'Status',label:'Status',chip:stateGroup},
       {key:'Action Required',label:'Action required',cls:'wrap'},
       {key:'Resolution Notes',label:'Resolution notes',cls:'wrap'}],
      '<strong>Cross-cutting blockers.</strong> None of these are code — they are procurement, paperwork, or a third party. They gate multiple epics.');
  else if(view==='qa') simpleTable(host, SEED.qa,
      [{key:'QA ID',label:'ID',cls:'id'},{key:'Story',label:'Story',cls:'id'},
       {key:'Area',label:'Area',cls:'title'},{key:'Result',label:'Result',chip:qaGroup},
       {key:'What was verified',label:'What was verified',cls:'wrap'},
       {key:'Evidence',label:'Evidence',cls:'wrap'},{key:'Test file',label:'Test file',cls:'id'}],
      '<strong>End-to-end QA.</strong> Every row was found by running something, not by reading code. “FIXED” means QA found a real defect and it was repaired.');
  else if(view==='security') simpleTable(host, SEED.security,
      [{key:'ID',label:'ID',cls:'id'},{key:'Severity',label:'Severity',chip:sevGroup},
       {key:'Component',label:'Component',cls:'title'},{key:'Status',label:'Status',chip:stateGroup},
       {key:'Vulnerability',label:'Vulnerability',cls:'wrap'},{key:'Impact',label:'Impact',cls:'wrap'},
       {key:'Fix',label:'Fix',cls:'wrap'},{key:'Regression test',label:'Regression test',cls:'wrap'}],
      '<strong>Penetration-test findings.</strong> Found by attacking the code rather than reviewing it. Every one carries a regression test so it cannot come back.');
  else if(view==='sit') simpleTable(host, SEED.sit,
      [{key:'SIT ID',label:'ID',cls:'id'},{key:'Story',label:'Story',cls:'id'},
       {key:'Severity',label:'Severity',chip:sevGroup},
       {key:'Component',label:'Component',cls:'title'},{key:'Status',label:'Status',chip:stateGroup},
       {key:'What happened',label:'What happened',cls:'wrap'},
       {key:'Why component tests missed it',label:'Why component tests missed it',cls:'wrap'},
       {key:'Root cause',label:'Root cause',cls:'wrap'},{key:'Fix',label:'Fix',cls:'wrap'},
       {key:'Regression test',label:'Regression test',cls:'wrap'}],
      '<strong>System integration testing.</strong> Raised on the QA branch, after component and integration QA passed \u2014 so every row is by definition something no component test could see. The \u201cwhy component tests missed it\u201d column is the process finding.');
  renderRollups();
  updateCounts();
  if(keepScroll) window.scrollTo(0,y);
}

function updateCounts(){
  document.getElementById('ctStories').textContent = SEED.stories.length;
  var eset={}; SEED.stories.forEach(function(s){eset[s.Epic]=1;});
  document.getElementById('ctEpics').textContent = Object.keys(eset).length;
  document.getElementById('ctBlockers').textContent = SEED.blockers.length;
  document.getElementById('ctQa').textContent = SEED.qa.length;
  document.getElementById('ctSec').textContent = SEED.security.length;
  document.getElementById('ctSit').textContent = SEED.sit.length;
}

function switchView(v){
  view=v;
  [].forEach.call(document.querySelectorAll('.tab'), function(b){
    b.setAttribute('aria-selected', b.dataset.view===v ? 'true':'false');
  });
  renderPanel();
}

function openDrawer(id){
  selected=id;
  var s = merged(byId[id]);
  var d = document.getElementById('drawer');
  d.innerHTML='';

  var head=el('div','dr-head');
  var htxt=el('div');
  htxt.appendChild(el('h3','', s['Story Title']));
  htxt.appendChild(el('div','sub', s['Story ID']+' · '+s.Epic+' '+s['Epic Name']));
  head.appendChild(htxt);
  var x=el('button','dr-close','×'); x.type='button'; x.setAttribute('aria-label','Close');
  x.addEventListener('click', closeDrawer); head.appendChild(x);
  d.appendChild(head);

  var body=el('div','dr-body');

  if(s['Build Concerns']){
    var open = concernIsOpen(s['Build Concerns']);
    var cn=el('div','concern'+(open?'':' closed'));
    cn.appendChild(el('span','ch', open?'Consider before building':'Concern — resolved'));
    cn.appendChild(document.createTextNode(s['Build Concerns']));
    body.appendChild(cn);
  }

  var mg=el('div','meta-grid');
  [['Phase',s.Phase],['Priority',s.Priority],['Risk',s.Risk],['Type',s.Type],
   ['Est. days',s['Est. Days']],['Tasks',s['Task Count']]].forEach(function(p){
    var c=el('div'); c.appendChild(el('div','k',p[0])); c.appendChild(el('div','v', p[1]==null?'—':String(p[1]))); mg.appendChild(c);
  });
  body.appendChild(mg);

  function field(label, node){ var f=el('div','fld'); f.appendChild(el('label','',label)); f.appendChild(node); return f; }
  function ro(label, val){
    if(val==null||val==='') return null;
    var f=el('div','fld'); f.appendChild(el('label','',label));
    f.appendChild(el('div','ro', String(val))); return f;
  }

  var stSel=el('select');
  STATUSES.forEach(function(v){ var o=el('option','',v); o.value=v; if(v===s.Status)o.selected=true; stSel.appendChild(o); });
  stSel.addEventListener('change', function(){
    setField(id,'Status',stSel.value);
    if(GROUP[stSel.value]==='done' && (s['% Complete']==null||s['% Complete']===''||parseFloat(s['% Complete'])<1)){
      setField(id,'% Complete',1); pct.value=100;
    }
    renderPanel(true); openDrawer(id); toast('Status → '+stSel.value);
  });
  body.appendChild(field('Status', stSel));

  var two=el('div','row2');
  var pct=el('input'); pct.type='number'; pct.min='0'; pct.max='100'; pct.step='5';
  var pv=s['% Complete']; pct.value = pv==null||pv===''?'':Math.round(parseFloat(pv)*100);
  pct.addEventListener('change', function(){
    var n=pct.value===''?'':Math.max(0,Math.min(100,parseFloat(pct.value)||0))/100;
    setField(id,'% Complete',n); renderPanel(true); toast('Progress updated');
  });
  var pf=el('div','fld'); pf.appendChild(el('label','','% Complete')); pf.appendChild(pct); two.appendChild(pf);

  var spr=el('input'); spr.type='text'; spr.value=esc(s.Sprint); spr.placeholder='e.g. Sprint 2';
  spr.addEventListener('change', function(){ setField(id,'Sprint',spr.value); toast('Sprint updated'); });
  var sf=el('div','fld'); sf.appendChild(el('label','','Sprint')); sf.appendChild(spr); two.appendChild(sf);
  body.appendChild(two);

  var two2=el('div','row2');
  ['Start Date','Target Date'].forEach(function(k){
    var i=el('input'); i.type='date'; i.value=esc(s[k]).slice(0,10);
    i.addEventListener('change', function(){ setField(id,k,i.value); toast(k+' set'); });
    var f=el('div','fld'); f.appendChild(el('label','',k)); f.appendChild(i); two2.appendChild(f);
  });
  body.appendChild(two2);

  var own=el('input'); own.type='text'; own.value=esc(s.Owner); own.placeholder='Unassigned';
  own.addEventListener('change', function(){ setField(id,'Owner',own.value); toast('Owner updated'); });
  body.appendChild(field('Owner', own));

  var blk=el('input'); blk.type='text'; blk.value=esc(s['Blocked By']); blk.placeholder='e.g. B6 (static IP)';
  blk.addEventListener('change', function(){ setField(id,'Blocked By',blk.value); toast('Blocker noted'); });
  body.appendChild(field('Blocked by', blk));

  var nt=el('textarea'); nt.value=esc(s.Notes); nt.placeholder='Findings, decisions, why this is where it is…';
  nt.addEventListener('change', function(){ setField(id,'Notes',nt.value); renderPanel(true); toast('Notes saved'); });
  body.appendChild(field('Notes', nt));

  var cm=el('textarea'); cm.value=esc(s.Comments); cm.placeholder='Running commentary…';
  cm.addEventListener('change', function(){ setField(id,'Comments',cm.value); renderPanel(true); toast('Comment saved'); });
  body.appendChild(field('Comments', cm));

  [['User story',s['User Story']],['Tasks',s.Tasks],['Acceptance criteria',s['Acceptance Criteria']],
   ['Dependencies',s.Dependencies]].forEach(function(p){
    var n=ro(p[0],p[1]); if(n) body.appendChild(n);
  });

  if(isEdited(id)){
    var rev=el('button','btn danger','Revert this story to baseline');
    rev.type='button';
    rev.addEventListener('click', function(){
      delete overlay[id]; persist(); renderPanel(true); openDrawer(id); toast('Reverted to baseline');
    });
    body.appendChild(rev);
  }

  d.appendChild(body);
  d.classList.add('on'); d.setAttribute('aria-hidden','false');
  document.getElementById('scrim').classList.add('on');
  renderPanel(true);
}
function closeDrawer(){
  selected=null;
  document.getElementById('drawer').classList.remove('on');
  document.getElementById('drawer').setAttribute('aria-hidden','true');
  document.getElementById('scrim').classList.remove('on');
  renderPanel(true);
}

function currentState(){
  return {
    exportedAt: new Date().toISOString(),
    schema: 'algotrader.tracker.v1',
    note: 'Overlay holds only fields changed from the baseline. Import this file to restore.',
    overlay: overlay,
    stories: rows()
  };
}
function saveFile(filename, text, okMsg){
  if(!window.claude || !window.claude.downloads){
    toast('Downloads are not available in this view.', true); return;
  }
  window.claude.downloads.save({filename:filename, data:text}).then(function(){
    toast(okMsg);
  }).catch(function(err){
    var code = err && err.code;
    if(code==='declined') return;
    if(code==='rate_limited'){ toast('A save prompt is already open. Try again in a moment.', true); return; }
    if(code==='extension_not_enabled'){ toast('CSV is not enabled here — use Export JSON instead.', true); return; }
    if(code==='too_large'){ toast('That file is over the 16 MB limit.', true); return; }
    if(code==='bad_request'){ toast('Could not build that file.', true); return; }
    toast('Saving is unavailable in this view.', true);
  });
}
function toCSV(list){
  var cols=['Story ID','Epic','Epic Name','Story Title','Status','Priority','Phase','Risk','Type',
            'Est. Days','Dependencies','Blocked By','Owner','Sprint','Start Date','Target Date',
            '% Complete','Task Count','Notes','Comments','Build Concerns'];
  function q(v){ v = v==null?'':String(v); return '"'+v.replace(/"/g,'""')+'"'; }
  var out=[cols.map(q).join(',')];
  list.forEach(function(s){ out.push(cols.map(function(c){return q(s[c]);}).join(',')); });
  return '﻿'+out.join('\r\n');
}

document.getElementById('exportBtn').addEventListener('click', function(){
  saveFile('algotrader-tracker.json', JSON.stringify(currentState(), null, 2),
           'Exported — re-import this file to restore your edits.');
});
document.getElementById('csvBtn').addEventListener('click', function(){
  saveFile('algotrader-tracker.csv', toCSV(rows()), 'CSV exported — opens in Excel.');
});
document.getElementById('importBtn').addEventListener('click', function(){
  document.getElementById('fileIn').click();
});
document.getElementById('fileIn').addEventListener('change', function(e){
  var f=e.target.files && e.target.files[0]; if(!f) return;
  var r=new FileReader();
  r.onload=function(){
    try{
      var parsed=JSON.parse(r.result);
      var inc = parsed && parsed.overlay;
      if(!inc || typeof inc!=='object') throw new Error('no overlay');
      var known=0;
      for(var k in inc){ if(byId[k]) known++; }
      if(!known) throw new Error('no matching stories');
      overlay = inc; persist(); renderPanel(); closeDrawer();
      toast('Imported edits for '+known+' stor'+(known===1?'y':'ies')+'.');
    }catch(err){
      toast('That file is not a tracker export — expected a JSON file with an "overlay" key.', true);
    }
  };
  r.onerror=function(){ toast('Could not read that file.', true); };
  r.readAsText(f);
  e.target.value='';
});

document.getElementById('themeBtn').addEventListener('click', function(){
  var root=document.documentElement;
  var cur=root.getAttribute('data-theme');
  var next = cur==='dark' ? 'light' : cur==='light' ? null : 'dark';
  if(next) root.setAttribute('data-theme', next); else root.removeAttribute('data-theme');
  toast('Theme: '+(next||'system'));
});

document.getElementById('tabs').addEventListener('click', function(e){
  var b=e.target.closest('.tab'); if(b) switchView(b.dataset.view);
});
document.getElementById('scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', function(e){ if(e.key==='Escape') closeDrawer(); });

switchView('stories');
})();
</script>
"""

out = HTML.replace("__PAYLOAD__", payload)
path = os.path.join(SC, "tracker.html")
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(out)
print("written:", path, len(out), "bytes")
