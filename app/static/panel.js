(function () {
  'use strict';
  window.panelToast = function(message, kind = 'good') {
    let region = document.getElementById('panel-toast-region');
    if (!region) { region = document.createElement('div'); region.id = 'panel-toast-region'; region.setAttribute('aria-live', 'polite'); document.body.appendChild(region); }
    const item = document.createElement('div'); item.className = 'panel-toast ' + kind; item.textContent = String(message); region.appendChild(item);
    while (region.childElementCount > 4) region.firstElementChild.remove();
    setTimeout(() => item.remove(), 8000);
  };
  const csrfMeta = document.querySelector('meta[name="fargovpn-csrf-token"]');
  const CSRF_TOKEN = csrfMeta ? String(csrfMeta.content || '') : '';
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (resource, options) {
    const init = Object.assign({}, options || {});
    const method = String(init.method || (resource instanceof Request ? resource.method : 'GET')).toUpperCase();
    const target = resource instanceof Request ? resource.url : String(resource || '');
    let sameOrigin = true;
    try { sameOrigin = new URL(target, location.href).origin === location.origin; } catch (_) {}
    if (CSRF_TOKEN && sameOrigin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(resource instanceof Request ? resource.headers : undefined);
      new Headers(init.headers || {}).forEach((value, key) => headers.set(key, value));
      headers.set('X-CSRF-Token', CSRF_TOKEN);
      init.headers = headers;
    }
    return nativeFetch(resource, init);
  };

  document.addEventListener('submit', async (event) => {
    if (event.defaultPrevented || !CSRF_TOKEN) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || String((event.submitter && event.submitter.getAttribute('formmethod')) || form.method || 'get').toLowerCase() !== 'post') return;
    event.preventDefault();
    if (form.dataset.submitting === '1') return;
    form.dataset.submitting = '1';
    const submitter = event.submitter;
    if (submitter) { submitter.disabled = true; submitter.setAttribute('aria-busy', 'true'); }
    const data = new FormData(form);
    if (submitter && submitter.name) data.append(submitter.name, submitter.value || '');
    const target = submitter && submitter.getAttribute('formaction') ? new URL(submitter.getAttribute('formaction'), document.baseURI).href : (form.action || location.href);
    try {
      const response = await window.fetch(target, {
        method: 'POST', body: data, credentials: 'same-origin', redirect: 'follow', signal: AbortSignal.timeout(30000)
      });
      if (response.redirected || response.ok) location.assign(response.url || location.href);
      else {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || ('HTTP ' + response.status));
      }
    } catch (error) {
      if (submitter) submitter.disabled = false;
      const status = document.getElementById('settings-save-status');
      if (status) { status.hidden = false; status.textContent = 'Не удалось сохранить: ' + error.message; }
      else window.panelToast('Запрос не выполнен: ' + error.message, 'bad');
    } finally { delete form.dataset.submitting; if (submitter) { submitter.disabled = false; submitter.removeAttribute('aria-busy'); } }
  });

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const publicScopeMeta = document.querySelector('meta[name=\"fargovpn-sw-scope\"]');
  const PUBLIC_PREFIX = publicScopeMeta ? String(publicScopeMeta.content || '').replace(/\/$/, '') : '';
  const panelPath = (path) => {
    const raw = String(path || '/');
    if (!raw.startsWith('/')) return (PUBLIC_PREFIX ? PUBLIC_PREFIX + '/' : '/') + raw;
    return PUBLIC_PREFIX && !raw.startsWith(PUBLIC_PREFIX + '/') ? PUBLIC_PREFIX + raw : raw;
  };

  async function pollPlatformUpdate() {
    const slot = document.getElementById('global-update-slot');
    if (!slot || document.getElementById('global-update-banner')) return;
    try {
      const response = await fetch(panelPath('/api/panel/update-status'), {cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      if (!data.available) return;
      const box = document.createElement('div');
      box.className = 'update-banner';
      box.id = 'global-update-banner';
      const strong = document.createElement('strong');
      strong.textContent = 'Доступно обновление ' + (data.version || '');
      const span = document.createElement('span');
      span.textContent = 'Новая версия готова к установке.';
      const link = document.createElement('a');
      link.className = 'button small';
      link.href = panelPath('/updates');
      link.textContent = 'Обновить';
      box.append(strong, span, link);
      slot.replaceWith(box);
    } catch (_) {}
  }

  function scrollPanelContainersToBottom(root = document) {
    const containers = root.querySelectorAll(
      '.chat-window, pre.auto-scroll-bottom, .log-output, [data-auto-scroll-bottom]'
    );
    containers.forEach((container) => {
      if (container && typeof container.scrollHeight === 'number') {
        container.scrollTop = container.scrollHeight;
      }
    });
  }

  function initBottomScroll() {
    const run = () => scrollPanelContainersToBottom();
    run();
    requestAnimationFrame(run);
    setTimeout(run, 80);
    document.querySelectorAll('.chat-window, pre.auto-scroll-bottom, .log-output, [data-auto-scroll-bottom]').forEach((container) => {
      if (container.dataset.bottomScrollObserver === '1' || typeof MutationObserver === 'undefined') return;
      container.dataset.bottomScrollObserver = '1';
      const observer = new MutationObserver(() => {
        if (!document.hidden) container.scrollTop = container.scrollHeight;
      });
      observer.observe(container, {childList: true, subtree: true});
    });
  }

  function initMobileNavigation() {
    const body = document.body;
    const root = document.documentElement;
    const toggle = document.getElementById('mobile-nav-toggle');
    const backdrop = document.getElementById('mobile-nav-backdrop');
    const aside = document.querySelector('body.panel-page > aside');
    if (!aside) return;

    const isMobile = () => window.matchMedia ? window.matchMedia('(max-width: 900px)').matches : window.innerWidth <= 900;
    const setOpen = (open) => {
      const mobile = isMobile();
      const next = Boolean(mobile && open);
      body.classList.toggle('mobile-nav-open', next);
      root.classList.toggle('mobile-menu-open', next);
      aside.setAttribute('aria-hidden', mobile && !next ? 'true' : 'false');
      if (toggle) {
        toggle.disabled = false;
        toggle.removeAttribute('disabled');
        toggle.removeAttribute('tabindex');
        toggle.setAttribute('aria-expanded', next ? 'true' : 'false');
        toggle.setAttribute('aria-label', next ? 'Закрыть меню' : 'Открыть меню');
        toggle.classList.toggle('is-open', next);
      }
      if (!mobile) {
        aside.style.removeProperty('transform');
        aside.style.removeProperty('visibility');
        aside.style.removeProperty('pointer-events');
      }
    };
    if (toggle && toggle.dataset.navBound !== '1') {
      toggle.dataset.navBound = '1';
      toggle.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!isMobile()) return;
        setOpen(!body.classList.contains('mobile-nav-open'));
      }, {passive:false});
    }
    if (backdrop && backdrop.dataset.navBound !== '1') {
      backdrop.dataset.navBound = '1';
      backdrop.addEventListener('click', () => setOpen(false), {passive:true});
    }
    aside.addEventListener('click', (event) => {
      if (!isMobile()) return;
      const link = event.target.closest('nav a, .aside-foot a');
      if (link) setOpen(false);
    }, {passive:true});

    const sync = () => {
      if (isMobile()) {
        const narrow = window.innerWidth <= 600;
        aside.style.removeProperty('transform');
        aside.style.removeProperty('visibility');
        aside.style.removeProperty('pointer-events');
        aside.style.width = narrow ? '' : '230px';
        setOpen(body.classList.contains('mobile-nav-open'));
      } else {
        body.classList.remove('mobile-nav-open');
        root.classList.remove('mobile-menu-open');
        aside.style.removeProperty('transform');
        aside.style.removeProperty('visibility');
        aside.style.removeProperty('pointer-events');
        aside.style.removeProperty('width');
        aside.setAttribute('aria-hidden', 'false');
        if (toggle) {
          toggle.classList.remove('is-open');
          toggle.setAttribute('aria-expanded', 'false');
        }
      }
    };
    sync();
    window.addEventListener('resize', sync, {passive:true});
    window.addEventListener('orientationchange', () => setTimeout(sync, 0), {passive:true});
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && isMobile() && body.classList.contains('mobile-nav-open')) setOpen(false);
    });
  }

  function initTabs() {
    document.querySelectorAll('[data-tabs]').forEach((root) => {
      if (root.closest('#settings-form')) return; // Settings uses its own tab handler and saved tab query.
      const buttons = Array.from(root.querySelectorAll('[data-tab]'));
      const sectionScope = root.parentElement || document;
      const sections = Array.from(sectionScope.querySelectorAll('[data-tab-section]'));
      const activate = (name, writeHash = true) => {
        buttons.forEach((button) => {
          const active = button.dataset.tab === name;
          button.classList.toggle('active', active);
          button.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        sections.forEach((section) => section.classList.toggle('active', section.dataset.tabSection === name));
        if (writeHash && history.replaceState) history.replaceState(null, '', '#' + encodeURIComponent(name));
      };
      root.classList.add('tabs-ready');
      root.setAttribute('role', 'tablist');
      buttons.forEach((button) => {
        button.setAttribute('role', 'tab');
        button.addEventListener('click', (event) => {
          event.preventDefault();
          activate(button.dataset.tab);
        });
      });
      const rawHash = location.hash.slice(1);
      let requested = '';
      try { requested = decodeURIComponent(rawHash); } catch (_) { requested = rawHash; }
      const initial = buttons.some((button) => button.dataset.tab === requested)
        ? requested : (buttons[0] && buttons[0].dataset.tab);
      if (initial) activate(initial, false);
    });
  }

  function initConfirmations() {
    document.querySelectorAll('[data-confirm]').forEach((element) => {
      element.addEventListener('click', (event) => {
        if (!window.confirm(element.dataset.confirm || 'Продолжить?')) event.preventDefault();
      });
    });
  }


  function initBroadcastForm() {
    const form = document.querySelector('[data-broadcast-form]');
    if (!form || form.dataset.broadcastBound === '1') return;
    form.dataset.broadcastBound = '1';
    const file = form.querySelector('#broadcast-file');
    const message = form.querySelector('#broadcast-message');
    const button = form.querySelector('#broadcast-start');
    const upload = form.querySelector('#broadcast-upload');
    const bar = form.querySelector('#broadcast-upload-bar');
    const uploadText = form.querySelector('#broadcast-upload-text');
    const csrf = document.querySelector('meta[name="fargovpn-csrf-token"]');
    const csrfToken = csrf ? String(csrf.content || '') : '';
    const busy = () => form.dataset.broadcastSubmitting === '1';
    form.addEventListener('submit', (event) => {
      if (busy()) { event.preventDefault(); return; }
      event.preventDefault(); event.stopImmediatePropagation();
      const hasFile = Boolean(file && file.files && file.files.length);
      const text = String(message ? message.value : '').trim();
      if (!hasFile && !text) { window.alert('Введите текст сообщения или прикрепите файл.'); return; }
      if (hasFile && text.length > 1024) { window.alert('При наличии вложения текст должен быть не длиннее 1024 символов.'); return; }
      if (!window.confirm('Запустить массовую рассылку всем пользователям?')) return;
      form.dataset.broadcastSubmitting = '1';
      if (button) button.disabled = true;
      if (upload) upload.classList.add('visible');
      if (bar) bar.style.width = '0%';
      if (uploadText) uploadText.textContent = 'Передача данных на сервер…';
      const xhr = new XMLHttpRequest();
      xhr.open('POST', form.action || '/broadcast/start');
      xhr.setRequestHeader('Accept', 'application/json');
      if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const percent = Math.round(e.loaded / e.total * 100);
        if (bar) bar.style.width = percent + '%';
        if (uploadText) uploadText.textContent = 'Загружено ' + percent + '%';
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) {
          if (bar) bar.style.width = '100%';
          if (uploadText) uploadText.textContent = 'Рассылка запущена; статус обновляется ниже.';
          form.dataset.broadcastSubmitting = '0';
          const state = data.status || data;
          const statusBox = document.getElementById('broadcast-state');
          const errorBox = document.getElementById('broadcast-error');
          if (statusBox && state.state) statusBox.textContent = state.state === 'queued' ? 'В очереди' : (state.state === 'running' ? 'Отправка' : state.state);
          if (errorBox) { errorBox.textContent = ''; errorBox.style.display = 'none'; }
          if (window.initBroadcastPolling) window.initBroadcastPolling();
        } else {
          form.dataset.broadcastSubmitting = '0';
          if (button) button.disabled = false;
          const detail = data.detail || data.error || ('HTTP ' + xhr.status);
          if (uploadText) uploadText.textContent = 'Ошибка: ' + detail;
          const errorBox = document.getElementById('broadcast-error');
          if (errorBox) { errorBox.textContent = String(detail); errorBox.style.display = 'block'; }
        }
      };
      xhr.onerror = () => {
        form.dataset.broadcastSubmitting = '0';
        if (button) button.disabled = false;
        if (uploadText) uploadText.textContent = 'Соединение прервано. Статус рассылки будет проверен автоматически.';
      };
      xhr.ontimeout = xhr.onerror; xhr.timeout = 120000;
      xhr.send(new FormData(form));
    });
  }

  function initSubscriptionRefreshTools() {
    const form = document.getElementById('subscription-refresh-form');
    const panel = document.getElementById('subscription-refresh-panel');
    if (!form || !panel) return;
    const start = document.getElementById('subscription-refresh-start');
    const box = document.getElementById('subscription-refresh-status');
    const get = (id) => document.getElementById(id);
    let active = false;
    const set = (id, value) => { const node = get(id); if (node) node.textContent = value == null ? '' : String(value); };
    const render = (data) => {
      if (!data || !data.state) return;
      active = ['queued', 'running'].includes(data.state);
      const progress = Math.max(0, Math.min(100, Number(data.progress) || 0));
      const bar = get('subscription-refresh-progress-bar');
      if (bar) bar.style.width = progress + '%';
      set('subscription-refresh-progress-percent', progress + '%');
      set('subscription-refresh-progress-text', data.message || 'Ожидание запуска');
      set('subscription-refresh-processed', (data.processed || 0) + ' / ' + (data.total || 0));
      set('subscription-refresh-delivered', data.delivered || 0);
      set('subscription-refresh-skipped', data.skipped || 0);
      set('subscription-refresh-failed', data.failed || 0);
      set('subscription-refresh-current', data.current_user ? 'Сейчас: ' + data.current_user + (data.last_action ? ' · ' + data.last_action : '') : '');
      set('subscription-refresh-log-body', Array.isArray(data.recent_log) && data.recent_log.length ? data.recent_log.join('\n') : (data.error || 'Журнал пока пуст.'));
      const badge = get('subscription-refresh-badge');
      if (badge) {
        badge.textContent = data.state === 'completed' ? 'Завершено' : data.state === 'failed' ? 'Ошибка' : active ? 'Выполняется' : 'Ожидание';
        badge.className = 'badge ' + (data.state === 'failed' ? 'bad' : data.state === 'completed' ? 'good' : active ? 'warn' : '');
      }
      if (start) { start.disabled = active; start.textContent = active ? '⏳ Выполняется…' : '↻ Запустить отправку'; }
      if (box) {
        box.hidden = !data.error;
        box.textContent = data.error ? 'Ошибка: ' + data.error : '';
        box.className = 'notice bad';
      }
    };
    const poll = async () => {
      try {
        const response = await fetch(panelPath('/api/users/subscription-refresh/status'), {cache:'no-store', credentials:'same-origin', headers:{Accept:'application/json'}});
        if (response.ok) render(await response.json());
      } catch (_) {}
      setTimeout(poll, active ? 900 : 5000);
    };
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!confirm('Разослать всем Telegram-пользователям их актуальные ссылки подписки?')) return;
      if (start) start.disabled = true;
      try {
        const response = await fetch(form.action, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8','Accept':'application/json'}, body:new URLSearchParams(new FormData(form))});
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) throw new Error(data.detail || data.error || 'Задача не запущена');
        render(data.status || data);
      } catch (error) {
        if (box) { box.hidden = false; box.textContent = 'Ошибка запуска: ' + (error.message || error); box.className = 'notice bad'; }
        if (start) start.disabled = false;
      }
    });
    poll();
  }

  function initUnreadMessages() {
    // The Messages page owns a faster incremental feed; avoid a second
    // unread poll that could race and overwrite its fresher snapshot.
    if (document.querySelector('[data-message-live-feed]')) return;
    const badges = () => Array.from(document.querySelectorAll('[data-unread-total]'));
    let timer = 0;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || document.hidden) { timer = setTimeout(refresh, 15000); return; }
      inFlight = true;
      try {
        const response = await fetch(panelPath('/api/panel/messages/unread'), {
          credentials: 'same-origin', cache: 'no-store', headers: {Accept:'application/json'}
        });
        if (!response.ok) return;
        const data = await response.json();
        const total = Math.max(0, Number(data.total) || 0);
        badges().forEach((node) => { node.textContent = total ? String(total) : ''; node.hidden = !total; });
      } catch (_) {}
      finally { inFlight = false; timer = setTimeout(refresh, 15000); }
    };
    refresh();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(timer); refresh(); } });
    window.addEventListener('pagehide', () => clearTimeout(timer), {once:true});
  }

  function initPaymentAttention() {
    const badges = () => Array.from(document.querySelectorAll('[data-payment-attention]'));
    let timer = 0;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || document.hidden) { timer = setTimeout(refresh, 15000); return; }
      inFlight = true;
      try {
        const response = await fetch(panelPath('/api/panel/payments/attention'), {
          credentials: 'same-origin', cache: 'no-store', headers: {Accept:'application/json'}
        });
        if (!response.ok) return;
        const data = await response.json();
        const total = Math.max(0, Number(data.total) || 0);
        badges().forEach((node) => { node.textContent = total ? String(total) : ''; node.hidden = !total; });
      } catch (_) {}
      finally { inFlight = false; timer = setTimeout(refresh, 15000); }
    };
    refresh();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(timer); refresh(); } });
    window.addEventListener('pagehide', () => clearTimeout(timer), {once:true});
  }

  function initServiceWorker() {
    const version = document.querySelector('meta[name="fargovpn-app-version"]');
    window.__FARGOVPN_APP_VERSION__ = version ? version.content : '';
    const meta = document.querySelector('meta[name="fargovpn-sw-url"]');
    if (!meta || !('serviceWorker' in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register(meta.content, {updateViaCache:'none'})
      .then(r => r.update()).catch(e => window.panelToast('Service Worker: ' + e.message, 'bad'));
  }

  function startPanelScripts() {
    const safeInit = (name, fn) => { try { fn(); } catch (error) { console.error('[FargoVPN]', name, error); } };
    safeInit('service-worker', initServiceWorker);
    safeInit('broadcast-form', initBroadcastForm);
    safeInit('bottom-scroll', initBottomScroll);
    safeInit('mobile-navigation', initMobileNavigation);
    safeInit('tabs', initTabs);
    safeInit('confirmations', initConfirmations);
    safeInit('subscription-refresh', initSubscriptionRefreshTools);
    if (document.body.classList.contains('panel-page')) {
      safeInit('unread-messages', initUnreadMessages);
      safeInit('payment-attention', initPaymentAttention);
    }
    setTimeout(pollPlatformUpdate, 3000);
    setInterval(pollPlatformUpdate, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startPanelScripts, {once: true});
  } else {
    startPanelScripts();
  }
})();

/* FargoVPN 4.2.1 interaction layer. Keeps page-specific handlers intact. */
(function(){
  const root=document.documentElement;
  const body=document.body;
  if(!body.classList.contains('panel-page')) return;

  const nav=document.getElementById('panel-sidebar');
  if(nav){
    nav.addEventListener('mouseenter',()=>root.classList.add('fv42-nav-hover'),{passive:true});
    nav.addEventListener('mouseleave',()=>root.classList.remove('fv42-nav-hover'),{passive:true});
    nav.querySelectorAll('a').forEach(link=>{
      link.addEventListener('focus',()=>root.classList.add('fv42-nav-hover'),{passive:true});
      link.addEventListener('blur',()=>root.classList.remove('fv42-nav-hover'),{passive:true});
    });
  }

  document.querySelectorAll('.button,button,.quick-action,.header-icon').forEach((el)=>{
    el.addEventListener('click',()=>{
      el.classList.remove('fv42-pulse');
      void el.offsetWidth;
      el.classList.add('fv42-pulse');
    },{passive:true});
  });

  const search=document.querySelector('[data-panel-search]');
  if(search){
    search.addEventListener('keydown',(event)=>{
      if(event.key==='Escape') search.blur();
      if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();search.focus();}
    });
  }
})();
