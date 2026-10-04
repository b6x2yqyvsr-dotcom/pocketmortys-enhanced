/* 控制台前端逻辑 —— 无框架，直接操作 DOM。
   数据来自 /admin/api/*，每 4 秒轮询当前页面。 */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = { page: 'overview', timer: null, traceId: null };

/* ── 工具 ───────────────────────────────────────────── */

async function api(path, opts) {
  const r = await fetch('/admin/api' + path, opts);
  return r.json();
}

async function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function uptime(sec) {
  sec = Math.max(0, sec | 0);
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
        m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d}天 ${h}小时`;
  if (h) return `${h}小时 ${m}分`;
  if (m) return `${m}分 ${s}秒`;
  return `${s}秒`;
}

function ago(sec) {
  const n = Math.max(0, Number(sec) || 0);
  if (n < 60) return n + ' 秒前';
  if (n < 3600) return Math.floor(n / 60) + ' 分钟前';
  if (n < 86400) return Math.floor(n / 3600) + ' 小时前';
  return Math.floor(n / 86400) + ' 天前';
}

const KIND_LABEL = {
  game: '游戏客户端', browser: '浏览器', curl: 'curl',
  android: 'Android', script: '脚本', unknown: '未知', other: '其他',
};

/* ── 页面切换 ───────────────────────────────────────── */

const TITLES = {
  overview: '概览', devices: '设备', players: '玩家', rooms: '房间',
  events: '事件流', settings: '设置', system: '系统', console: '请求日志',
};

function goto(page) {
  state.page = page;
  $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  $$('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + page));
  $('#page-title').textContent = TITLES[page] || page;
  render();
}

/* ── 概览 ───────────────────────────────────────────── */

async function renderOverview() {
  const [d, dev] = await Promise.all([api('/overview'), api('/devices')]);

  $('#chip-host').textContent = `${d.server.host}:${d.server.port}`;
  $('#chip-uptime').textContent = '运行 ' + uptime(d.server.uptime_seconds);
  $('#chip-online').textContent = `在线 ${d.counts.online}`;

  const a = $('#announce');
  if (d.announcement) { a.textContent = d.announcement; a.hidden = false; }
  else a.hidden = true;

  const c = d.counts;
  $('#stat-cards').innerHTML = [
    ['注册玩家', c.users, '累计账号数', ''],
    ['在线玩家', c.online, '5 分钟内活跃', 'b'],
    ['已连接设备', dev.summary.total, `${dev.summary.online} 个活跃`, 'p'],
    ['房间', c.rooms, `${c.live_rooms} 个有人`, 'w'],
    ['事件', c.events, 'event_queue 行数', ''],
    ['莫蒂', c.mortys, 'owned_morties 行数', ''],
  ].map(([k, v, s, cls]) => `
    <div class="card ${cls}">
      <div class="k">${k}</div>
      <div class="v">${Number(v || 0).toLocaleString()}</div>
      <div class="s">${s}</div>
    </div>`).join('');

  const ch = d.cheats;
  $('#cheat-grid').innerHTML = [
    ['作弊总开关', ch.enabled], ['免费扭蛋', ch.free_gacha],
    ['新号 50 级', ch.start_level_50], ['全地图解锁', ch.all_dimensions],
    ['全闪亮', ch.all_shiny], ['完美 IV 16/16/16/16', ch.perfect_ivs],
  ].map(([n, on]) =>
    `<span class="cheat ${on ? 'on' : 'off'}"><span class="d"></span>${n}</span>`).join('');

  $('#dev-mini-hint').textContent =
    `${dev.summary.total} 个已记录 / ${dev.summary.online} 个活跃`;
  $('#dev-mini').innerHTML = dev.devices.length
    ? `<div class="table-wrap"><table>${devRows(dev.devices.slice(0, 6))}</table></div>`
    : '<div class="empty">还没有客户端连接过</div>';
}

/* ── 设备 ───────────────────────────────────────────── */

function devRows(list) {
  return `
    <thead><tr>
      <th>状态</th><th>类型</th><th>地址</th><th>User-Agent</th>
      <th>请求</th><th>错误</th><th>最后请求</th><th>空闲</th>
    </tr></thead>
    <tbody>${list.map(x => `
      <tr class="dev-row" data-id="${x.id}" style="cursor:pointer">
        <td><span class="tag ${x.online ? 'on' : 'off'}">${x.online ? '活跃' : '离线'}</span></td>
        <td><span class="tag">${KIND_LABEL[x.kind] || esc(x.kind)}</span></td>
        <td class="mono">${esc(x.address)}</td>
        <td class="ua" title="${esc(x.user_agent)}">${esc((x.user_agent || '(无)').slice(0, 52))}</td>
        <td>${x.hits}</td>
        <td>${x.errors ? `<span class="bad">${x.errors}</span>` : 0}</td>
        <td class="mono">${esc((x.last_path || '—').slice(0, 34))}</td>
        <td>${ago(x.idle_seconds)}</td>
      </tr>`).join('')}
    </tbody>`;
}

async function renderDevices() {
  const d = await api('/devices');
  const s = d.summary;

  $('#dev-cards').innerHTML = [
    ['设备总数', s.total, '记录过的客户端', ''],
    ['当前活跃', s.online, '2 分钟内有请求', 'b'],
    ...Object.entries(s.by_kind || {}).map(([k, v]) =>
      [KIND_LABEL[k] || k, v, '按 User-Agent 分类', 'p']),
  ].map(([k, v, sub, cls]) => `
    <div class="card ${cls}">
      <div class="k">${k}</div>
      <div class="v">${Number(v || 0).toLocaleString()}</div>
      <div class="s">${sub}</div>
    </div>`).join('');

  $('#dev-table').innerHTML = d.devices.length
    ? devRows(d.devices)
    : '<tbody><tr><td class="empty">还没有客户端连接过</td></tr></tbody>';

  $$('.dev-row').forEach(tr => tr.addEventListener('click', () => {
    state.traceId = Number(tr.dataset.id);
    showTrace(d.devices.find(x => x.id === state.traceId));
  }));

  if (state.traceId) {
    const cur = d.devices.find(x => x.id === state.traceId);
    if (cur) showTrace(cur);
  }
}

function showTrace(dev) {
  if (!dev) return;
  $('#dev-trace-hint').textContent =
    `${dev.address} · ${KIND_LABEL[dev.kind] || dev.kind} · ${dev.hits} 次请求`;
  const lines = (dev.activity || []).slice().reverse().map(a => {
    const t = new Date(a.t * 1000).toLocaleTimeString('zh-CN');
    return `${t}  ${String(a.s).padStart(3)}  ${a.m} ${a.p}`;
  });
  $('#dev-trace').innerHTML = lines.length
    ? lines.map(l => `<span class="l ${/\s(4|5)\d\d\s/.test(l) ? 'err' : ''}">${esc(l)}</span>`).join('')
    : '<span class="l">（无活动记录）</span>';
}

/* ── 玩家 ───────────────────────────────────────────── */

async function renderPlayers() {
  const q = $('#player-search').value.trim();
  const d = await api('/players?limit=200' + (q ? '&q=' + encodeURIComponent(q) : ''));
  $('#player-count').textContent = `${d.players.length} / ${d.total}`;

  if (!d.players.length) {
    $('#player-table').innerHTML = '<tbody><tr><td class="empty">没有玩家</td></tr></tbody>';
    return;
  }
  $('#player-table').innerHTML = `
    <thead><tr>
      <th>状态</th><th>用户名</th><th>等级</th><th>金币</th><th>奖券</th>
      <th>胜负</th><th>莫蒂</th><th>最后在线</th><th>操作</th>
    </tr></thead>
    <tbody>${d.players.map(p => `
      <tr>
        <td><span class="tag ${p.online ? 'on' : 'off'}">${p.online ? '在线' : '离线'}</span></td>
        <td>${esc(p.username || '—')}</td>
        <td>${p.level ?? 0}</td>
        <td>${(p.coins ?? 0).toLocaleString()}</td>
        <td>${p.coupons ?? 0}</td>
        <td>${p.wins ?? 0} / ${p.losses ?? 0}</td>
        <td>${p.morty_count ?? 0}</td>
        <td>${p.last_seen ? esc(String(p.last_seen).slice(0, 16)) : '—'}</td>
        <td>
          <button class="btn xs" data-act="grant" data-pid="${esc(p.player_id)}">发资源</button>
          <button class="btn xs" data-act="heal"  data-pid="${esc(p.player_id)}">治疗</button>
          <button class="btn xs" data-act="kick"  data-pid="${esc(p.player_id)}">踢下线</button>
        </td>
      </tr>`).join('')}
    </tbody>`;

  $$('#player-table button[data-act]').forEach(b =>
    b.addEventListener('click', async () => {
      const act = b.dataset.act;
      const body = { player_id: b.dataset.pid, action: act };
      if (act === 'grant') { body.coins = 10000; body.coupons = 100; }
      const r = await post('/player/action', body);
      b.textContent = r.ok ? '✓' : '✗';
      setTimeout(renderPlayers, 900);
    }));
}

/* ── 房间 ───────────────────────────────────────────── */

async function renderRooms() {
  const d = await api('/rooms');
  $('#room-count').textContent = `${d.rooms.length} 个`;

  if (!d.rooms.length) {
    $('#room-list').innerHTML = '<div class="empty">还没有房间。用上面的「生成」按钮创建。</div>';
    return;
  }
  $('#room-list').innerHTML = d.rooms.map(r => `
    <div class="room">
      <div class="room-head">
        <span class="room-id">${esc(r.room_id)}</span>
        <span class="hint">world ${esc(r.world_id ?? '—')} · zone ${esc(r.zone_id ?? '—')}</span>
      </div>
      <div class="room-meta">
        <span>UDP <b>${esc(r.room_udp_host ?? '—')}:${esc(r.room_udp_port ?? '—')}</b></span>
        <span>拾取物 <b>${r.pickups}</b></span>
        <span>野生莫蒂 <b>${r.wild_morties}</b></span>
        <span>机器人 <b>${r.bots}</b></span>
        <span>事件 <b>${r.events}</b></span>
      </div>
      <div class="room-players">
        ${(r.players || []).length
          ? r.players.map(p =>
              `<span class="tag on">${esc(p.username || p.player_id.slice(0, 8))} · Lv${p.level ?? 0}</span>`).join('')
          : '<span class="tag off">房间内无人</span>'}
        <button class="btn xs" data-clear="${esc(r.room_id)}">清空事件</button>
      </div>
    </div>`).join('');

  $$('#room-list button[data-clear]').forEach(b =>
    b.addEventListener('click', async () => {
      await post('/rooms/clear', { room_id: b.dataset.clear });
      renderRooms();
    }));
}

/* ── 事件 ───────────────────────────────────────────── */

async function renderEvents() {
  const d = await api('/events?limit=120');
  $('#event-count').textContent = `${d.events.length} 条（最新在上）`;
  if (!d.events.length) {
    $('#event-table').innerHTML = '<tbody><tr><td class="empty">还没有事件</td></tr></tbody>';
    return;
  }
  $('#event-table').innerHTML = `
    <thead><tr><th>id</th><th>事件名</th><th>房间</th><th>目标玩家</th><th>载荷</th></tr></thead>
    <tbody>${d.events.map(e => {
      let p = e.payload_json || '';
      try { p = JSON.stringify(JSON.parse(p)); } catch (_) {}
      if (p.length > 140) p = p.slice(0, 140) + '…';
      return `<tr>
        <td class="mono">${e.id}</td>
        <td><span class="tag">${esc(e.event_name)}</span></td>
        <td class="mono">${esc((e.room_id || '—').slice(0, 10))}</td>
        <td class="mono">${esc((e.player_id || '—').slice(0, 10))}</td>
        <td class="mono">${esc(p)}</td>
      </tr>`;
    }).join('')}</tbody>`;
}

/* ── 设置 ───────────────────────────────────────────── */

async function renderSettings() {
  const d = await api('/settings');
  $('#settings-form').innerHTML = d.schema.map(s => {
    const id = `set-${s.key}`;
    if (s.kind === 'bool') {
      return `<label class="set-row" for="${id}">
        <span class="set-label">${esc(s.label)}<em>${esc(s.help || '')}</em></span>
        <input type="checkbox" id="${id}" data-key="${s.key}" data-kind="bool"
               ${String(s.value) === '1' ? 'checked' : ''}>
      </label>`;
    }
    const type = s.kind === 'int' ? 'number' : 'text';
    return `<label class="set-row" for="${id}">
      <span class="set-label">${esc(s.label)}<em>${esc(s.help || '')}</em></span>
      <input class="input" type="${type}" id="${id}" data-key="${s.key}"
             data-kind="${s.kind}" value="${esc(s.value)}">
    </label>`;
  }).join('');
}

async function saveSettings() {
  const values = {};
  $$('#settings-form [data-key]').forEach(el => {
    values[el.dataset.key] = el.dataset.kind === 'bool'
      ? (el.checked ? '1' : '0')
      : el.value;
  });
  const r = await post('/settings', { values });
  $('#settings-result').textContent =
    r.ok ? '✓ 已保存\n' + JSON.stringify(r.values, null, 2) : '✗ 保存失败';
  render();
}

/* ── 系统 ───────────────────────────────────────────── */

async function renderSystem() {
  const [sys, cfg] = await Promise.all([api('/system'), api('/config')]);
  const total = sys.tables.reduce((a, t) => a + (t.rows || 0), 0);
  $('#sys-hint').textContent = `${sys.tables.length} 张表 / ${total.toLocaleString()} 行`;
  $('#sys-table').innerHTML = `
    <thead><tr><th>表名 / 文件</th><th>行数 / 大小</th></tr></thead>
    <tbody>${sys.tables.map(t => `
      <tr><td class="mono">${esc(t.table)}</td><td>${(t.rows || 0).toLocaleString()}</td></tr>
    `).join('')}
    ${sys.files.map(f => `
      <tr><td class="mono">${esc(f.path)}</td>
          <td>${(f.bytes / 1024).toFixed(1)} KB</td></tr>`).join('')}
    </tbody>`;
  $('#config-view').textContent = JSON.stringify(cfg, null, 2);
}

/* ── 日志 ───────────────────────────────────────────── */

async function renderConsole() {
  const d = await api('/console?limit=220');
  $('#console-src').textContent =
    d.source ? d.source.split('/').slice(-2).join('/') : '（无日志文件）';
  const el = $('#console-view');
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  el.innerHTML = d.lines.length
    ? d.lines.map(l => {
        const cls = /\s(4\d\d|5\d\d)\s/.test(l) ? 'err' : (/\s200\s/.test(l) ? 'ok' : '');
        return `<span class="l ${cls}">${esc(l)}</span>`;
      }).join('')
    : '<span class="l">（没有日志。用「一键建服」启动会写入 logs/server.log）</span>';
  if (atBottom) el.scrollTop = el.scrollHeight;
}

/* ── 调度 ───────────────────────────────────────────── */

const RENDERERS = {
  overview: renderOverview, devices: renderDevices, players: renderPlayers,
  rooms: renderRooms, events: renderEvents, settings: renderSettings,
  system: renderSystem, console: renderConsole,
};

async function render() {
  const fn = RENDERERS[state.page];
  if (!fn) return;
  try {
    await fn();
    $('#conn-dot').className = 'status-dot on';
    $('#conn-text').textContent = '已连接';
  } catch (e) {
    $('#conn-dot').className = 'status-dot off';
    $('#conn-text').textContent = '连接失败';
    console.error(e);
  }
}

/* ── 事件绑定 ───────────────────────────────────────── */

$$('.nav-item').forEach(el => el.addEventListener('click', () => goto(el.dataset.page)));
$('#btn-refresh').addEventListener('click', render);

$('#player-search').addEventListener('input', () => {
  clearTimeout(window.__pq);
  window.__pq = setTimeout(renderPlayers, 250);
});

$('#btn-seed').addEventListener('click', async () => {
  const out = $('#seed-result');
  out.textContent = '正在生成…';
  out.textContent = JSON.stringify(await post('/rooms/seed', {}), null, 2);
});

$('#btn-seed-custom').addEventListener('click', async () => {
  const out = $('#seed-custom-result');
  out.textContent = '正在生成…';
  out.textContent = JSON.stringify(await post('/rooms/seed', {
    pickups: Number($('#seed-pickups').value),
    wilds: Number($('#seed-wilds').value),
    bots: Number($('#seed-bots').value),
  }), null, 2);
});

$('#btn-seed-force').addEventListener('click', async () => {
  const out = $('#seed-custom-result');
  out.textContent = '正在清空旧房间并重建…';
  const rooms = (await api('/rooms')).rooms;
  for (const r of rooms) await post('/rooms/clear', { room_id: r.room_id });
  out.textContent = JSON.stringify(await post('/rooms/seed', {
    pickups: Number($('#seed-pickups').value),
    wilds: Number($('#seed-wilds').value),
    bots: Number($('#seed-bots').value),
  }), null, 2);
});

$('#btn-save-settings').addEventListener('click', saveSettings);

goto('overview');
state.timer = setInterval(() => {
  if (state.page === 'console' && !$('#auto-tail').checked) return;
  render();
}, 4000);
