(() => {
  'use strict';
  const trigger = document.getElementById('project-update-button');
  if (!trigger) return;
  const phases = new Set(['queued','fetching','preparing','installing','restarting','verifying','rolling_back']);
  const dialog = document.createElement('dialog');
  dialog.className = 'project-update-dialog';
  dialog.setAttribute('aria-labelledby','project-update-title');
  dialog.innerHTML = `<div class="project-update-head"><div><h2 id="project-update-title">项目更新</h2><p>让小三月保持最新状态</p></div><button class="project-update-close" aria-label="关闭更新窗口" type="button">×</button></div>
    <div class="project-update-body"><dl class="project-update-versions"><div class="project-update-version"><dt>当前版本</dt><dd id="update-current">—</dd></div><div class="project-update-version"><dt>GitHub 最新版本</dt><dd id="update-latest">—</dd></div></dl>
    <p class="project-update-status" role="status" aria-live="polite">正在读取版本…</p><progress class="project-update-progress" aria-label="更新进行中" hidden></progress>
    <p class="project-update-note" id="update-reason"></p><p class="project-update-note">更新前会备份当前版本，保留账号、配置和聊天。安装会短暂重启服务，完成后刷新并重新登录。</p>
    <p class="project-update-note"><a id="update-source" target="_blank" rel="noopener noreferrer">查看 GitHub 项目 ↗</a></p></div>
    <div class="project-update-actions"><button id="update-check" type="button">检查更新</button><button id="update-install" type="button" class="update-primary" disabled>更新到最新版</button><button id="update-reload" type="button" class="update-primary" hidden>刷新页面</button></div>`;
  document.body.appendChild(dialog);
  const toast = document.createElement('button');
  toast.type='button'; toast.className='project-update-toast'; toast.hidden=true;
  toast.textContent='发现新版本 · 点击查看更新'; document.body.appendChild(toast);
  const el = selector => dialog.querySelector(selector);
  let info=null, checking=false, job=null, polling=null;
  const label=trigger.querySelector('.update-button-label'), badge=trigger.querySelector('.update-badge');
  const message=el('.project-update-status'), install=el('#update-install'), check=el('#update-check'), reload=el('#update-reload');
  function open() {
    if (!dialog.open) dialog.showModal();
    toast.hidden=true; paint();
    if (!job) load(false);
  }
  trigger.onclick=open; toast.onclick=open;
  el('.project-update-close').onclick=()=>dialog.close();
  dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close();});
  dialog.addEventListener('close',()=>trigger.focus());
  function paint() {
    const busy=job && phases.has(job.phase);
    el('#update-current').textContent=info?.current?.slice(0,7)||'未记录';
    el('#update-latest').textContent=info?.latest?.slice(0,7)||'待检查';
    message.textContent=job?.message || (checking?'正在检查 GitHub 更新…':info?.error || (info?.available?'发现新版本，可以更新。':info?.latest?'当前已经是最新版。':'尚未检查更新。'));
    el('#update-reason').textContent=info?.reason || '';
    el('.project-update-progress').hidden=!busy;
    check.disabled=checking || !!busy;
    install.disabled=checking || !!busy || !info?.can_update;
    install.hidden=job?.phase==='succeeded';
    reload.hidden=!job || phases.has(job.phase);
    label.textContent=busy?'正在更新…':info?.available?'发现新版本':'检查更新';
    badge.hidden=!info?.available || !!busy;
    trigger.dataset.update=info?.available?'available':'';
    const menu=document.getElementById('menu-btn');
    if(menu) menu.dataset.update=info?.available?'available':'';
    if(info?.repo && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(info.repo))el('#update-source').href='https://github.com/'+info.repo;
  }
  async function request(path,options={}) {
    const response=await fetch(path,{credentials:'same-origin',...options});
    let data={}; try{data=await response.json();}catch{}
    if(!response.ok){const error=new Error(data.detail || (response.status===401?'登录已过期，请刷新并重新登录。':'暂时无法连接更新服务。'));error.status=response.status;throw error;}
    return data;
  }
  async function load(manual) {
    if(checking || (job && phases.has(job.phase)))return;
    checking=true; paint();
    try {
      info=await request(manual?'/api/updates/check':'/api/updates?check=1',manual?{method:'POST'}:{});
      if(info.available && !info.error && !manual) {
        let seen=''; try{seen=localStorage.getItem('march-update-notified');}catch{}
        if(seen!==info.latest) {
          try{localStorage.setItem('march-update-notified',info.latest);}catch{}
          toast.style.top=((document.querySelector('header')?.getBoundingClientRect().bottom||60)+8)+'px';
          toast.hidden=false; setTimeout(()=>{toast.hidden=true;},12000);
        }
      }
    }catch(error){info={...(info||{}),error:error.message,can_update:false};}
    finally{checking=false;paint();}
  }
  check.onclick=()=>{job=null;load(true);};
  install.onclick=async()=>{
    if(!info?.can_update)return;
    install.disabled=true;
    try {
      const result=await request('/api/updates/install',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'MarchAgent'},body:JSON.stringify({commit:info.latest})});
      job={...result.job,token:result.token};
      try{sessionStorage.setItem('march-update-job',JSON.stringify(job));}catch{}
      paint(); poll();
    }catch(error){message.textContent=error.message;install.disabled=false;}
  };
  async function poll() {
    clearTimeout(polling);
    if(!job?.id || !job.token)return;
    try {
      const state=await request('/api/updates/jobs/'+encodeURIComponent(job.id),{headers:{'X-Update-Token':job.token}});
      job={...state,token:job.token};
      if(job.phase==='succeeded')info={...(info||{}),current:job.target,latest:job.target,available:false,can_update:false};
      paint();
      if(phases.has(job.phase))polling=setTimeout(poll,3000);
    }catch(error){
      if(error.status===401 || error.status===403){job={...job,phase:'failed',message:error.message};paint();return;}
      message.textContent='服务正在切换；稍后会继续检查。';polling=setTimeout(poll,4000);
    }
  }
  reload.onclick=()=>{
    try {
      const draft=document.getElementById('input')?.value;
      if(draft)sessionStorage.setItem('march-update-draft',draft);
      sessionStorage.removeItem('march-update-job');
    }catch{}
    window.location.reload();
  };
  try {
    const draft=sessionStorage.getItem('march-update-draft');
    if(draft && document.getElementById('input')){document.getElementById('input').value=draft;sessionStorage.removeItem('march-update-draft');}
    const pending=JSON.parse(sessionStorage.getItem('march-update-job')||'null');
    if(pending && /^[a-f0-9]{32}$/.test(pending.id) && typeof pending.token==='string' && pending.token.length<200){job=pending;poll();}
  }catch{}
  setTimeout(()=>load(false),1500);
  setInterval(()=>{if(!document.hidden)load(false);},30*60*1000);
})();
