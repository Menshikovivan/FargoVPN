
(function(){
  // Settings must remain usable even when an old PWA cache delays panel.js.
  const settingsForm=document.getElementById('settings-form');
  if(settingsForm){
    const saveButton=settingsForm.querySelector('.settings-actions button[type="submit"]');
    const saveCard=settingsForm.querySelector('.settings-actions');
    let status=settingsForm.querySelector('#settings-save-status');
    if(!status){
      status=document.createElement('div');
      status.id='settings-save-status';
      status.className='notice';
      status.style.marginTop='10px';
      status.setAttribute('role','status');
      status.setAttribute('aria-live','polite');
      if(saveCard) saveCard.appendChild(status);
    }
    settingsForm.addEventListener('submit', function(event){
      if(event.submitter===saveButton){
        settingsForm.action=(settingsForm.querySelector('[name="settings_tab"]')?.value==='xui')
          ? settingsForm.dataset.xuiAction
          : settingsForm.dataset.settingsAction;
      }
      if(status){
        status.textContent='Отправляю настройки на сервер…';
        status.hidden=false;
      }
      if(saveButton){
        saveButton.disabled=true;
        saveButton.dataset.originalText=saveButton.textContent;
        saveButton.textContent='Сохранение…';
      }
      // Do not cancel the submit event: the browser sends the real POST.
    });
  }
  const root=document.querySelector('[data-tabs]');
  if(!root) return;
  const buttons=[...root.querySelectorAll('[data-tab]')];
  const sections=[...document.querySelectorAll('[data-tab-section]')];
  const selected=settingsForm.querySelector('[name="settings_tab"]');
  const activate=(name, clicked=false)=>{
    if(selected) selected.value=name;
    buttons.forEach(b=>{
      const on=b.dataset.tab===name;
      b.classList.toggle('active',on);
      b.setAttribute('aria-selected',on?'true':'false');
    });
    sections.forEach(s=>s.classList.toggle('active',s.dataset.tabSection===name));
    if(clicked){
      const url=new URL(location.href);url.searchParams.set('tab',name);url.hash='';
      try{history.replaceState(null,'',url);}catch(_){}
      window.scrollTo({top:0,behavior:'instant'});
    }
  };
  buttons.forEach(b=>b.addEventListener('click',(e)=>{e.preventDefault();activate(b.dataset.tab,true);}));
  let name=new URLSearchParams(location.search).get('tab')||'';
  if(!name){try{name=decodeURIComponent(location.hash.slice(1));}catch(_){}}
  activate(buttons.some(b=>b.dataset.tab===name)?name:(buttons[0]?.dataset.tab||''));
})();
