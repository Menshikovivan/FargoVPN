(() => {
  'use strict';
  const root = document.querySelector('meta[name="fargovpn-canonical-path"]')?.content || '/';
  const api = path => root.replace(/\/$/, '') + path;
  const text = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = String(value); };
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const bytes = value => {
    let n = Math.max(0, number(value)), i = 0;
    const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + units[i];
  };
  const read = async path => {
    const controller = new AbortController(); controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(api(path), {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return await response.json();
    } finally { clearTimeout(timeout); controllers.delete(controller); }
  };
  const samples = [], xuiCpu = [], download = [], upload = [];
  const controllers = new Set();
  const chart = (id, series, maximum = 100) => {
    const el = document.getElementById(id); if (!el) return;
    const lines = series.map((values, index) => {
      const points = (values.length === 1 ? [values[0], values[0]] : values)
        .map((v, i, a) => `${i * 800 / Math.max(1, a.length - 1)},${210 - Math.min(maximum, Math.max(0, v)) / maximum * 210}`).join(' ');
      return `<polyline class="${index ? 'chart-upload' : ''}" points="${points}"/>`;
    });
    el.innerHTML = `<svg viewBox="0 0 800 210" preserveAspectRatio="none" role="img" aria-label="График метрик">${lines.join('')}</svg>`;
  };
  const sample = (list, value) => { list.push(number(value)); if (list.length > 50) list.shift(); };
  let stopped = false, timer;
  const metrics = async () => {
    try {
      const d = await read('/api/metrics');
      ['cpu', 'ram', 'swap', 'disk'].forEach(key => text('mon-' + key, Math.round(number(d[key])) + '%'));
      text('mon-load', `Load: ${number(d.load1).toFixed(2)} / ${number(d.load5).toFixed(2)} / ${number(d.load15).toFixed(2)}`);
      text('mon-ram-hint', `${bytes(d.ram_used)} из ${bytes(d.ram_total)}`);
      text('mon-swap-hint', `${bytes(d.swap_used)} из ${bytes(d.swap_total)}`);
      text('mon-disk-hint', 'Свободно ' + bytes(d.disk_free));
      text('mon-bytes-in', d.net_down || '0 Б/с');
      text('mon-bytes-out', d.net_up || '0 Б/с');
      sample(samples, d.cpu); chart('monitor-chart', [samples]);
    } catch (_) { text('mon-load', 'Метрики недоступны; повтор через 10 секунд'); }
  };
  const online = async () => {
    try { const d = await read('/api/online-metrics'); text('mon-online', d.stale ? '—' : Math.max(0, number(d.count))); }
    catch (_) { text('mon-online', '—'); }
  };
  const telemetry = async () => {
    try {
      const d = await read('/api/xui-telemetry');
      const s = d.status || {};
      text('mon-xui-state', s.xray_state || 'Нет данных');
      text('mon-xui-version', s.xray_version || '—');
      const available=s._available||{};const hasCpu=available.cpu!==false&&s.cpu!=null;const hasNetwork=available.network!==false&&s.net_down_speed!=null&&s.net_up_speed!=null;
      text('mon-xui-tcp', available.tcp_count!==false&&s.tcp_count!=null ? number(s.tcp_count) : 'Нет данных');
      text('mon-xui-speed', hasNetwork ? `${bytes(s.net_down_speed)}/с / ${bytes(s.net_up_speed)}/с` : 'Нет данных');
      if (Object.keys(s).length && !d.error) {
        if(hasCpu){sample(xuiCpu,s.cpu);chart('monitor-xui-cpu-chart',[xuiCpu]);}else{text('monitor-xui-cpu-chart','Нет данных CPU');}
        if(hasNetwork){sample(download,s.net_down_speed);sample(upload,s.net_up_speed);chart('monitor-speed-chart',[download,upload],Math.max(1,...download,...upload));text('mon-speed-scale','Максимум шкалы: '+bytes(Math.max(1,...download,...upload))+'/с');}else{text('monitor-speed-chart','Нет данных скорости');text('mon-speed-scale','Нет данных');}
      }
      text('mon-xui-fail2ban', d.fail2ban?.status || 'Нет данных');
      text('mon-xui-error', d.error || '');
      const badge = document.getElementById('mon-xui-state');
      if (badge) badge.className = 'badge ' + (s.xray_state === 'running' ? 'good' : 'warn');
      const nodes = Array.isArray(d.nodes) ? d.nodes : [];
      const target = document.getElementById('mon-xui-nodes');
      if (target) {
        target.replaceChildren();
        for (const node of nodes.slice(0, 50)) {
          const row = document.createElement('tr');
          [node.name || 'Без имени', node.address || '—', node.status || 'unknown', node.version || '—',
            bytes(node.net_down) + '/с', bytes(node.net_up) + '/с', number(node.latency_ms) + ' мс'].forEach(value => {
            const cell = document.createElement('td'); cell.textContent = String(value); row.appendChild(cell);
          });
          target.appendChild(row);
        }
      }
      const card = document.getElementById('mon-xui-nodes-card');
      if (card) card.hidden = !nodes.length;
      text('mon-xui-nodes-count', `${nodes.filter(n => ['online', 'running', 'ok', 'active'].includes(n.status)).length}/${nodes.length} онлайн`);
    } catch (e) { text('mon-xui-error', 'Телеметрия 3x-ui недоступна: ' + e.message); }
  };
  const tick = async () => {
    if (stopped) return;
    if (!document.hidden) await Promise.allSettled([metrics(), online(), telemetry()]);
    if (!stopped) timer = setTimeout(tick, 10000);
  };
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); controllers.forEach(c => c.abort()); });
  window.addEventListener('pageshow', () => { if (stopped) { stopped = false; tick(); } });
  tick();
})();
