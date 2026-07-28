/* ============================================
   Symbio UI — Computer Use 页：浏览器会话、动作与回放
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Computer Use Page ============
const cuState = { activeId: null };

async function loadComputerUse() {
  try {
    const res = await fetch(`${API}/computer-use/sessions`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const badge = document.getElementById('cu-mode-badge');
    if (badge) {
      const live = data.playwright_available;
      badge.textContent = live ? '● Playwright 实时模式' : '○ Dry-run 模式（未装 Playwright）';
      badge.className = 'cu-mode-badge ' + (live ? 'live' : 'dry');
    }
    renderCuSessions(data.sessions || []);
    if (cuState.activeId) await loadCuSession(cuState.activeId);
  } catch (e) {
    console.warn('加载 Computer Use 失败:', e.message);
  }
}

function renderCuSessions(sessions) {
  const container = document.getElementById('cu-sessions');
  if (!container) return;
  if (!sessions.length) {
    container.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">暂无浏览器会话</p>
        <p class="empty-block-hint">Computer Use 让 Agent 真的去操作一个浏览器：点按钮、填表单、读页面。适合那些没有 API、只能靠界面完成的事。上方「新建会话」起一个受控浏览器，之后每一步动作都会留痕。</p>
        <div class="empty-block-actions">
          <button class="btn-primary" type="button" onclick="document.getElementById('btn-cu-create')?.click()">创建会话</button>
        </div>
      </div>`;
    return;
  }
  container.innerHTML = sessions.map(s => `
    <div class="cu-session-item ${s.session_id === cuState.activeId ? 'active' : ''}" data-id="${esc(s.session_id)}">
      <span class="cu-session-id">${esc(s.session_id)}</span>
      <span class="cu-session-url">${esc(s.current_url || '(空白)')}</span>
      <span class="cu-session-meta">${s.step_count} 步 · ${s.status}</span>
    </div>`).join('');
  container.querySelectorAll('.cu-session-item').forEach(el => {
    el.addEventListener('click', () => { cuState.activeId = el.dataset.id; loadCuSession(el.dataset.id); loadComputerUse(); });
  });
}

async function loadCuSession(sessionId) {
  const panel = document.getElementById('cu-active-panel');
  try {
    const res = await fetch(`${API}/computer-use/sessions/${sessionId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cuState.activeId = sessionId;
    if (panel) panel.style.display = 'block';
    const idEl = document.getElementById('cu-active-id');
    const urlEl = document.getElementById('cu-active-url');
    if (idEl) idEl.textContent = sessionId;
    if (urlEl) urlEl.textContent = data.current_url || '(空白页)';
    renderCuTimeline(data.steps || []);
  } catch (e) {
    if (panel) panel.style.display = 'none';
  }
}

function renderCuTimeline(steps) {
  const container = document.getElementById('cu-timeline');
  if (!container) return;
  if (!steps.length) {
    container.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">这个会话还没有任何动作</p>
        <p class="empty-block-hint">会话已经开着，但浏览器停在起始页。用下方「执行动作」手动走一步（navigate / click / type…），或者填一个目标让它自己规划。每一步的耗时、成败和截图都会按顺序记在这里，也是之后「回放」的依据。</p>
      </div>`;
    return;
  }
  container.innerHTML = steps.map(s => {
    const shot = s.result && s.result.screenshot;
    const detail = s.error || (s.result && (s.result.text || s.result.navigated_to || s.result.note)) || JSON.stringify(s.params || {});
    return `<div class="cu-step ${s.success ? '' : 'failed'}">
      <span class="cu-step-num">${s.index + 1}</span>
      <span class="cu-step-action">${esc(s.action)}</span>
      <span class="cu-step-detail" title="${esc(String(detail))}">${esc(String(detail).substring(0, 80))}</span>
      <span class="cu-step-status">${s.success ? '✓' : '✗'} ${s.elapsed_ms}ms</span>
      ${shot ? `<span class="cu-step-shot">📷</span>` : ''}
    </div>`;
  }).join('');
}

document.getElementById('btn-cu-create')?.addEventListener('click', async () => {
  const url = document.getElementById('cu-start-url')?.value.trim() || '';
  // 创建会话会在本机拉起一个真浏览器，之后 Agent 的点击/输入都作用在真实站点上：
  // 表单会真的提交，按钮会真的被按。这类「动作发生在 Symbio 之外」的入口
  // 必须先确认，否则用户会以为自己只是开了个预览窗口。
  if (!confirmDanger('创建浏览器会话？', `${url ? `起始地址：${url}\n\n` : ''}会话会在本机启动一个受控浏览器，Agent 之后的点击、输入、跳转都作用在真实网站上——表单会真的提交，操作不可撤销。如果目标站点已登录，Agent 就带着你的登录态在操作。\n\n若本机没有安装 Playwright，会退化为 dry-run（只记录动作、不真正操作），创建后的提示会说明当前是哪种模式。`)) {
    return;
  }
  try {
    const res = await fetch(`${API}/computer-use/sessions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_url: url }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cuState.activeId = data.session_id;
    toast('success', '会话已创建', data.dry_run ? 'Dry-run 模式' : 'Playwright 实时模式');
    await loadComputerUse();
    await loadCuSession(data.session_id);
  } catch (e) { toast('error', '创建失败', e.message); }
});

async function cuAct(action, params) {
  if (!cuState.activeId) return;
  try {
    const res = await fetch(`${API}/computer-use/sessions/${cuState.activeId}/act`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, params }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadCuSession(cuState.activeId);
  } catch (e) { toast('error', '动作失败', e.message); }
}

document.getElementById('btn-cu-act')?.addEventListener('click', () => {
  const action = document.getElementById('cu-action-type')?.value;
  const raw = document.getElementById('cu-action-param')?.value.trim() || '';
  const params = {};
  if (action === 'navigate') params.url = raw;
  else if (action === 'type') params.text = raw;
  else if (action === 'click') { if (raw) params.selector = raw; }
  else if (action === 'extract_text') { if (raw) params.selector = raw; }
  else if (action === 'scroll') params.dy = parseInt(raw) || 400;
  else if (action === 'wait') params.ms = parseInt(raw) || 500;
  cuAct(action, params);
});

document.getElementById('btn-cu-plan')?.addEventListener('click', async () => {
  if (!cuState.activeId) return;
  const goal = document.getElementById('cu-goal')?.value.trim() || '';
  const useLlm = document.getElementById('cu-use-llm')?.checked || false;
  try {
    const res = await fetch(`${API}/computer-use/sessions/${cuState.activeId}/plan`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal, auto_execute: true, use_llm: useLlm }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const tag = { llm: '🧠 LLM', 'heuristic-fallback': '↩ 回退启发式', heuristic: '启发式' }[data.plan.planner] || '';
    toast('info', `规划：${data.plan.action} ${tag}`, data.plan.reason || '');
    await loadCuSession(cuState.activeId);
  } catch (e) { toast('error', '规划失败', e.message); }
});

document.getElementById('btn-cu-replay')?.addEventListener('click', async () => {
  if (!cuState.activeId) return;
  try {
    const res = await fetch(`${API}/computer-use/sessions/${cuState.activeId}/replay`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    toast('success', '回放完成', `${data.succeeded}/${data.replayed_steps} 步成功`);
    await loadCuSession(cuState.activeId);
  } catch (e) { toast('error', '回放失败', e.message); }
});

document.getElementById('btn-cu-close')?.addEventListener('click', async () => {
  if (!cuState.activeId) return;
  try {
    await fetch(`${API}/computer-use/sessions/${cuState.activeId}`, { method: 'DELETE' });
    toast('success', '会话已关闭', '审计已持久化');
    cuState.activeId = null;
    const panel = document.getElementById('cu-active-panel');
    if (panel) panel.style.display = 'none';
    await loadComputerUse();
  } catch (e) { toast('error', '关闭失败', e.message); }
});

document.getElementById('btn-refresh-cu')?.addEventListener('click', loadComputerUse);
