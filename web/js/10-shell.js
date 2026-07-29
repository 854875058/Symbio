/* ============================================
   Symbio UI — 外壳：页面路由、侧栏折叠/分组、命令面板、模态框无障碍、快捷键、状态栏
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Navigation ============
const PAGE_TITLES = {
  chat: '对话', tasks: '任务监控', models: '模型配置', memory: '记忆管理',
  ontology: '本体图谱', skills: 'Skills', dashboard: '仪表盘',
  capabilities: '能力账本', evolution: '数据飞轮', sandbox: '沙箱执行',
  'external-agents': '外部 Agent', hitl: '审批中心', a2a: 'A2A 协议', mcp: 'MCP 工具网关',
  security: '安全防火墙', 'computer-use': 'Computer Use', wechat: '微信机器人',
  workbench: 'Agent 工作台',
};

/**
 * 切换页面。
 *
 * @param {string} name 页面名（对应 #page-<name> 与 nav-tab 的 data-page）
 * @param {{ updateHash?: boolean }} [opts]
 *        updateHash 为 false 时不回写 location.hash——用于响应 hashchange，
 *        否则会形成"改 hash → 触发切页 → 又改 hash"的回环。
 */
async function switchPage(name, opts = {}) {
  const { updateHash = true } = opts;
  // 未知页面名一律落回对话页：hash 是可以被手输/被旧书签带进来的，
  // 不校验的话会切到一个所有 .page 都不 active 的空白界面。
  if (!PAGE_TITLES[name]) name = 'chat';

  state.page = name;
  if (updateHash) {
    // 写 location.hash 会自动进浏览器历史，于是前进/后退天然可用。
    // 相等时不写：否则同一页的重复点击会堆出一串无意义的后退记录。
    const target = `#/${name}`;
    if (location.hash !== target) location.hash = target;
  }
  dom.navTabs.forEach(t => {
    const on = t.dataset.page === name;
    t.classList.toggle('active', on);
    // 读屏用户靠 aria-current 知道"当前在哪一页"，.active 类只是视觉
    if (on) t.setAttribute('aria-current', 'page');
    else t.removeAttribute('aria-current');
  });
  dom.pages.forEach(p => p.classList.toggle('active', p.id === `page-${name}`));
  revealActiveNavGroup();

  // Update topbar title
  if (dom.topbarTitle) dom.topbarTitle.textContent = PAGE_TITLES[name] || name;
  // 同步文档标题：浏览器的历史列表、书签、标签页都只显示 document.title。
  // 不改的话后退菜单里会是一串一模一样的"Symbio"，认不出哪条是哪页。
  document.title = name === 'chat' ? 'Symbio' : `${PAGE_TITLES[name]} · Symbio`;

  // 会话列表抽屉只属于对话页；切走时收起并隐藏入口
  const sessBtn = $('#topbar-sessions-toggle');
  if (sessBtn) sessBtn.dataset.available = name === 'chat' ? '1' : '0';
  if (name !== 'chat') setSessionsOpen(false);

  // Lazy load: only load data on first visit per page (avoids re-fetching on tab switch)
  if (name === 'models' && !state.pagesLoaded.models) { state.pagesLoaded.models = true; await loadModels(); await loadConfig(); }
  else if (name === 'models') { await loadModels(); }
  if (name === 'tasks' && !state.pagesLoaded.tasks) { state.pagesLoaded.tasks = true; await loadTasks(); }
  if (name === 'memory' && !state.pagesLoaded.memory) { state.pagesLoaded.memory = true; await loadMemories(); }
  if (name === 'ontology' && !state.pagesLoaded.ontology) { state.pagesLoaded.ontology = true; await loadOntology(); }
  else if (name === 'ontology') { await loadOntology(); }
  if (name === 'skills' && !state.pagesLoaded.skills) { state.pagesLoaded.skills = true; await loadSkills(); }
  else if (name === 'skills' && state.skillMode === 'marketplace') { await loadMarketplace(dom.skillsSearch?.value.trim() || undefined); }
  if (name === 'dashboard') await loadDashboard();
  if (name === 'capabilities') await loadCapabilities();
  if (name === 'evolution') await loadEvolution();
  if (name === 'sandbox') await loadSandbox();
  if (name === 'workbench') await loadWorkbench();
  if (name === 'hitl') { await loadHitl(); await loadHitlChannels(); await loadHitlTimeoutPolicy(); }
  if (name === 'mcp') await loadMCP();
  if (name === 'a2a') await loadA2A();
  if (name === 'security') await loadSecurity();
  if (name === 'computer-use') await loadComputerUse();
  if (name === 'wechat') await loadWeChat();
  if (name !== 'wechat' && wxState.pollTimer) { clearInterval(wxState.pollTimer); wxState.pollTimer = null; }
}

dom.navTabs.forEach(tab => {
  tab.addEventListener('click', () => switchPage(tab.dataset.page));
});

// ============ Hash 路由 ============
// 页面状态此前只活在 DOM class 里：刷新回到对话页，浏览器前进/后退按钮
// 完全失效，也没法把「我正在看审批中心」这个位置收藏或发给别人。
// 用 hash 而非 History API 是因为整个 UI 是 /static/index.html 下的单文件，
// pushState 改出来的路径刷新后会 404（静态服务器不会把任意路径回落到 index）。

/** 从 location.hash 解析页面名。支持 #/tasks 与 #tasks 两种写法。 */
function pageFromHash() {
  const raw = decodeURIComponent(location.hash.replace(/^#\/?/, '')).trim();
  return raw && PAGE_TITLES[raw] ? raw : null;
}

window.addEventListener('hashchange', () => {
  const name = pageFromHash();
  if (!name) {
    // hash 无效（手输错了、旧书签）。留在当前页，但把 hash 改回真实位置——
    // 否则地址栏写着 #/does-not-exist、界面显示的却是别的页，URL 在说谎。
    const target = `#/${state.page}`;
    if (location.hash !== target) location.replace(target);
    return;
  }
  // 已经在这一页就不重复切（hash 由 switchPage 自己写入时会走到这里）
  if (name === state.page) return;
  switchPage(name, { updateHash: false });
});

/** 首屏按 hash 恢复页面。没有 hash 或 hash 无效时留在对话页。 */
function initRouter() {
  const name = pageFromHash();
  if (name) {
    if (name !== 'chat') switchPage(name, { updateHash: false });
    return;
  }
  // 带着无效 hash 进来（旧书签、手输错）：用 replaceState 把它抹掉，
  // 不留历史条目——用户没主动导航过，不该在后退栈里凭空多一格。
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
}

// 侧栏内方向键在可见导航项之间移动焦点（跨分组连续），Home/End 跳首尾
document.querySelector('.sidebar-nav')?.addEventListener('keydown', (e) => {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
  const visible = Array.from(document.querySelectorAll('.sidebar-nav .nav-tab'))
    .filter(el => el.offsetWidth || el.offsetHeight);
  if (!visible.length) return;
  const cur = visible.indexOf(document.activeElement);
  let next;
  if (e.key === 'Home') next = 0;
  else if (e.key === 'End') next = visible.length - 1;
  else if (cur === -1) next = 0;
  else next = (cur + (e.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length;
  e.preventDefault();
  visible[next].focus();
});

// ============ Sidebar Collapse ============
// 窄屏（<=600px）下侧边栏和会话列表都是浮层抽屉，由 .sidebar-expanded /
// .sessions-open 两个类驱动；宽屏下 .sidebar-collapsed 控制图标条模式。
const MOBILE_QUERY = '(max-width: 600px)';

function isMobileLayout() {
  return window.matchMedia(MOBILE_QUERY).matches;
}

function setSidebarCollapsed(collapsed, { persist = true } = {}) {
  state.sidebarCollapsed = collapsed;
  // 手机端的强制收起不写 localStorage，否则会覆盖桌面端的展开偏好
  if (persist) localStorage.setItem('symbio-sidebar-collapsed', collapsed ? '1' : '0');
  dom.appRoot?.classList.toggle('sidebar-collapsed', collapsed);
  // 抽屉的展开态与折叠态互为反面；宽屏下这个类不参与布局，加着无副作用。
  dom.appRoot?.classList.toggle('sidebar-expanded', !collapsed);
  const btn = dom.sidebarCollapseBtn;
  if (btn) {
    btn.title = collapsed ? '展开侧边栏' : '收起侧边栏';
    const svg = btn.querySelector('svg');
    if (svg) svg.style.transform = collapsed ? 'rotate(180deg)' : '';
  }
  const menuToggle = $('#topbar-menu-toggle');
  // 手机端汉堡键常驻（抽屉收起后需要有入口再打开），宽屏保持原有行为。
  if (menuToggle) {
    menuToggle.style.display = collapsed || isMobileLayout() ? 'flex' : 'none';
  }
}

function setSessionsOpen(open) {
  dom.appRoot?.classList.toggle('sessions-open', open);
}

function closeMobileDrawers() {
  setSessionsOpen(false);
  if (isMobileLayout()) setSidebarCollapsed(true, { persist: false });
}

dom.sidebarCollapseBtn?.addEventListener('click', () => setSidebarCollapsed(!state.sidebarCollapsed));
$('#topbar-menu-toggle')?.addEventListener('click', () => {
  setSessionsOpen(false);
  setSidebarCollapsed(false, { persist: !isMobileLayout() });
});
$('#topbar-sessions-toggle')?.addEventListener('click', () => {
  const willOpen = !dom.appRoot?.classList.contains('sessions-open');
  setSidebarCollapsed(true, { persist: !isMobileLayout() });
  setSessionsOpen(willOpen);
});
$('#sidebar-scrim')?.addEventListener('click', closeMobileDrawers);

// 手机端点导航项后自动收起抽屉，否则内容被浮层挡住
document.querySelectorAll('.nav-tab').forEach(item => {
  item.addEventListener('click', () => {
    if (isMobileLayout()) closeMobileDrawers();
  });
});

// 视口跨过断点时同步一次，避免旋转屏幕后残留错误状态
window.matchMedia(MOBILE_QUERY).addEventListener('change', (e) => {
  setSessionsOpen(false);
  if (e.matches) {
    setSidebarCollapsed(true, { persist: false });
  } else {
    // 回到宽屏时恢复用户此前保存的偏好
    setSidebarCollapsed(localStorage.getItem('symbio-sidebar-collapsed') === '1');
  }
});

// ============ 侧栏分组折叠 ============
// 17 个页面挤在一列里要滚动才能看全。分组可折叠，状态存 localStorage；
// 但当前页所在的分组强制展开，否则会出现"高亮项被折叠隐藏"的矛盾状态。
const NAV_GROUPS_KEY = 'symbio-nav-collapsed';

function collapsedGroups() {
  try {
    const raw = JSON.parse(localStorage.getItem(NAV_GROUPS_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch { return new Set(); }
}

function setNavGroup(group, expanded, { persist = true } = {}) {
  const wrap = document.querySelector(`.nav-group[data-group="${group}"]`);
  if (!wrap) return;
  wrap.querySelector('.nav-group-label')?.setAttribute('aria-expanded', String(expanded));
  const items = wrap.querySelector('.nav-group-items');
  if (items) items.hidden = !expanded;
  if (!persist) return;
  const set = collapsedGroups();
  if (expanded) set.delete(group); else set.add(group);
  localStorage.setItem(NAV_GROUPS_KEY, JSON.stringify([...set]));
}

function initNavGroups() {
  const collapsed = collapsedGroups();
  document.querySelectorAll('.nav-group[data-group]').forEach(wrap => {
    const g = wrap.dataset.group;
    setNavGroup(g, !collapsed.has(g), { persist: false });
    wrap.querySelector('.nav-group-label')?.addEventListener('click', () => {
      const label = wrap.querySelector('.nav-group-label');
      setNavGroup(g, label.getAttribute('aria-expanded') !== 'true');
    });
  });
  revealActiveNavGroup();
}

// 当前页所在分组必须可见（不写回 localStorage，用户的折叠偏好保留）
function revealActiveNavGroup() {
  const tab = document.querySelector('.nav-tab.active');
  const wrap = tab?.closest('.nav-group[data-group]');
  if (wrap) setNavGroup(wrap.dataset.group, true, { persist: false });
}

// ============ 命令面板（Ctrl/⌘+K）============
const cmdk = { open: false, items: [], index: 0, opener: null };

function cmdkSource() {
  // 直接从导航 DOM 取，页面增减时不需要再维护一份清单
  return Array.from(document.querySelectorAll('.nav-tab[data-page]')).map(tab => ({
    page: tab.dataset.page,
    label: tab.querySelector('.nav-label')?.textContent.trim() || tab.dataset.page,
    hint: tab.getAttribute('title') || '',
    group: tab.closest('.nav-group')?.querySelector('.nav-group-label span')?.textContent.trim() || '',
  }));
}

// 子序列模糊匹配：'cu' 能命中 'Computer Use'，返回命中位置用于高亮
function fuzzyMatch(text, query) {
  if (!query) return [];
  const t = text.toLowerCase();
  const q = query.toLowerCase();
  const hits = [];
  let i = 0;
  for (const ch of q) {
    const at = t.indexOf(ch, i);
    if (at === -1) return null;
    hits.push(at);
    i = at + 1;
  }
  return hits;
}

function highlight(text, hits) {
  if (!hits || !hits.length) return esc(text);
  const set = new Set(hits);
  return [...text].map((ch, i) => (set.has(i) ? `<mark>${esc(ch)}</mark>` : esc(ch))).join('');
}

function cmdkRender(query) {
  const q = query.trim();
  const scored = [];
  for (const it of cmdkSource()) {
    const hay = `${it.label} ${it.page} ${it.hint}`;
    const onLabel = fuzzyMatch(it.label, q);
    const m = onLabel || fuzzyMatch(hay, q);
    if (q && !m) continue;
    // 标签命中优先，其次命中位置越靠前越好
    scored.push({ ...it, hits: onLabel, rank: (onLabel ? 0 : 100) + (m?.[0] ?? 0) });
  }
  scored.sort((a, b) => a.rank - b.rank);
  cmdk.items = scored;
  cmdk.index = 0;

  const list = $('#cmdk-list');
  if (!list) return;
  if (!scored.length) {
    list.innerHTML = '<li class="cmdk-empty">没有匹配的页面</li>';
    $('#cmdk-input')?.setAttribute('aria-activedescendant', '');
    return;
  }
  list.innerHTML = scored.map((it, i) => `
    <li class="cmdk-item" id="cmdk-opt-${i}" role="option" data-page="${esc(it.page)}"
        aria-selected="${i === 0 ? 'true' : 'false'}">
      <span>${highlight(it.label, it.hits)}</span>
      <span class="cmdk-item-group">${esc(it.group)}</span>
    </li>`).join('');
  cmdkSelect(0);
  list.querySelectorAll('.cmdk-item').forEach((li, i) => {
    li.addEventListener('mouseenter', () => cmdkSelect(i));
    li.addEventListener('click', () => cmdkConfirm(i));
  });
}

function cmdkSelect(i) {
  const list = $('#cmdk-list');
  const opts = list?.querySelectorAll('.cmdk-item') || [];
  if (!opts.length) return;
  cmdk.index = (i + opts.length) % opts.length;
  opts.forEach((li, n) => li.setAttribute('aria-selected', String(n === cmdk.index)));
  const cur = opts[cmdk.index];
  cur.scrollIntoView({ block: 'nearest' });
  $('#cmdk-input')?.setAttribute('aria-activedescendant', cur.id);
}

function cmdkConfirm(i) {
  const it = cmdk.items[i ?? cmdk.index];
  if (!it) return;
  cmdkClose();
  switchPage(it.page);
}

function cmdkOpen() {
  const overlay = $('#cmdk-overlay');
  if (!overlay) return;
  cmdk.opener = document.activeElement;
  cmdk.open = true;
  overlay.hidden = false;
  const input = $('#cmdk-input');
  if (input) { input.value = ''; input.focus(); }
  cmdkRender('');
}

function cmdkClose() {
  const overlay = $('#cmdk-overlay');
  if (!overlay) return;
  overlay.hidden = true;
  cmdk.open = false;
  const el = cmdk.opener;
  cmdk.opener = null;
  if (el?.isConnected && typeof el.focus === 'function') {
    try { el.focus({ preventScroll: true }); } catch { /* 忽略 */ }
  }
}

function initCmdk() {
  $('#cmdk-trigger')?.addEventListener('click', cmdkOpen);
  // macOS 显示 ⌘K，其他平台 Ctrl K
  if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
    const kbd = $('#cmdk-trigger-kbd');
    if (kbd) kbd.textContent = '⌘ K';
  }
  $('#cmdk-input')?.addEventListener('input', (e) => cmdkRender(e.target.value));
  $('#cmdk-overlay')?.addEventListener('click', (e) => {
    if (e.target.id === 'cmdk-overlay') cmdkClose();
  });
  $('#cmdk-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdkSelect(cmdk.index + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkSelect(cmdk.index - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); cmdkConfirm(); }
    else if (e.key === 'Escape') { e.preventDefault(); cmdkClose(); }
    else if (e.key === 'Tab') e.preventDefault(); // 面板里只有一个输入框，别把焦点漏到背后
  });
}

// ============ 模态框无障碍（角色标注 + 焦点管理 + Tab 捕获）============
// 8 处弹窗各自 appendChild(overlay)，而 overlay.remove() 散落在 20 多个地方。
// 与其改 30 处调用，这里在 body 上挂一个 MutationObserver 统一接管：
// 新增 .modal-overlay 时补齐 role/aria-modal/aria-labelledby、记住触发元素并把
// 焦点移进弹窗；最后一个弹窗移除时把焦点还给触发元素。
const modalA11y = { opener: null, seq: 0 };

const FOCUSABLE_SEL =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusablesIn(root) {
  return Array.from(root.querySelectorAll(FOCUSABLE_SEL))
    .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0);
}

// 当前可见的弹窗容器（动态 .modal-overlay 或静态目录选择器）
function activeModalBox() {
  const dyn = document.querySelector('.modal-overlay:last-of-type .modal');
  if (dyn) return dyn;
  const dp = $('#dirpicker-overlay');
  if (dp && dp.style.display !== 'none') return dp.querySelector('.dirpicker-modal');
  return null;
}

function enhanceModal(box) {
  if (!box || box.dataset.a11yReady === '1') {
    if (box) focusIntoModal(box);
    return;
  }
  box.dataset.a11yReady = '1';
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.tabIndex = -1;
  const title = box.querySelector('.modal-header h3, .modal-header h2, .dirpicker-title');
  if (title) {
    if (!title.id) title.id = `modal-title-${++modalA11y.seq}`;
    box.setAttribute('aria-labelledby', title.id);
  } else {
    box.setAttribute('aria-label', '对话框');
  }
  focusIntoModal(box);
}

function focusIntoModal(box) {
  // 优先聚焦第一个"有内容意义"的控件，而不是右上角关闭按钮
  const items = focusablesIn(box);
  const target = items.find(el => !el.classList.contains('modal-close-btn')) || items[0] || box;
  try { target.focus({ preventScroll: true }); } catch { /* 元素不可聚焦时忽略 */ }
}

function restoreModalFocus() {
  const el = modalA11y.opener;
  modalA11y.opener = null;
  if (el && el.isConnected && typeof el.focus === 'function') {
    try { el.focus({ preventScroll: true }); } catch { /* 忽略 */ }
  }
}

new MutationObserver((records) => {
  for (const r of records) {
    for (const n of r.removedNodes) {
      if (n.nodeType === 1 && n.classList?.contains('modal-overlay') && !activeModalBox()) {
        restoreModalFocus();
      }
    }
    for (const n of r.addedNodes) {
      if (n.nodeType === 1 && n.classList?.contains('modal-overlay')) {
        if (!modalA11y.opener) modalA11y.opener = document.activeElement;
        enhanceModal(n.querySelector('.modal') || n.firstElementChild);
      }
    }
  }
}).observe(document.body, { childList: true });

// ============ Keyboard Shortcuts ============
document.addEventListener('keydown', (e) => {
  // Ctrl/⌘+K 打开命令面板（已打开时再按一次关闭）
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    cmdk.open ? cmdkClose() : cmdkOpen();
    return;
  }

  // Esc to close modals
  if (e.key === 'Escape') {
    if (cmdk.open) { cmdkClose(); return; }
    const modal = document.querySelector('.modal-overlay');
    if (modal) {
      modal.remove();
      return;
    }
    const dp = $('#dirpicker-overlay');
    if (dp && dp.style.display !== 'none') {
      closeDirPicker();
      return;
    }
  }

  // 弹窗打开时把 Tab 圈在弹窗内，否则焦点会跑到背后不可见的页面上
  if (e.key === 'Tab') {
    const box = activeModalBox();
    if (!box) return;
    const items = focusablesIn(box);
    if (!items.length) { e.preventDefault(); box.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (!box.contains(document.activeElement)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }
});

// ============ Status ============
function updateStatus() {
  // 只写底部状态栏的两个 span。原先这里先写 #status-tokens / #status-cost，
  // 紧接着又用 innerHTML 重建 .status-center 把它们整片替换掉——既丢了千分位
  // 格式化，又让 dom 里的引用变成悬空节点。
  const total = state.tokens?.total || 0;
  if (dom.statusTokens) {
    dom.statusTokens.textContent = typeof formatNumber === 'function' ? formatNumber(total) : String(total);
  }
  if (dom.statusCost) dom.statusCost.textContent = '$' + (state.cost || 0).toFixed(2);
}

// ============ Health Check ============
async function checkHealth() {
  try {
    const res = await fetch(`${API}/health`);
    const data = await res.json();
    state.connected = data.status === 'ok';
    if (data.version) {
      const verEl = document.querySelector('.brand-ver');
      if (verEl) verEl.textContent = `v${data.version}`;
    }
  } catch {
    state.connected = false;
  }
  updateConnectionStatus(state.connected);
}
