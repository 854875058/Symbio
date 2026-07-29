/* ============================================
   Symbio UI — 收尾：回车快捷键绑定、init() 与 DOMContentLoaded（必须最后加载）
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

bindEnter('cost-budget-input', 'btn-set-budget');
bindEnter('security-scan-input', 'btn-security-scan', { ctrl: true });
bindEnter('cu-start-url', 'btn-cu-create');
bindEnter('cu-goal', 'btn-cu-plan');
bindEnter('cu-action-param', 'btn-cu-act');
bindEnter('hitl-escalation-target', 'btn-save-timeout-policy');

// ============ Init ============
async function init() {
  // Apply theme（persist 跟随是否存在显式选择，否则会把系统默认值写成显式偏好）
  applyTheme(state.theme, { persist: Boolean(localStorage.getItem(THEME_KEY)) });

  // Apply sidebar state（手机端一律先收起，避免抽屉挡住首屏）
  setSidebarCollapsed(isMobileLayout() ? true : state.sidebarCollapsed, {
    persist: !isMobileLayout(),
  });

  initNavGroups();
  initCmdk();

  await loadSessions();
  await Promise.all([loadModels(), loadConfig()]);
  await checkHealth();
  connectWebSocket();
  setupVirtualScroll();

  // 路由放在基础数据就绪之后：initRouter 可能直接切到某一页并触发它的
  // 加载函数，而那些函数会读 state.models / state.config。
  // 也必须在 initNavGroups 之后——恢复页面时要展开对应的侧栏分组。
  initRouter();

  // Update status model name
  const modelName = state.selectedChatModel || (state.models?.[0]?.model_id) || '--';
  if (dom.statusModelName) dom.statusModelName.textContent = modelName;

  setInterval(checkHealth, 30000);
  updateWelcomeCapCount();
  console.log('Symbio UI initialized');
}

// 首页欢迎语的能力数从 /api/capabilities 动态取（原先写死 "33 项" 是错的）
async function updateWelcomeCapCount() {
  const el = document.getElementById('welcome-cap-count');
  if (!el) return;
  try {
    const res = await fetch(`${API}/capabilities`);
    const data = await res.json();
    const s = data.summary || {};
    const impl = s.implemented ?? 0;
    const total = s.total ?? (data.items?.length || 0);
    el.textContent = total ? `${impl}/${total} 项能力已落地` : '多项核心能力';
  } catch (e) {
    el.textContent = '多项核心能力';  // 拿不到就退化成不带数字的文案
  }
}

document.addEventListener('DOMContentLoaded', init);
