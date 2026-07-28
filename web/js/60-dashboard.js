/* ============================================
   Symbio UI — 仪表盘页：成本/Token/预算
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Dashboard Page ============
async function loadDashboard() {
  try {
    // 三个数据源各自独立：任一失败不应让整块归零
    const [memData, sessData, costData] = await Promise.all([
      fetch(`${API}/memory/stats`).then(r => r.ok ? r.json() : {}).catch(() => ({})),
      fetch(`${API}/sessions`).then(r => r.ok ? r.json() : {}).catch(() => ({})),
      fetch(`${API}/costs/dashboard`).then(r => r.ok ? r.json() : {}).catch(() => ({})),
    ]);

    const sessions = sessData.sessions || [];
    const totalMessages = sessions.reduce((sum, s) => sum + (s.message_count || 0), 0);

    // /api/memory/stats 返回 {sqlite:{total}, memory_manager:{total_memories,...}}，
    // 没有 total_count / total_tokens 这两个字段。以前直接读，导致有记忆也显示 0。
    const memoryCount = (memData.sqlite?.total ?? 0) + (memData.memory_manager?.total_memories ?? 0);

    // token 真值在 /api/costs/dashboard，不在 memory/stats 也不在 sessions。
    const totalTokens = costData.summary?.total_tokens
      || costData.budget?.consumed_tokens
      || state.tokens.total
      || 0;

    document.getElementById('dash-total-messages').textContent = formatNumber(totalMessages);
    document.getElementById('dash-total-tokens').textContent = formatNumber(totalTokens);
    document.getElementById('dash-active-sessions').textContent = formatNumber(sessions.length);
    document.getElementById('dash-memory-count').textContent = formatNumber(memoryCount);

    // 按模型分组的真实消耗；sessions 不含 token 字段，不能用来画图
    renderTokenChart(costData.summary?.models || []);
    await loadObservabilitySummary();
    renderCostDashboard(costData);
  } catch (e) {
    console.warn('加载仪表盘数据失败:', e.message);
    document.getElementById('dash-total-tokens').textContent = formatNumber(state.tokens.total);
    document.getElementById('dash-active-sessions').textContent = formatNumber(state.sessions.length);
    await loadObservabilitySummary();
    await loadCostDashboard();
  }
}

async function loadCostDashboard() {
  try {
    const res = await fetch(`${API}/costs/dashboard`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderCostDashboard(await res.json());
  } catch (e) {
    console.warn('加载成本中心失败:', e.message);
    const el = document.getElementById('cost-cache-hit-rate');
    if (el) el.textContent = '—';
    renderCostModelTable([], { failed: true, reason: e.message });
  }
}

// 与 loadCostDashboard 分离：loadDashboard 已经并行取过一次 /costs/dashboard，
// 不必为了渲染再请求一遍。
function renderCostDashboard(data) {
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  const summary = data.summary || {};
  setText('cost-total-tokens', formatNumber(summary.total_tokens || 0));
  setText('cost-total-requests', formatNumber(summary.total_requests || 0));

  const cache = data.cache || {};
  if (cache.enabled && cache.total_queries > 0) {
    setText('cost-cache-hit-rate', `${Math.round((cache.hit_rate || 0) * 100)}%`);
  } else {
    setText('cost-cache-hit-rate', cache.enabled ? '0%' : '未启用');
  }
  setText('cost-cache-saved', formatNumber(cache.estimated_token_saved || 0));

  const budget = data.budget || {};
  const fill = document.getElementById('cost-budget-fill');
  const text = document.getElementById('cost-budget-text');
  if (budget.available && budget.monthly_limit_tokens > 0) {
    const pct = Math.min(100, Math.round((budget.percentage_used || 0) * 100));
    if (fill) {
      fill.style.width = `${pct}%`;
      fill.classList.toggle('over', budget.is_exceeded);
      fill.classList.toggle('warn', !budget.is_exceeded && budget.should_downgrade);
    }
    if (text) {
      let label = `${formatNumber(budget.consumed_tokens)} / ${formatNumber(budget.monthly_limit_tokens)} (${pct}%)`;
      if (budget.is_exceeded) label += ' · 已超预算';
      else if (budget.should_downgrade) label += ` · 建议降级${budget.downgrade_model ? `到 ${budget.downgrade_model}` : '模型'}`;
      text.textContent = label;
    }
  } else {
    if (fill) { fill.style.width = '0%'; fill.classList.remove('over', 'warn'); }
    if (text) text.textContent = budget.available ? `已消耗 ${formatNumber(budget.consumed_tokens || 0)} · 未设置上限` : '未设置（不限制）';
  }

  renderCostModelTable(summary.models || []);
}

function renderCostModelTable(models, opts = {}) {
  const container = document.getElementById('cost-model-table');
  if (!container) return;
  // 区分「没有数据」和「取不到数据」——空白同时代表两者时，用户无法判断系统是否正常
  if (opts.failed) {
    container.innerHTML = `
      <div class="empty-block is-inline is-error">
        <p class="empty-block-title">成本数据读取失败</p>
        <p class="empty-block-hint">${esc(opts.reason || '接口无响应')} · 这里显示的不是「没花钱」，而是「不知道花了多少」。请确认服务仍在运行，然后点右上角刷新重试。</p>
      </div>`;
    return;
  }
  if (!models.length) {
    container.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">还没有模型用量记录</p>
        <p class="empty-block-hint">发起一次对话或跑一次任务后，这里会按模型列出请求数与输入/输出 token。它的用处是回答「钱花在哪个模型上」——上面的预算条只给总量，分不清是某个贵模型调了两次，还是便宜模型被刷了几百次。</p>
      </div>`;
    return;
  }
  const maxTotal = Math.max(...models.map(m => m.total_tokens || 0), 1);
  container.innerHTML = `
    <div class="cost-table-row cost-table-head">
      <span>模型</span><span>请求</span><span>输入</span><span>输出</span><span>占比</span>
    </div>
    ${models.map(m => {
      const pct = Math.round(((m.total_tokens || 0) / maxTotal) * 100);
      return `<div class="cost-table-row">
        <span class="cost-model-name" title="${esc(m.model)}">${esc(m.model)}</span>
        <span>${formatNumber(m.request_count || 0)}</span>
        <span>${formatNumber(m.total_input_tokens || 0)}</span>
        <span>${formatNumber(m.total_output_tokens || 0)}</span>
        <span class="cost-model-share"><i style="width:${pct}%"></i></span>
      </div>`;
    }).join('')}`;
}

document.getElementById('btn-set-budget')?.addEventListener('click', async () => {
  const input = document.getElementById('cost-budget-input');
  const value = parseInt(input?.value, 10);
  if (isNaN(value) || value < 0) {
    toast('error', '预算无效', '请输入有效的月度 Token 上限（0 表示不限）');
    return;
  }
  try {
    const res = await fetch(`${API}/costs/budget`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: 'default', monthly_limit_tokens: value }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('success', '预算已更新', value === 0 ? '已取消预算限制' : `月度预算设置为 ${formatNumber(value)} tokens`);
    await loadCostDashboard();
  } catch (e) {
    toast('error', '设置预算失败', e.message);
  }
});

async function loadObservabilitySummary() {
  try {
    const res = await fetch(`${API}/observability/summary`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const status = data.enabled && data.is_started ? 'online' : (data.enabled ? 'ready' : 'offline');
    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    };
    setText('obs-tracer-status', status);
    setText('obs-span-count', formatNumber(data.spans?.captured || 0));
    setText('obs-metric-count', formatNumber(data.metrics?.records || 0));
    setText('obs-token-entry-count', formatNumber(data.tokens?.entries || 0));
  } catch (e) {
    const el = document.getElementById('obs-tracer-status');
    if (el) el.textContent = 'error';
  }
}

let _tokenChartInst = null;
// 入参是 /api/costs/dashboard 的 summary.models[]，不是 sessions。
// sessions 里根本没有 token 字段，旧实现读 s.token_count 恒为 0。
function renderTokenChart(models) {
  const container = dom.tokenBarChart;
  if (!container) return;

  if (_tokenChartInst) { _tokenChartInst.destroy(); _tokenChartInst = null; }

  const data = (models || [])
    .map(m => ({ label: String(m.model || '未知模型'), value: m.total_tokens || 0 }))
    .filter(d => d.value > 0)
    .sort((a, b) => b.value - a.value)
    .slice(0, 8);

  // 零数据就说零数据，不再伪造「会话1…会话6」六根空柱假装有图
  if (data.length === 0) {
    container.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">暂无 Token 消耗记录</p>
        <p class="empty-block-hint">去「对话」页发起一次提问，这里会出现按模型分组的消耗柱状图，用来判断钱花在了哪个模型上。</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="switchPage('chat')">去对话页</button>
        </div>
      </div>`;
    return;
  }

  if (typeof Chart === 'undefined') {
    // Chart.js 未加载时的纯 CSS 兜底
    const maxVal = Math.max(...data.map(d => d.value), 1);
    container.innerHTML = data.map(d => `
      <div class="token-bar-row">
        <span class="token-bar-label" title="${esc(d.label)}">${esc(d.label)}</span>
        <span class="token-bar-track"><i style="width:${Math.round((d.value / maxVal) * 100)}%"></i></span>
        <span class="token-bar-value">${formatNumber(d.value)}</span>
      </div>`).join('');
    return;
  }

  container.innerHTML = '<canvas id="token-chart-canvas" style="max-height:160px" role="img"></canvas>';
  const canvas = container.querySelector('canvas');
  // canvas 对读屏器是黑盒，把同样的数据写进可访问名称
  canvas.setAttribute('aria-label',
    `按模型分组的 Token 消耗：${data.map(d => `${d.label} ${formatNumber(d.value)}`).join('，')}`);

  // Chart.js 画在 canvas 上，读不到 CSS 变量，必须取计算值传字面色，
  // 否则主题切换后图表配色会和界面脱节。
  const cvar = (name, fallback) => {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  };
  const isDark = state.theme !== 'light';
  const gridColor = isDark ? 'rgba(255,250,240,0.08)' : 'rgba(60,50,35,0.10)';
  const labelColor = cvar('--text-tertiary', '#948d80');
  const barColor = cvar('--accent', '#e07a5a');

  _tokenChartInst = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: data.map(d => d.label),
      datasets: [{
        label: 'Token 用量',
        data: data.map(d => d.value),
        backgroundColor: barColor,
        borderColor: barColor,
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.parsed.y.toLocaleString()} tokens`,
          },
        },
      },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: labelColor, font: { size: 11 } } },
        y: { grid: { color: gridColor }, ticks: { color: labelColor, font: { size: 11 } } },
      },
    },
  });
}

// Refresh dashboard button
$('#btn-refresh-dashboard')?.addEventListener('click', loadDashboard);
