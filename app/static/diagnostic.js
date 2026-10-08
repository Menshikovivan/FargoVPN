(function(){
 const button=document.getElementById('diagnostic-collect');if(!button)return;
 const out=document.getElementById('diagnostic-progress'),link=document.getElementById('diagnostic-download');
 const scope=document.querySelector('meta[name="fargovpn-sw-scope"]');const prefix=(scope?.content||'').replace(/\/$/,'');
 const url=p=>prefix+p;
 const api=async(path,options={})=>{
   if(window.apiFetch){const result=await window.apiFetch(url(path),{...options,timeout:20000});return result.data||{};}
   const r=await fetch(url(path),{...options,credentials:'same-origin',signal:AbortSignal.timeout(20000),headers:{Accept:'application/json',...(options.headers||{})}});
   const ct=String(r.headers.get('content-type')||'').toLowerCase();const raw=await r.text();let d=null;try{if(ct.includes('application/json')&&raw.trim())d=JSON.parse(raw);}catch(e){d=null;}
   if(!r.ok)throw new Error('HTTP '+r.status+(raw.trim()?' · '+raw.replace(/\s+/g,' ').slice(0,240):''));
   if(!d)throw new Error('Сервер вернул не JSON (Content-Type: '+(ct||'не указан')+') · '+raw.replace(/\s+/g,' ').slice(0,240));
   return d;
 };
 button.addEventListener('click',async()=>{
  if(button.disabled)return;button.disabled=true;button.setAttribute('aria-busy','true');link.hidden=true;out.textContent='Собираю сведения и журналы…';let timer;
  try{
   const started=await api('/api/diagnostics/collect',{method:'POST'});const job=String(started.job_id || started.job?.job_id || started.job?.id || '').trim();if(!job)throw new Error('Сервер не вернул идентификатор диагностики');
   const deadline=Date.now()+240000;
   while(true){
    if(Date.now()>deadline)throw new Error('Таймаут ожидания результата');
    const d=await api('/api/diagnostics/collect/'+encodeURIComponent(job));
    if(d.state==='failed')throw new Error(d.error||'Сбор прерван');
    if(d.state==='completed'){
      const checks=Array.isArray(d.checks)?d.checks:[];out.textContent=checks.map(c=>c.status+' '+c.name).join('\n')||'Отчёт пуст';
      link.href=url('/api/diagnostics/collect/'+encodeURIComponent(job)+'/download');link.hidden=false;out.textContent=(checks.map(c=>c.status+' '+c.name).join('\n')||'Отчёт пуст')+'\n\nФайл: '+String(d.path||'—');
      window.panelToast('Отчёт готов. PASS: '+checks.filter(c=>c.status==='PASS').length+', FAIL: '+checks.filter(c=>c.status==='FAIL').length, checks.some(c=>c.status==='FAIL')?'bad':'good');break;
    }
    const remain=Math.max(0,Math.round((deadline-Date.now())/1000));out.textContent='Собираю сведения и журналы… ещё до '+remain+' с';await new Promise(r=>{timer=setTimeout(r,1000)});
   }
  }catch(e){out.textContent='Не удалось собрать отчёт: '+(e?.message||e);window.panelToast(out.textContent,'bad');}
  finally{clearTimeout(timer);button.disabled=false;button.removeAttribute('aria-busy');}
 });
})();
