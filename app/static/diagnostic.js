(function(){
 const button=document.getElementById('diagnostic-collect');if(!button)return;
 const out=document.getElementById('diagnostic-progress'),link=document.getElementById('diagnostic-download');
 const scope=document.querySelector('meta[name="fargovpn-sw-scope"]');const prefix=(scope?.content||'').replace(/\/$/,'');
 const url=p=>prefix+p;
 button.addEventListener('click',async()=>{
  if(button.disabled)return;button.disabled=true;button.setAttribute('aria-busy','true');link.hidden=true;out.textContent='Собираю сведения и журналы…';
  let timer;
  const fetchJson=async(p,options={})=>{const r=await fetch(url(p),{...options,signal:AbortSignal.timeout(15000)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'HTTP '+r.status);return d;};
  try{
   const {job}=await fetchJson('/api/diagnostics/collect',{method:'POST'});const deadline=Date.now()+240000;
   while(true){
    if(Date.now()>deadline)throw new Error('Таймаут ожидания результата');
    const d=await fetchJson('/api/diagnostics/collect/'+job);
    if(d.state==='failed')throw new Error(d.error||'Сбор прерван');
    if(d.state==='completed'){
     out.textContent=d.checks.map(c=>c.status+' '+c.name).join('\n');link.href=url('/api/diagnostics/collect/'+job+'/download');link.hidden=false;
     window.panelToast('Отчёт готов. PASS: '+d.checks.filter(c=>c.status==='PASS').length+', FAIL: '+d.checks.filter(c=>c.status==='FAIL').length);break;
    }
    out.textContent='Собираю сведения и журналы… '+Math.round((240000-(deadline-Date.now()))/1000)+' с';await new Promise(r=>{timer=setTimeout(r,1000)});
   }
  }catch(e){out.textContent='Не удалось собрать отчёт: '+e.message;window.panelToast(out.textContent,'bad');}
  finally{clearTimeout(timer);button.disabled=false;button.removeAttribute('aria-busy');}
 });
})();
