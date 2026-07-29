/* ============================================
   Symbio UI — 基础层：API 鉴权、全局 state、DOM 引用、主题、通用工具
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

/* ============================================
   Symbio — Neural Command Center
   ============================================ */

const API = `${window.location.origin}/api`;

/* ============ API 鉴权 ============
   服务端配置了 SYMBIO_API_TOKEN 时，所有 /api 请求都需要 Bearer token。
   这里一次性拦截 window.fetch，避免逐个改上百处调用点。
   token 存在 localStorage：首次收到 401 时提示用户输入。 */
const TOKEN_KEY = 'symbio-api-token';

function getApiToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

function setApiToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function withToken(url) {
  const token = getApiToken();
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

(function installAuthFetch() {
  const nativeFetch = window.fetch.bind(window);
  let prompting = false;

  window.fetch = async (input, init = {}) => {
    const token = getApiToken();
    if (token) {
      const headers = new Headers(
        init.headers || (input instanceof Request ? input.headers : undefined)
      );
      headers.set('Authorization', `Bearer ${token}`);
      init = { ...init, headers };
    }

    const resp = await nativeFetch(input, init);

    if (resp.status === 401 && !prompting) {
      prompting = true;
      try {
        const entered = window.prompt(
          'API 需要鉴权。请输入服务端配置的 API token（SYMBIO_API_TOKEN）：',
          ''
        );
        if (entered && entered.trim()) {
          setApiToken(entered.trim());
          window.location.reload();
        }
      } finally {
        prompting = false;
      }
    }
    return resp;
  };
})();

const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/chat`;

// ============ State ============
const state = {
  page: 'chat',
  sessions: [{ id: 'default', title: '新对话', time: '刚刚' }],
  currentSession: 'default',
  messages: [],
  models: [],
  selectedChatModel: localStorage.getItem('symbio-chat-model') || '',
  tasks: [],
  taskFilter: 'all',
  memories: [],
  memoryImportance: 'all',
  ontologyGraph: { stats: {}, nodes: [], edges: [] },
  ontologySelection: null,
  ontologySim: null,   // Obsidian 风格力导向仿真运行态（见 renderOntologyGraph）
  skills: [],
  skillDetail: null,
  skillMode: 'local',
  marketplace: { packages: [], stats: {}, installed: [], categories: [], remoteAutoLoaded: false },
  tokens: { input: 0, output: 0, total: 0 },
  cost: 0,
  // null = 还没探测过（首次 /health 未返回）。用 false 起步会让界面在
  // 问过后端之前就断言"已断开"，跟原先硬编码"已连接"是同一类谎话。
  connected: null,
  ws: null,
  wsReconnectDelay: 1000,
  wsMaxReconnectDelay: 30000,
  wsReconnectTimer: null,
  streaming: false,
  streamContent: '',
  config: {},
  hitlItems: [],
  hitlFilter: 'pending',
  capabilities: { summary: {}, items: [] },
  capabilityFilter: 'all',
  evolution: { export: null, suites: [] },
  sandbox: { policy: null, audit: [], lastResult: null },
  externalAgents: { providers: [], sessions: [], transcripts: [], audit: [], activeSessionId: '', lastResult: null },
  // 无显式选择时跟随系统（systemTheme 是函数声明，已提升，此处可调用）
  theme: localStorage.getItem('symbio-theme') || systemTheme(),
  pagesLoaded: {},
  virtualScrollEnabled: false,
  executionCache: {},
  a2a: { sessions: [], inboundTasks: [], ownCard: null },
  sidebarCollapsed: localStorage.getItem('symbio-sidebar-collapsed') === '1',
};

// ============ DOM ============
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const escapeSelectorValue = (value) => {
  const text = String(value ?? '');
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    return CSS.escape(text);
  }
  return text.replace(/["\\]/g, '\\$&');
};

const dom = {
  navTabs: $$('.nav-tab'),
  pages: $$('.page'),
  sessionsList: $('#sessions-list'),
  welcome: $('#welcome-screen'),
  messages: $('#messages-container'),
  input: $('#message-input'),
  chatModelSelect: $('#chat-model-select'),
  sendBtn: $('#send-btn'),
  newChat: $('#btn-new-chat'),
  chips: $$('.chip'),
  togglePanel: $('#toggle-panel'),
  panel: $('#context-panel'),
  toast: $('#toast-container'),
  modelsGrid: $('#models-grid'),
  tasksGrid: $('#tasks-grid'),
  memoryGrid: $('#memory-grid'),
  memorySearch: $('#memory-search'),
  memoryImportanceFilter: $('#memory-importance-filter'),
  ontologySummary: $('#ontology-summary'),
  ontologyGraph: $('#ontology-graph'),
  ontologyEmpty: $('#ontology-empty'),
  ontologyDetail: $('#ontology-detail'),
  ontologySearch: $('#ontology-search'),
  ontologyFilter: $('#ontology-category-filter'),
  skillsGrid: $('#skills-grid'),
  skillsSearch: $('#skills-search'),
  skillsModeTabs: $('#skills-mode-tabs'),
  marketplaceShell: $('#marketplace-shell'),
  marketplaceSummary: $('#marketplace-summary'),
  marketplaceGrid: $('#marketplace-grid'),
  btnImportSkill: $('#btn-import-skill'),
  configSection: $('#llm-config-section'),
  themeToggle: $('#theme-toggle'),
  dashboardCards: $('#dashboard-cards'),
  tokenBarChart: $('#token-bar-chart'),
  hitlGrid: $('#hitl-grid'),
  hitlFilter: $('#hitl-filter'),
  capabilitySummary: $('#capability-summary'),
  capabilityGrid: $('#capability-grid'),
  capabilityFilter: $('#capability-filter'),
  exportFormat: $('#export-format'),
  exportMeta: $('#evolution-export-meta'),
  exportPreview: $('#evolution-export-preview'),
  evalSuitePath: $('#eval-suite-path'),
  evalSuiteGrid: $('#evolution-suite-grid'),
  sandboxPolicy: $('#sandbox-policy'),
  sandboxCommand: $('#sandbox-command'),
  sandboxPermission: $('#sandbox-permission'),
  sandboxApprovalPolicy: $('#sandbox-approval-policy'),
  sandboxAccessMode: $('#sandbox-access-mode'),
  sandboxTimeout: $('#sandbox-timeout'),
  sandboxWorkingDir: $('#sandbox-working-dir'),
  sandboxApproved: $('#sandbox-approved'),
  sandboxShell: $('#sandbox-shell'),
  sandboxResult: $('#sandbox-result'),
  sandboxAuditList: $('#sandbox-audit-list'),
  externalAgentProviderBadges: $('#external-agent-provider-badges'),
  externalAgentProvider: $('#external-agent-provider'),
  externalAgentLabel: $('#external-agent-label'),
  externalAgentWorkspace: $('#external-agent-workspace'),
  externalAgentSessionId: $('#external-agent-session-id'),
  externalAgentModel: $('#external-agent-model'),
  externalAgentSandboxMode: $('#external-agent-sandbox-mode'),
  externalAgentApprovalPolicy: $('#external-agent-approval-policy'),
  externalAgentPrompt: $('#external-agent-prompt'),
  externalAgentResult: $('#external-agent-result'),
  externalAgentSessions: $('#external-agent-sessions'),
  externalAgentTranscripts: $('#external-agent-transcripts'),
  externalAgentAudit: $('#external-agent-audit'),
  sidebarEl: $('#sidebar'),
  appRoot: $('.app'),
  topbarTitle: $('#topbar-title'),
  statusTokens: $('#status-tokens'),
  statusCost: $('#status-cost'),
  statusModelName: $('#status-model-name'),
  statusDot: $('#status-dot'),
  sidebarCollapseBtn: $('#sidebar-collapse'),
};

// ============ Theme Toggle ============
// 默认跟随系统 prefers-color-scheme；用户点过顶栏切换键后，
// localStorage 里的显式选择优先，不再被系统偏好改写。
const THEME_KEY = 'symbio-theme';

function systemTheme() {
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function applyTheme(theme, { persist = true } = {}) {
  document.documentElement.setAttribute('data-theme', theme);
  if (persist) localStorage.setItem(THEME_KEY, theme);
  state.theme = theme;
  // 图标反映当前主题：浅色显示月亮（点→去深色），深色显示太阳（点→去浅色）
  const light = theme === 'light';
  document.querySelectorAll('.icon-sun').forEach(el => { el.style.display = light ? 'none' : ''; });
  document.querySelectorAll('.icon-moon').forEach(el => { el.style.display = light ? '' : 'none'; });
}

function toggleTheme() {
  applyTheme(state.theme === 'dark' ? 'light' : 'dark');
}

dom.themeToggle?.addEventListener('click', toggleTheme);
document.getElementById('topbar-theme-toggle')?.addEventListener('click', toggleTheme);

// 没有显式选择时跟随系统，且系统偏好变化时实时同步
window.matchMedia?.('(prefers-color-scheme: light)').addEventListener('change', () => {
  if (!localStorage.getItem(THEME_KEY)) applyTheme(systemTheme(), { persist: false });
});

// Apply saved theme on load（无存储时用系统值，且不写回存储）
applyTheme(state.theme, { persist: Boolean(localStorage.getItem(THEME_KEY)) });

// ============ Toast ============
function toast(type, title, msg) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;

  const iconSvg = {
    success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>',
    error: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };

  el.innerHTML = `
    <div class="toast-icon ${type}">${iconSvg[type] || iconSvg.info}</div>
    <div style="flex:1">
      <div style="font-weight:600;font-size:var(--fs-sm)">${esc(title)}</div>
      ${msg ? `<div style="font-size:var(--fs-xs);color:var(--text-secondary);margin-top:2px">${esc(msg)}</div>` : ''}
    </div>
  `;
  dom.toast.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transform = 'translateX(40px)';
    el.style.transition = 'all 300ms';
    setTimeout(() => el.remove(), 300);
  }, 4000);
}

// ============ Utility ============
// 转义 HTML 文本内容。textContent -> innerHTML 只处理 & < >，不处理引号，
// 所以额外手工转义 " 和 '：属性上下文（尤其是 onclick="fn('${...}')"）
// 缺了引号转义就能被 payload 闭合属性并注入可执行表达式。
function esc(text) {
  if (text === null || text === undefined || text === '') return '';
  const d = document.createElement('div');
  d.textContent = String(text);
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 用于内联 JS 字符串字面量（onclick="fn('${escJs(x)}')"）。
// 先按 JS 字面量转义反斜杠与引号，再交给 esc() 做 HTML 层转义。
// 不可信数据尽量改用 dataset + addEventListener，本函数是过渡期的兜底。
function escJs(text) {
  if (text === null || text === undefined || text === '') return '';
  return esc(String(text).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r?\n/g, '\\n'));
}

// 高风险动作确认。统一入口的目的不是弹窗好看，而是保证「危险程度」和
// 「确认强度」成正比：以前删 Skill 有确认、放开沙箱全权限却没有，
// 用户学到的规律是「没弹窗 = 安全」，恰好在最危险的地方失效。
// consequence 必须写清楚不可逆的后果，不能只写「确定吗」。
function confirmDanger(title, consequence) {
  return confirm(`${title}\n\n${consequence}`);
}

function showLoading(container, message = '加载中...') {
  // aria-busy/role=status 挂在 loading-state 上而不是 container 上：
  // 各调用方是用 innerHTML 整体替换来结束加载的，挂在内部节点才会自动消失。
  container.innerHTML = `
    <div class="loading-state" role="status" aria-live="polite" aria-busy="true">
      <div class="loading-spinner" aria-hidden="true"></div>
      <p>${esc(message)}</p>
    </div>
  `;
}

function formatTime(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  } catch {
    return isoStr;
  }
}

// 数字格式化。两个此前的坑：
// 1) 传进 undefined / null / NaN 时会 String() 出字面量 "undefined" 显示在页面上；
// 2) 1000 以下不加千分位没问题，但 K/M 之下的整数（如 4 位以内）也该易读，
//    所以四位以上统一走 toLocaleString。
function formatNumber(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  if (abs >= 1000000) return (v / 1000000).toFixed(1) + 'M';
  if (abs >= 1000) return (v / 1000).toFixed(1) + 'K';
  return v.toLocaleString('zh-CN');
}

// 键盘快捷键：让新页面的输入框支持回车直接触发主操作
function bindEnter(inputId, buttonId, opts = {}) {
  const input = document.getElementById(inputId);
  const btn = document.getElementById(buttonId);
  if (!input || !btn) return;
  input.addEventListener('keydown', (e) => {
    const wantCtrl = !!opts.ctrl;
    if (e.key === 'Enter' && (!wantCtrl || e.ctrlKey || e.metaKey) && !e.shiftKey) {
      e.preventDefault();
      btn.click();
    }
  });
}
