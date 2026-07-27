/* ============================================
   Symbio UI — 仪表盘页：成本/Token/预算
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Dashboard Page ============
async function loadDashboard() {
  try {
    // Fetch memory stats
    const memRes = await fetch(`${API}/memory/stats`);
    const memData = await memRes.json();

    // Fetch sessions
    const sessRes = await fetch(`${API}/sessions`);
    const sessData = await sessRes.json();

    // Update cards
    const totalMessages = (sessData.sessions || []).reduce((sum, s) => sum + (s.message_count || 0), 0);
    const totalTokens = memData.total_tokens || state.tokens.total;
    const activeSessions = (sessData.sessions || []).length;
    const memoryCount = memData.total_count || 0;

    document.getElementById('dash-total-messages').textContent = totalMessages;
    document.getElementById('dash-total-tokens').textContent = formatNumber(totalTokens);
    document.getElementById('dash-active-sessions').textContent = activeSessions;
    document.getElementById('dash-memory-count').textContent = memoryCount;

    // Render bar chart from session token data
    renderTokenChart(sessData.sessions || []);
    await loadObservabilitySummary();
    await loadCostDashboard();
  } catch (e) {
    console.warn('加载仪表盘数据失败:', e.message);
    // Use local state as fallback
    document.getElementById('dash-total-tokens').textContent = formatNumber(state.tokens.total);
    document.getElementById('dash-active-sessions').textContent = state.sessions.length;
    await loadObservabilitySummary();
    await loadCostDashboard();
  }
}

async function loadCostDashboard() {
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  try {
    const res = await fetch(`${API}/costs/dashboard`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

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
  } catch (e) {
    console.warn('加载成本中心失败:', e.message);
    setText('cost-cache-hit-rate', 'error');
  }
}

function renderCostModelTable(models) {
  const container = document.getElementById('cost-model-table');
  if (!container) return;
  if (!models.length) {
    container.innerHTML = '<div class="cost-table-empty">暂无模型用量记录，发起一次对话后这里会出现按模型分组的消耗明细</div>';
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
function renderTokenChart(sessions) {
  const container = dom.tokenBarChart;
  if (!container) return;

  // Use Chart.js if available
  if (typeof Chart !== 'undefined') {
    const data = sessions.slice(-8).map(s => ({
      label: (s.title || '对话').substring(0, 8),
      value: s.token_count || s.tokens || 0,
    }));
    if (data.length === 0) {
      // Generate placeholder data
      for (let i = 1; i <= 6; i++) data.push({ label: `会话${i}`, value: 0 });
    }

    // Destroy existing chart
    if (_tokenChartInst) { _tokenChartInst.destroy(); _tokenChartInst = null; }

    // Create canvas if needed
    container.innerHTML = '<canvas id="token-chart-canvas" style="max-height:160px"></canvas>';
    const canvas = container.querySelector('canvas');
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
          label: 'Tokens',
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
    return;
  }

  // Fallback: simple bars
  const data = sessions.slice(-7).map(s => ({
    label: (s.title || '会话').substring(0, 6),
    value: s.token_count || s.tokens || 0,
  }));
  if (data.length === 0 || data.every(d => d.value === 0)) {
    container.innerHTML = '<div class="empty-state-lg"><p>暂无 Token 数据</p></div>';
    return;
  }
  const maxVal = Math.max(...data.map(d => d.value), 1);
  container.innerHTML = data.map(d => {
    const pct = Math.max(4, Math.round((d.value / maxVal) * 100));
    return `<div class="bar-chart-bar"><div class="bar-chart-fill" style="height:${pct}%" data-value="${d.value}"></div><div class="bar-chart-label">${esc(d.label)}</div></div>`;
  }).join('');
}

// Refresh dashboard button
$('#btn-refresh-dashboard')?.addEventListener('click', loadDashboard);
