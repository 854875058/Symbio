/* ============================================
   Symbio UI — 微信 Bridge 页：登录二维码、状态轮询、消息试发
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ WeChat Bridge Page ============
const wxState = { pollTimer: null };
const WX_STATUS = {
  logged_out:   { label: '未连接',       cls: 'out' },
  waiting_scan: { label: '待扫码',       cls: 'wait' },
  scanned:      { label: '已扫码待确认', cls: 'scan' },
  logged_in:    { label: '已绑定',       cls: 'in' },
  failed:       { label: '绑定失败',     cls: 'fail' },
};

async function loadWeChat() {
  await refreshWeChatStatus();
  await refreshWeChatMessages();
  // 轮询登录态 + 消息流，扫码后/收发时自动刷新
  if (wxState.pollTimer) clearInterval(wxState.pollTimer);
  wxState.pollTimer = setInterval(async () => {
    const active = document.getElementById('page-wechat')?.classList.contains('active');
    if (!active) { clearInterval(wxState.pollTimer); wxState.pollTimer = null; return; }
    await refreshWeChatStatus();
    await refreshWeChatMessages();
  }, 3000);
}

async function refreshWeChatMessages() {
  const container = document.getElementById('wx-stream');
  const countEl = document.getElementById('wx-stream-count');
  if (!container) return;
  try {
    const res = await fetch(`${API}/wechat/messages?limit=40`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const msgs = data.messages || [];
    if (countEl) countEl.textContent = `${data.total || msgs.length} 条`;
    if (!msgs.length) {
      container.innerHTML = '<div class="cost-table-empty">还没有消息 —— 让好友给机器人发一条试试</div>';
      return;
    }
    container.innerHTML = msgs.map(m => {
      const inbound = m.direction === 'in';
      const t = (m.at || '').slice(11, 19);
      const kind = m.kind ? `<span class="wx-msg-kind">${esc(m.kind)}</span>` : '';
      return `<div class="wx-msg ${inbound ? 'in' : 'out'}">
        <div class="wx-msg-meta"><span class="wx-msg-dir">${inbound ? '收' : '发'}</span><span class="wx-msg-user" title="${esc(m.user)}">${esc(m.user)}</span>${kind}<span class="wx-msg-time">${t}</span></div>
        <div class="wx-msg-text">${esc(m.text)}</div>
      </div>`;
    }).join('');
  } catch (e) {
    if (countEl) countEl.textContent = '加载失败';
  }
}

async function refreshWeChatStatus() {
  try {
    const res = await fetch(`${API}/wechat/login/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const state = await res.json();
    renderWeChatLogin(state);
    return state;
  } catch (e) {
    console.warn('加载微信状态失败:', e.message);
    return null;
  }
}

// 二维码渲染适配器（基于本地 vendored qrcodejs）
window.QRCanvas = {
  render(el, text, size) {
    if (!el || !window.QRCode) return;
    el.innerHTML = '';
    try {
      new window.QRCode(el, { text, width: size, height: size, correctLevel: window.QRCode.CorrectLevel.M });
    } catch (e) { console.warn('QR 渲染失败:', e.message); }
  },
};

function renderWeChatLogin(state) {
  const meta = WX_STATUS[state.status] || WX_STATUS.logged_out;
  const badge = document.getElementById('wx-status-badge');
  if (badge) { badge.textContent = meta.label + (state.user ? ` · ${state.user}` : ''); badge.className = `wx-status-badge ${meta.cls}`; }

  const empty = document.getElementById('wx-qr-empty');
  const img = document.getElementById('wx-qr-img');
  const canvas = document.getElementById('wx-qr-canvas');
  const link = document.getElementById('wx-qr-link');
  const bindStatus = document.getElementById('wx-bind-status');
  const loginBtn = document.getElementById('btn-wx-login');
  const logoutBtn = document.getElementById('btn-wx-logout');

  const show = (el, on) => { if (el) el.style.display = on ? '' : 'none'; };

  if (state.status === 'logged_in') {
    show(empty, false); show(img, false); show(canvas, false); show(link, false);
    show(loginBtn, false); show(logoutBtn, true);
    if (bindStatus) bindStatus.innerHTML = `<div class="wx-bound">✅ 已绑定微信账号：<b>${esc(state.user || '(未知)')}</b> · 后台正在收发消息</div>`;
    return;
  }
  show(logoutBtn, false);
  show(loginBtn, true);
  if (loginBtn) loginBtn.textContent = (state.status === 'waiting_scan' || state.status === 'scanned') ? '重新拉取二维码' : '开始扫码登录';
  if (bindStatus) {
    if (state.status === 'scanned') bindStatus.innerHTML = '<div class="wx-scanned">📱 已扫码，请在手机上确认登录…</div>';
    else if (state.status === 'failed') bindStatus.innerHTML = '<div class="wx-failed">⚠️ 登录失败或二维码过期，请重试</div>';
    else bindStatus.innerHTML = '';
  }

  if (state.qr_image) {
    // 外部 bridge 直接给图
    show(empty, false); show(link, false); show(canvas, false);
    if (img) { img.src = state.qr_image; show(img, true); }
  } else if (state.qr) {
    // 内置 iLink：拿到二维码内容字符串，前端渲染成二维码
    show(empty, false); show(img, false); show(link, false);
    if (canvas && window.QRCanvas) {
      show(canvas, true);
      window.QRCanvas.render(canvas, state.qr, 220);
    } else if (link) {
      link.innerHTML = `<div class="wx-qr-string">二维码内容：</div><code>${esc(state.qr)}</code>`;
      show(link, true);
    }
  } else {
    show(img, false); show(canvas, false); show(link, false); show(empty, true);
  }
}

let _wxPollTimer = null;
function startWeChatLoginPoll() {
  if (_wxPollTimer) clearInterval(_wxPollTimer);
  _wxPollTimer = setInterval(async () => {
    const state = await refreshWeChatStatus();
    // 终态停止轮询
    if (state && (state.status === 'logged_in' || state.status === 'failed')) {
      clearInterval(_wxPollTimer); _wxPollTimer = null;
    }
  }, 2500);
}

document.getElementById('btn-wx-login')?.addEventListener('click', async () => {
  const btn = document.getElementById('btn-wx-login');
  if (btn) { btn.disabled = true; btn.textContent = '拉取中…'; }
  try {
    const res = await fetch(`${API}/wechat/login/start`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const body = await res.json();
    renderWeChatLogin(body.login || {});
    startWeChatLoginPoll();
    toast('success', '已拉取二维码', '请用微信扫码并在手机确认');
  } catch (e) {
    toast('error', '扫码登录失败', e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
});

document.getElementById('btn-wx-logout')?.addEventListener('click', async () => {
  try {
    await fetch(`${API}/wechat/logout`, { method: 'POST' });
    if (_wxPollTimer) { clearInterval(_wxPollTimer); _wxPollTimer = null; }
    await refreshWeChatStatus();
    toast('success', '已登出', '微信收发已停止');
  } catch (e) {
    toast('error', '登出失败', e.message);
  }
});

document.getElementById('btn-refresh-wechat')?.addEventListener('click', refreshWeChatStatus);

document.getElementById('btn-wx-test-send')?.addEventListener('click', async () => {
  const to = document.getElementById('wx-test-user')?.value.trim();
  const content = document.getElementById('wx-test-content')?.value.trim();
  const resEl = document.getElementById('wx-test-result');
  if (!to || !content) { toast('error', '请填写完整', '目标用户和消息内容都要填'); return; }
  try {
    const res = await fetch(`${API}/wechat/send`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to_user: to, content }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (resEl) {
      if (data.delivery_status === 'sent') {
        resEl.textContent = data.via === 'ilink' ? '✓ 已通过微信 iLink 发送' : '✓ 已通过 bridge 发送';
      } else if (data.delivery_status === 'error') {
        resEl.textContent = '✗ iLink 发送失败：' + (data.error || JSON.stringify(data.response || {}));
      } else {
        resEl.textContent = '已就绪（未登录 iLink 且未配置 send_endpoint，内容随响应返回）';
      }
    }
  } catch (e) {
    if (resEl) resEl.textContent = '发送失败: ' + e.message;
  }
});
