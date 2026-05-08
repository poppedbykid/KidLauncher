const BG={fabric:'linear-gradient(135deg,#0d1b4b,#1a0d3d)',vanilla:'linear-gradient(135deg,#0d2a1a,#1a2a0d)',quilt:'linear-gradient(135deg,#2a1a0d,#3d2a0d)'};
let instances=JSON.parse(localStorage.getItem('ki')||'[]'),current=null,allMods=[];

const es=new EventSource('/events');
es.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.type==='log')log(m.data);else if(m.type==='progress')prog(m.data);else if(m.type==='mods')renderMods(m.data.mods);else onEv(m);}catch(_){}};

async function api(r,b){try{return await(await fetch('/api'+r,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();}catch(e){log('[ERR]'+e);}}

function nav(v){
  document.querySelectorAll('.view').forEach(m=>{m.classList.remove('active');m.classList.add('hidden');});
  const el=document.getElementById('view-'+v);if(el){el.classList.remove('hidden');el.classList.add('active');}
  document.querySelectorAll('.nb').forEach(n=>n.classList.remove('active'));
  const ni=document.getElementById('n-'+v);if(ni)ni.classList.add('active');
  if(v==='settings')loadSettings();
}

function renderInstances(){
  const g=document.getElementById('inst-strip');
  g.innerHTML=instances.map((inst,i)=>`
    <div class="inst-thumb${current&&current.id===inst.id?' sel':''}" onclick="selInst(${i})">
      <div class="inst-thumb-bg" style="background:${BG[inst.loader]||BG.vanilla}"></div>
      <div class="inst-thumb-overlay">
        <div class="inst-thumb-name">${inst.name}</div>
        <div class="inst-thumb-ver">${inst.version} · ${inst.loader}</div>
      </div>
    </div>`).join('')+
  `<div class="add-thumb" onclick="openModal()">
    <svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
    New Instance
  </div>`;
}

function selInst(i){
  current=instances[i];
  document.getElementById('inst-title').innerText=current.name;
  document.getElementById('inst-sub').innerHTML=`<span class="badge b-${current.loader}">${current.loader}</span> ${current.version}`;
  document.getElementById('home-bg').style.backgroundImage=`url('/ui/bg.png')`;
  document.getElementById('sel-lbl').innerText=current.name;
  document.getElementById('sel-meta').innerText=current.version+' · '+current.loader;
  document.getElementById('mods-pg-title').innerText=current.name;
  renderInstances();
  api('/list_mods',{instance:current.name});
}

function openModal(){document.getElementById('modal-bg').classList.add('open');setTimeout(()=>document.getElementById('inst-name').focus(),50);}
function closeModal(){document.getElementById('modal-bg').classList.remove('open');}
document.getElementById('modal-bg').onclick=e=>{if(e.target.id==='modal-bg')closeModal();};

function createInstance(){
  const name=document.getElementById('inst-name').value.trim();if(!name)return;
  const version=document.getElementById('inst-version').value,loader=document.getElementById('inst-loader').value;
  const inst={name,version,loader,id:Date.now()};
  instances.push(inst);localStorage.setItem('ki',JSON.stringify(instances));
  closeModal();renderInstances();api('/add_instance',inst);
  log('[KID] Created: '+name);selInst(instances.length-1);
  document.getElementById('inst-name').value='';
}

function renderMods(mods){
  allMods=mods||[];showMods(allMods);
}
function showMods(mods){
  const el=document.getElementById('mod-list');
  if(!mods.length){el.innerHTML='<div style="color:var(--dim);text-align:center;padding:24px;font-size:12px;">No mods — drop .jar files above</div>';return;}
  el.innerHTML=mods.map(f=>{
    const dis=f.endsWith('.disabled'),n=(dis?f.replace('.jar.disabled',''):f.replace('.jar','')).replace(/-[\d.]+.*/,'');
    return`<div class="mod-row${dis?' dis':''}">
      <div class="mod-ico">🧩</div>
      <div class="mod-info"><div class="mod-n">${n}</div><div class="mod-s">${dis?'disabled':'enabled'}</div></div>
      <div style="display:flex;gap:4px;">
        <button class="btn btn-xs btn-ghost" onclick="api('/toggle_mod',{instance:current.name,filename:'${f}'})">${dis?'▶':'⏸'}</button>
        <button class="btn btn-xs btn-danger" onclick="if(confirm('Delete?'))api('/remove_mod',{instance:current.name,filename:'${f}'})">✕</button>
      </div>
    </div>`;
  }).join('');
}
document.getElementById('mod-filter').oninput=function(){showMods(allMods.filter(m=>m.toLowerCase().includes(this.value.toLowerCase())));};

const dz=document.getElementById('drop-z');
dz.ondragover=e=>{e.preventDefault();dz.classList.add('ov');};
dz.ondragleave=()=>dz.classList.remove('ov');
dz.ondrop=e=>{e.preventDefault();dz.classList.remove('ov');upload(e.dataTransfer.files);};
dz.onclick=()=>document.getElementById('file-in').click();
document.getElementById('file-in').onchange=function(){upload(this.files);};

function upload(files){
  if(!current){alert('Select an instance first!');return;}
  const jars=Array.from(files).filter(f=>f.name.endsWith('.jar'));if(!jars.length)return;
  const form=new FormData();form.append('instance',current.name);jars.forEach(f=>form.append('mods',f));
  status('Uploading '+jars.length+' mod(s)...');
  fetch('/api/upload_mods',{method:'POST',body:form}).then(r=>r.json()).then(d=>{if(d.ok)status('Uploaded '+d.injected.length+' mod(s)');});
}

function launchCurrent(){
  if(!current){alert('Select an instance first!');return;}
  const user=document.getElementById('username').value||'Player';
  const ram=parseInt(document.getElementById('ram-slider').value)||4;
  status('Launching '+current.name+'...');
  document.getElementById('play-btn').style.opacity='.5';
  document.getElementById('launch-btn-big').style.opacity='.5';
  api('/launch',{username:user,version:current.version,ram,instance:current.name,loader:current.loader.toUpperCase()});
}

async function doSearch(){
  const q=document.getElementById('srch-in').value.trim();if(!q)return;
  const el=document.getElementById('srch-res');el.innerHTML='<div style="color:var(--dim);padding:20px 0;font-size:12px;">Searching...</div>';
  const r=await fetch(`https://api.modrinth.com/v2/search?query=${encodeURIComponent(q)}&facets=[["project_type:mod"]]&limit=20`).then(x=>x.json()).catch(()=>({hits:[]}));
  if(!r.hits||!r.hits.length){el.innerHTML='<div style="color:var(--dim);font-size:12px;">No results.</div>';return;}
  el.innerHTML=r.hits.map(h=>`
    <div class="mr-card">
      <img class="mr-icon" src="${h.icon_url||''}" onerror="this.style.display='none'">
      <div class="mr-body">
        <div class="mr-title">${h.title}</div>
        <div class="mr-desc">${h.description||''}</div>
        <div style="margin-top:6px;">${(h.categories||[]).slice(0,4).map(c=>`<span class="tag">${c}</span>`).join('')}</div>
      </div>
      <div class="mr-side">
        <select id="s-${h.project_id}" style="font-size:11px;width:120px;">${instances.map(i=>`<option>${i.name}</option>`).join('')||'<option>—</option>'}</select>
        <button class="btn btn-primary btn-sm" onclick="instMod('${h.project_id}','${h.title.replace(/'/g,'')}')">Install</button>
      </div>
    </div>`).join('');
}

function instMod(id,title){
  const sel=document.getElementById('s-'+id);
  const iname=sel?sel.value:null;if(!iname||!instances.length){alert('Create an instance first!');return;}
  const inst=instances.find(i=>i.name===iname);
  status('Installing '+title+'...');
  api('/install_mod',{instance:iname,modId:id,mcVersion:inst?.version||'1.20.1',loader:inst?.loader||'fabric'});
}

function checkUpdates(){
  if(!current){alert('Select an instance first!');return;}
  document.getElementById('upd-btn').innerText='⏳';
  api('/check_updates',{instance:current.name,mcVersion:current.version,loader:current.loader});
}

function importModpack(file){
  if(!file)return;
  const name=prompt('Instance name:',file.name.replace('.mrpack',''));if(!name)return;
  const form=new FormData();form.append('pack',file);form.append('instance',name);
  status('Importing modpack...');
  fetch('/api/import_modpack',{method:'POST',body:form}).then(r=>r.json());
}

async function loadSettings(){
  const d=await fetch('/api/settings/load').then(r=>r.json()).catch(()=>({}));
  if(d.clientId)document.getElementById('cid').value=d.clientId;
  if(d.blacklistUrl)document.getElementById('blurl').value=d.blacklistUrl;
  const a=await fetch('/api/auth/status').then(r=>r.json()).catch(()=>({}));
  document.getElementById('acct-status').innerHTML=a.loggedIn
    ?`<span style="color:var(--green)">✅ ${a.username}</span> <span style="color:var(--dim);font-size:10px;">(${a.type})</span>`
    :'<span style="color:var(--dim)">Not logged in — offline mode</span>';
  if(a.loggedIn){setUser(a.username);document.getElementById('username').value=a.username;}
}
function saveSettings(){
  api('/settings/save',{clientId:document.getElementById('cid').value.trim(),blacklistUrl:document.getElementById('blurl').value.trim()});
  status('Settings saved');
}
async function msLogin(){
  const cid=document.getElementById('cid').value.trim();if(!cid){alert('Enter Client ID first');return;}
  const d=await api('/auth/login/microsoft',{clientId:cid});
  if(d&&d.url)window.open(d.url,'_blank');else if(d&&d.error)alert(d.error);
}
function logout(){fetch('/api/auth/logout',{method:'POST'}).then(()=>loadSettings());}

function setUser(name){
  document.getElementById('user-chip').innerText=name[0].toUpperCase();
  document.getElementById('user-chip').classList.add('ms');
}

function onEv(m){
  if(m.type==='updates_available'){
    const p=document.getElementById('upd-panel');
    document.getElementById('upd-btn').innerText='🔄';
    if(!m.data.updates||!m.data.updates.length){p.style.display='none';return;}
    p.style.display='block';
    p.innerHTML=`<div style="font-size:11px;font-weight:700;color:var(--accent);margin-bottom:10px;">⬆ ${m.data.updates.length} Update(s)</div>`+
      m.data.updates.map(u=>`<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        <div><div style="font-size:11px;font-weight:600;">${u.filename.replace('.jar','').substring(0,28)}</div>
        <div style="font-size:10px;color:var(--dim);">${u.current}→<span style="color:var(--green)">${u.latest}</span></div></div>
        <button class="btn btn-xs btn-primary" onclick="api('/apply_update',{instance:current.name,oldFilename:'${u.filename}',fileUrl:'${u.file_url}',fileName:'${u.file_name}'})">↑</button>
      </div>`).join('');
  }
  if(m.type==='modpack_imported'){
    const inst={name:m.data.name,version:m.data.version,loader:m.data.loader,id:Date.now()};
    instances.push(inst);localStorage.setItem('ki',JSON.stringify(instances));
    renderInstances();status('Modpack ready: '+m.data.name);
  }
  if(m.type==='banned'){alert('❌ Banned: '+m.data.reason);}
  if(m.type==='auth'&&m.data.loggedIn){setUser(m.data.username);document.getElementById('username').value=m.data.username;log('[AUTH] ✅ '+m.data.username);}
}

function log(msg){
  const cls=msg.includes('[OK]')||msg.includes('✅')?'log-ok':msg.includes('[ERR]')||msg.includes('❌')?'log-err':'log-dim';
  const s=`<div class="${cls}">${msg.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`;
  ['console-log'].forEach(id=>{const c=document.getElementById(id);if(c){c.innerHTML+=s;c.scrollTop=c.scrollHeight;}});
}
function status(s){document.getElementById('status-txt').innerText=s;}
function prog(d){
  const b=document.getElementById('prog-bar'),p=document.getElementById('prog-pct');
  if(!d||d.type==='download-finished'){b.style.width='0';p.innerText='0%';document.getElementById('play-btn').style.opacity='1';document.getElementById('launch-btn-big').style.opacity='1';status('Ready');return;}
  if(d.current!=null&&d.total){const pv=Math.round((d.current/d.total)*100);b.style.width=pv+'%';p.innerText=pv+'%';}
}

document.getElementById('settings-username').oninput=function(){
  document.getElementById('username').value=this.value||'Player';
  document.getElementById('user-chip').innerText=(this.value||'P')[0].toUpperCase();
};

renderInstances();log('[KID] ⚡ KidLauncher ready.');

// Check dev mode from server (only true if YOUR settings.json has "devMode": true)
fetch('/api/dev/check').then(r=>r.json()).then(d=>{
  if(d.dev){
    document.querySelectorAll('.dev-only').forEach(el=>el.classList.add('unlocked'));
    log('[DEV] 🔓 Dev mode active');
  }
}).catch(()=>{});

fetch('/api/auth/status').then(r=>r.json()).then(d=>{
  if(d.loggedIn){setUser(d.username);document.getElementById('username').value=d.username;}
}).catch(()=>{});
