/* ============================================
   Symbio UI — 能力账本页
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Capabilities Page ============
async function loadCapabilities() {
  showLoading(dom.capabilityGrid, 'Loading capabilities...');
  try {
    const res = await fetch(`${API}/capabilities`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.capabilities = await res.json();
    renderCapabilities();
  } catch (e) {
    toast('error', 'Failed to load capabilities', e.message);
    if (dom.capabilityGrid) {
      dom.capabilityGrid.innerHTML = `<div class="empty-state-lg"><p>Load failed. Try again.</p></div>`;
    }
  }
}

function renderCapabilities() {
  const summary = state.capabilities.summary || {};
  const items = state.capabilities.items || [];
  if (dom.capabilitySummary) {
    dom.capabilitySummary.innerHTML = [
      capabilityStatCard('Total', summary.total || items.length || 0, 'all'),
      capabilityStatCard('Implemented', summary.implemented || 0, 'implemented'),
      capabilityStatCard('Partial', summary.partial || 0, 'partial'),
      capabilityStatCard('Missing', summary.missing || 0, 'missing'),
    ].join('');
  }

  const filtered = state.capabilityFilter === 'all'
    ? items
    : items.filter(item => item.status === state.capabilityFilter);

  if (!dom.capabilityGrid) return;
  if (filtered.length === 0) {
    dom.capabilityGrid.innerHTML = `<div class="empty-state-lg"><p>No matching capabilities.</p></div>`;
    return;
  }

  dom.capabilityGrid.innerHTML = filtered.map(item => `
    <article class="capability-card capability-${esc(item.status)}">
      <div class="capability-card-head">
        <div>
          <div class="capability-module">${esc(item.module || 'module')}</div>
          <h3>${esc(item.claim || item.id)}</h3>
        </div>
        <span class="capability-status status-${esc(item.status)}">${capabilityStatusLabel(item.status)}</span>
      </div>
      <div class="capability-next">${esc(item.next_step || '')}</div>
      <div class="capability-meta-block">
        <div class="capability-meta-title">Evidence</div>
        <div class="capability-chip-row">
          ${(item.evidence || []).length
            ? item.evidence.map(path => `<span class="capability-chip">${esc(path)}</span>`).join('')
            : '<span class="capability-chip muted">No code evidence yet</span>'}
        </div>
      </div>
      <div class="capability-meta-block">
        <div class="capability-meta-title">Docs</div>
        <div class="capability-chip-row">
          ${(item.docs || []).map(path => `<span class="capability-chip doc">${esc(path)}</span>`).join('')}
        </div>
      </div>
    </article>
  `).join('');
}

function capabilityStatCard(label, value, status) {
  return `
    <button class="capability-stat capability-stat-${status} ${state.capabilityFilter === status ? 'active' : ''}" data-status="${status}">
      <span>${esc(label)}</span>
      <strong>${formatNumber(value)}</strong>
    </button>
  `;
}

function capabilityStatusLabel(status) {
  const map = {
    implemented: 'Implemented',
    partial: '部分实现',
    missing: 'Missing',
  };
  return map[status] || status || 'Unknown';
}

dom.capabilityFilter?.addEventListener('change', (e) => {
  state.capabilityFilter = e.target.value;
  renderCapabilities();
});
dom.capabilitySummary?.addEventListener('click', (e) => {
  const card = e.target.closest('.capability-stat');
  if (!card) return;
  state.capabilityFilter = card.dataset.status || 'all';
  if (dom.capabilityFilter) dom.capabilityFilter.value = state.capabilityFilter;
  renderCapabilities();
});
$('#btn-refresh-capabilities')?.addEventListener('click', loadCapabilities);
