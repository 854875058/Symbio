/* ============================================
   Symbio UI — 记忆页 + 本体图谱页
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Memory Page ============
async function loadMemories(query) {
  showLoading(dom.memoryGrid, query ? '搜索记忆...' : '加载记忆...');
  try {
    const url = query ? `${API}/memory/search?q=${encodeURIComponent(query)}` : `${API}/memory`;
    const res = await fetch(url);
    const data = await res.json();
    state.memories = data.memories || [];
    renderMemories(query);
  } catch (e) {
    toast('error', '加载记忆失败', e.message);
    dom.memoryGrid.innerHTML = `<div class="empty-state-lg"><p>加载失败，请重试</p></div>`;
  }
}

function renderMemories(query) {
  if (state.memories.length === 0) {
    dom.memoryGrid.innerHTML = `
      <div class="empty-state-lg">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
        <p>${query ? '未找到匹配的记忆' : '记忆库为空'}</p>
        <span class="empty-hint">${query ? '尝试不同的搜索词' : '对话中的重要信息会自动存储'}</span>
      </div>
    `;
    return;
  }

  dom.memoryGrid.innerHTML = state.memories.map(m => `
    <div class="memory-card" data-id="${m.id}">
      <div class="memory-card-header">
        <div class="memory-card-title">${esc(m.title)}</div>
        <div class="memory-card-importance">
          <div class="importance-bar">
            <div class="importance-fill" style="width:${(m.importance || 0) * 100}%"></div>
          </div>
        </div>
      </div>
      <div class="memory-card-snippet">${esc(m.content)}</div>
      <div class="memory-card-meta">
        <div class="memory-tags">
          ${(m.tags || []).map(t => `<span class="memory-tag">${esc(t)}</span>`).join('')}
        </div>
        <div class="memory-info">
          <span class="memory-source">${esc(m.source || 'chat')}</span>
          ${m.relevance !== undefined ? `<span class="memory-relevance">相关度 ${(m.relevance * 100).toFixed(0)}%</span>` : ''}
        </div>
      </div>
    </div>
  `).join('');

  dom.memoryGrid.querySelectorAll('.memory-card').forEach(card => {
    card.addEventListener('click', () => showMemoryDetail(card.dataset.id));
  });
}

function showMemoryDetail(memoryId) {
  const mem = state.memories.find(m => m.id === memoryId);
  if (!mem) return;

  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal modal-wide">
      <div class="modal-header">
        <h3>${esc(mem.title)}</h3>
        <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-section">
          <div class="detail-label">内容</div>
          <div class="detail-value">${esc(mem.content)}</div>
        </div>
        <div class="detail-section">
          <div class="detail-label">标签</div>
          <div class="memory-tags">${(mem.tags || []).map(t => `<span class="memory-tag">${esc(t)}</span>`).join(' ')}</div>
        </div>
        <div class="detail-row">
          <div class="detail-section">
            <div class="detail-label">来源</div>
            <div class="detail-value">${esc(mem.source || 'chat')}</div>
          </div>
          <div class="detail-section">
            <div class="detail-label">重要度</div>
            <div class="detail-value">${((mem.importance || 0) * 100).toFixed(0)}%</div>
          </div>
          <div class="detail-section">
            <div class="detail-label">访问次数</div>
            <div class="detail-value">${mem.access_count || 0}</div>
          </div>
          <div class="detail-section">
            <div class="detail-label">创建时间</div>
            <div class="detail-value">${formatTime(mem.created_at)}</div>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-close-btn-bottom">关闭</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-close-btn-bottom').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// Memory search
let memorySearchTimer = null;
dom.memorySearch?.addEventListener('input', () => {
  clearTimeout(memorySearchTimer);
  memorySearchTimer = setTimeout(() => {
    const q = dom.memorySearch.value.trim();
    loadMemories(q || undefined);
  }, 300);
});

// ============ Ontology Page ============
async function loadOntology() {
  if (dom.ontologySummary) {
    dom.ontologySummary.innerHTML = '';
  }
  if (dom.ontologyGraph) {
    dom.ontologyGraph.innerHTML = '';
  }
  if (dom.ontologyEmpty) {
    dom.ontologyEmpty.style.display = 'none';
  }

  try {
    const res = await fetch(`${API}/ontology`);
    const data = await res.json();
    state.ontologyGraph = data || { stats: {}, nodes: [], edges: [] };
    const nodeIds = new Set((state.ontologyGraph.nodes || []).map(node => node.id));
    if (!nodeIds.has(state.ontologySelection)) {
      state.ontologySelection = null;
    }
    renderOntology();
  } catch (e) {
    toast('error', '加载本体图谱失败', e.message);
    if (dom.ontologySummary) {
      dom.ontologySummary.innerHTML = '';
    }
    if (dom.ontologyGraph) {
      dom.ontologyGraph.innerHTML = '';
    }
    if (dom.ontologyEmpty) {
      dom.ontologyEmpty.style.display = 'flex';
      dom.ontologyEmpty.innerHTML = '<p>加载失败</p><span>请稍后重试</span>';
    }
    if (dom.ontologyDetail) {
      dom.ontologyDetail.innerHTML = `
        <div class="ontology-detail-title">图谱不可用</div>
        <div class="ontology-detail-body">${esc(e.message)}</div>
      `;
    }
  }
}

function ontologyStructuralEdges(nodes) {
  const edges = [];
  for (const node of nodes) {
    if (node.category === 'concept') {
      for (const parentId of (node.parent_ids || [])) {
        edges.push({
          id: `parent:${parentId}:${node.id}`,
          source: parentId,
          target: node.id,
          label: 'is_a',
          relation_type: 'hierarchy',
          structural: true,
        });
      }
    }
    if (node.category === 'individual') {
      for (const conceptId of (node.concept_ids || [])) {
        edges.push({
          id: `instance:${node.id}:${conceptId}`,
          source: node.id,
          target: conceptId,
          label: 'instance_of',
          relation_type: 'instance_of',
          structural: true,
        });
      }
    }
  }
  return edges;
}

function getOntologyView() {
  const sourceNodes = Array.isArray(state.ontologyGraph.nodes) ? state.ontologyGraph.nodes : [];
  const query = (dom.ontologySearch?.value || '').trim().toLowerCase();
  const category = dom.ontologyFilter?.value || 'all';

  const nodes = sourceNodes
    .filter((node) => category === 'all' || node.category === category)
    .filter((node) => {
      if (!query) return true;
      const haystack = [
        node.label,
        node.description,
        ...(node.parent_labels || []),
        ...(node.concept_labels || []),
        ...Object.keys(node.properties || {}),
        ...Object.values(node.properties || {}).map(value => String(value)),
      ].join(' ').toLowerCase();
      return haystack.includes(query);
    })
    .sort((a, b) => {
      if (a.category !== b.category) return a.category.localeCompare(b.category);
      return a.label.localeCompare(b.label, 'zh-CN');
    });

  const visibleIds = new Set(nodes.map((node) => node.id));
  const edges = [
    ...ontologyStructuralEdges(sourceNodes),
    ...((state.ontologyGraph.edges || []).map((edge) => ({ ...edge, structural: false }))),
  ].filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));

  return { nodes, edges, query, category };
}

function renderOntology() {
  const view = getOntologyView();
  renderOntologySummary(view);
  renderOntologyGraph(view);
  renderOntologyDetail(view);
}

function renderOntologySummary(view) {
  if (!dom.ontologySummary) return;
  const stats = state.ontologyGraph.stats || {};
  const tbox = stats.tbox || {};
  const abox = stats.abox || {};
  const visibleConcepts = view.nodes.filter((node) => node.category === 'concept').length;
  const visibleIndividuals = view.nodes.filter((node) => node.category === 'individual').length;

  dom.ontologySummary.innerHTML = `
    <div class="ontology-stat-card">
      <div class="ontology-stat-label">当前视图</div>
      <div class="ontology-stat-value">${view.nodes.length}</div>
      <div class="ontology-stat-meta">节点 / ${view.edges.length} 条连线</div>
    </div>
    <div class="ontology-stat-card">
      <div class="ontology-stat-label">概念层</div>
      <div class="ontology-stat-value">${tbox.concepts || 0}</div>
      <div class="ontology-stat-meta">可见 ${visibleConcepts} / 属性 ${tbox.properties || 0}</div>
    </div>
    <div class="ontology-stat-card">
      <div class="ontology-stat-label">实例层</div>
      <div class="ontology-stat-value">${abox.individuals || 0}</div>
      <div class="ontology-stat-meta">可见 ${visibleIndividuals} / 关系 ${abox.relation_instances || 0}</div>
    </div>
  `;
}


// ===== Obsidian 风格力导向仿真（纯手写，无外部依赖，随服务器分发离线可用）=====
// 斥力（节点互斥）+ 弹簧（边拉拢）+ 向心力（防飘散），requestAnimationFrame 迭代收敛。
function computeNodeDegrees(nodes, edges) {
  const deg = {};
  nodes.forEach((n) => { deg[n.id] = 0; });
  edges.forEach((e) => {
    if (deg[e.source] !== undefined) deg[e.source] += 1;
    if (deg[e.target] !== undefined) deg[e.target] += 1;
  });
  return deg;
}

// 节点半径按连接度：连得越多越大，这是 Obsidian 图谱的招牌观感。
function ontologyNodeRadius(degree) {
  return Math.min(30, 8 + Math.sqrt(degree || 0) * 5);
}

function createOntologySim(nodes, edges, width, height) {
  const degrees = computeNodeDegrees(nodes, edges);
  const cx = width / 2;
  const cy = height / 2;
  // 初始位置：圆周撒开，避免全叠在中心导致初期抖动
  const simNodes = nodes.map((node, i) => {
    const angle = (i / Math.max(1, nodes.length)) * Math.PI * 2;
    const ring = 120 + (i % 5) * 40;
    return {
      ...node,
      degree: degrees[node.id] || 0,
      radius: ontologyNodeRadius(degrees[node.id]),
      x: cx + Math.cos(angle) * ring,
      y: cy + Math.sin(angle) * ring,
      vx: 0, vy: 0, fixed: false,
    };
  });
  const index = new Map(simNodes.map((n) => [n.id, n]));
  const simEdges = edges
    .map((e) => ({ ...e, s: index.get(e.source), t: index.get(e.target) }))
    .filter((e) => e.s && e.t);
  return { nodes: simNodes, edges: simEdges, index, cx, cy, alpha: 1 };
}

// 单步物理：库仑斥力 + 胡克弹簧 + 向心回拉 + 速度阻尼。
function tickOntologySim(sim) {
  const { nodes, edges, cx, cy } = sim;
  const REPULSION = 5200;    // 节点互斥强度
  const SPRING = 0.02;       // 边弹簧劲度
  const SPRING_LEN = 90;     // 边自然长度
  const CENTER = 0.012;      // 向心力
  const DAMPING = 0.82;      // 阻尼

  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 0.01) { d2 = 0.01; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
      const dist = Math.sqrt(d2);
      const force = REPULSION / d2;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    }
  }
  edges.forEach((e) => {
    const dx = e.t.x - e.s.x;
    const dy = e.t.y - e.s.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const force = (dist - SPRING_LEN) * SPRING;
    const fx = (dx / dist) * force;
    const fy = (dy / dist) * force;
    e.s.vx += fx; e.s.vy += fy;
    e.t.vx -= fx; e.t.vy -= fy;
  });
  nodes.forEach((n) => {
    if (n.fixed) { n.vx = 0; n.vy = 0; return; }
    n.vx += (cx - n.x) * CENTER;
    n.vy += (cy - n.y) * CENTER;
    n.vx *= DAMPING;
    n.vy *= DAMPING;
    n.x += n.vx;
    n.y += n.vy;
  });
  sim.alpha *= 0.985;
}

function edgeStrokeClass(edge) {
  if (edge.structural) {
    return edge.relation_type === 'instance_of' ? 'ontology-edge-instance' : 'ontology-edge-structural';
  }
  return 'ontology-edge-relation';
}

function compactOntologyLabel(text, limit = 16) {
  const value = String(text || '');
  if (value.length <= limit) return value;
  return `${value.slice(0, limit - 3)}...`;
}

// 停掉上一轮仿真的动画循环，避免多次进入页面叠加多个 rAF。
function stopOntologySim() {
  if (state.ontologySim && state.ontologySim.raf) {
    cancelAnimationFrame(state.ontologySim.raf);
    state.ontologySim.raf = null;
  }
}

function renderOntologyGraph(view) {
  if (!dom.ontologyGraph || !dom.ontologyEmpty) return;
  stopOntologySim();

  if (view.nodes.length === 0) {
    dom.ontologyGraph.innerHTML = '';
    dom.ontologyGraph.removeAttribute('viewBox');
    dom.ontologyEmpty.style.display = 'flex';
    return;
  }
  dom.ontologyEmpty.style.display = 'none';

  const visibleNodeIds = new Set(view.nodes.map((node) => node.id));
  if (state.ontologySelection && !visibleNodeIds.has(state.ontologySelection)) {
    state.ontologySelection = null;
  }

  const shell = dom.ontologyGraph.parentElement;
  const width = Math.max(640, shell?.clientWidth || 960);
  const height = Math.max(420, shell?.clientHeight || 560);
  dom.ontologyGraph.setAttribute('viewBox', `0 0 ${width} ${height}`);

  const sim = createOntologySim(view.nodes, view.edges, width, height);
  // 邻接表：悬停高亮时快速判断谁是选中/悬停节点的直接邻居。
  const neighbors = new Map(sim.nodes.map((n) => [n.id, new Set()]));
  sim.edges.forEach((e) => {
    neighbors.get(e.s.id)?.add(e.t.id);
    neighbors.get(e.t.id)?.add(e.s.id);
  });

  // 视图变换（缩放/平移）挂在最外层 <g> 上
  const viewState = { k: 1, tx: 0, ty: 0 };
  dom.ontologyGraph.innerHTML = `
    <g class="ontology-viewport">
      <g class="ontology-layer ontology-layer-edges"></g>
      <g class="ontology-layer ontology-layer-nodes"></g>
    </g>`;
  const viewport = dom.ontologyGraph.querySelector('.ontology-viewport');
  const edgeLayer = dom.ontologyGraph.querySelector('.ontology-layer-edges');
  const nodeLayer = dom.ontologyGraph.querySelector('.ontology-layer-nodes');

  const SVGNS = 'http://www.w3.org/2000/svg';
  const edgeEls = sim.edges.map((edge) => {
    const line = document.createElementNS(SVGNS, 'line');
    line.setAttribute('class', `ontology-edge-line ${edgeStrokeClass(edge)}`);
    edgeLayer.appendChild(line);
    return { edge, line };
  });
  const nodeEls = sim.nodes.map((node) => {
    const g = document.createElementNS(SVGNS, 'g');
    g.setAttribute('class', `ontology-node-group ontology-node-${node.category}`);
    g.dataset.nodeId = node.id;
    const circle = document.createElementNS(SVGNS, 'circle');
    circle.setAttribute('r', String(node.radius));
    circle.setAttribute('class', 'ontology-node-circle');
    const label = document.createElementNS(SVGNS, 'text');
    label.setAttribute('class', 'ontology-node-label');
    label.setAttribute('y', String(node.radius + 13));
    label.textContent = compactOntologyLabel(node.label);
    g.appendChild(circle);
    g.appendChild(label);
    nodeLayer.appendChild(g);
    return { node, g };
  });

  function applyViewTransform() {
    viewport.setAttribute('transform', `translate(${viewState.tx}, ${viewState.ty}) scale(${viewState.k})`);
  }
  function paint() {
    edgeEls.forEach(({ edge, line }) => {
      line.setAttribute('x1', edge.s.x); line.setAttribute('y1', edge.s.y);
      line.setAttribute('x2', edge.t.x); line.setAttribute('y2', edge.t.y);
    });
    nodeEls.forEach(({ node, g }) => {
      g.setAttribute('transform', `translate(${node.x}, ${node.y})`);
      g.classList.toggle('selected', node.id === state.ontologySelection);
    });
  }

  // 悬停高亮：非邻居节点+边淡出
  function applyHighlight(focusId) {
    if (!focusId) {
      nodeEls.forEach(({ g }) => g.classList.remove('dimmed', 'highlight'));
      edgeEls.forEach(({ line }) => line.classList.remove('dimmed', 'highlight'));
      return;
    }
    const near = neighbors.get(focusId) || new Set();
    nodeEls.forEach(({ node, g }) => {
      const on = node.id === focusId || near.has(node.id);
      g.classList.toggle('highlight', on);
      g.classList.toggle('dimmed', !on);
    });
    edgeEls.forEach(({ edge, line }) => {
      const on = edge.s.id === focusId || edge.t.id === focusId;
      line.classList.toggle('highlight', on);
      line.classList.toggle('dimmed', !on);
    });
  }

  ontologyWireInteractions(dom.ontologyGraph, sim, nodeEls, viewState, applyViewTransform, applyHighlight, paint);

  // 仿真主循环：迭代到能量足够低就停，省电。
  const runSim = () => {
    for (let s = 0; s < 2; s++) tickOntologySim(sim);
    paint();
    if (sim.alpha > 0.02) {
      state.ontologySim.raf = requestAnimationFrame(runSim);
    } else {
      state.ontologySim.raf = null;
    }
  };
  state.ontologySim = { sim, raf: null };
  applyViewTransform();
  runSim();
}

// 缩放/平移/拖拽节点/悬停 的事件绑定
function ontologyWireInteractions(svg, sim, nodeEls, viewState, applyViewTransform, applyHighlight, paint) {
  const nodeById = new Map(nodeEls.map(({ node, g }) => [node.id, { node, g }]));

  // 屏幕坐标 → 图坐标（考虑当前缩放/平移）
  function toGraph(evt) {
    const rect = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const sx = (evt.clientX - rect.left) / rect.width * (vb.width || rect.width);
    const sy = (evt.clientY - rect.top) / rect.height * (vb.height || rect.height);
    return { x: (sx - viewState.tx) / viewState.k, y: (sy - viewState.ty) / viewState.k };
  }

  // 滚轮缩放（以光标为中心）
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const px = (e.clientX - rect.left) / rect.width * (vb.width || rect.width);
    const py = (e.clientY - rect.top) / rect.height * (vb.height || rect.height);
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const k = Math.min(4, Math.max(0.25, viewState.k * factor));
    viewState.tx = px - (px - viewState.tx) * (k / viewState.k);
    viewState.ty = py - (py - viewState.ty) * (k / viewState.k);
    viewState.k = k;
    applyViewTransform();
  }, { passive: false });

  let drag = null;  // { type: 'node'|'pan', ... }
  svg.addEventListener('pointerdown', (e) => {
    const g = e.target.closest?.('.ontology-node-group');
    if (g) {
      const entry = nodeById.get(g.dataset.nodeId);
      if (entry) {
        entry.node.fixed = true;
        drag = { type: 'node', entry, moved: false };
        svg.setPointerCapture(e.pointerId);
      }
    } else {
      drag = { type: 'pan', x: e.clientX, y: e.clientY, tx: viewState.tx, ty: viewState.ty, moved: false };
      svg.setPointerCapture(e.pointerId);
    }
  });
  svg.addEventListener('pointermove', (e) => {
    if (!drag) {
      // 无拖拽时：悬停高亮
      const g = e.target.closest?.('.ontology-node-group');
      applyHighlight(g ? g.dataset.nodeId : null);
      return;
    }
    if (drag.type === 'node') {
      const p = toGraph(e);
      drag.entry.node.x = p.x; drag.entry.node.y = p.y;
      drag.entry.node.vx = 0; drag.entry.node.vy = 0;
      drag.moved = true;
      sim.alpha = Math.max(sim.alpha, 0.3);
      if (!state.ontologySim.raf) {
        const kick = () => { for (let s = 0; s < 2; s++) tickOntologySim(sim); paint();
          if (sim.alpha > 0.02) state.ontologySim.raf = requestAnimationFrame(kick); else state.ontologySim.raf = null; };
        state.ontologySim.raf = requestAnimationFrame(kick);
      }
      paint();
    } else {
      viewState.tx = drag.tx + (e.clientX - drag.x);
      viewState.ty = drag.ty + (e.clientY - drag.y);
      drag.moved = true;
      applyViewTransform();
    }
  });
  const endDrag = (e) => {
    if (!drag) return;
    if (drag.type === 'node') {
      drag.entry.node.fixed = false;
      if (!drag.moved) {   // 没拖动=点击选中
        state.ontologySelection = drag.entry.node.id;
        renderOntologyDetail(getOntologyView());
        paint();
      }
    }
    try { svg.releasePointerCapture(e.pointerId); } catch (_) {}
    drag = null;
  };
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);
  svg.addEventListener('pointerleave', () => applyHighlight(null));
}

function formatOntologyProperties(properties) {
  const entries = Object.entries(properties || {});
  if (entries.length === 0) {
    return '<div class="ontology-inline-empty">无属性</div>';
  }
  return entries.map(([key, value]) => `
    <div class="ontology-kv-row">
      <span class="ontology-kv-key">${esc(key)}</span>
      <span class="ontology-kv-value">${esc(typeof value === 'string' ? value : JSON.stringify(value))}</span>
    </div>
  `).join('');
}

function renderOntologyDetail(view) {
  if (!dom.ontologyDetail) return;
  const nodes = view.nodes || [];
  const selected = nodes.find((node) => node.id === state.ontologySelection) || nodes[0] || null;
  if (!selected) {
    dom.ontologyDetail.innerHTML = `
      <div class="ontology-detail-title">暂无节点详情</div>
      <div class="ontology-detail-body">当前筛选条件下没有可查看的节点。</div>
    `;
    return;
  }

  if (!state.ontologySelection) {
    state.ontologySelection = selected.id;
  }

  const relatedEdges = view.edges.filter((edge) => edge.source === selected.id || edge.target === selected.id);
  const relatedMarkup = relatedEdges.length === 0
    ? '<div class="ontology-inline-empty">无直接连接</div>'
    : relatedEdges.map((edge) => {
        const peer = edge.source === selected.id ? edge.target : edge.source;
        const peerNode = nodes.find((node) => node.id === peer)
          || (state.ontologyGraph.nodes || []).find((node) => node.id === peer);
        return `
          <div class="ontology-edge-chip ${edge.structural ? 'structural' : 'relation'}">
            <span>${esc(edge.label || edge.relation_type || '')}</span>
            <strong>${esc(peerNode?.label || peer)}</strong>
          </div>
        `;
      }).join('');

  dom.ontologyDetail.innerHTML = `
    <div class="ontology-detail-head">
      <div>
        <div class="ontology-detail-title">${esc(selected.label)}</div>
        <div class="ontology-detail-subtitle">${selected.category === 'concept' ? '概念节点' : '个体节点'}</div>
      </div>
      <div class="ontology-detail-badge">${relatedEdges.length} 连接</div>
    </div>
    ${selected.description ? `<div class="ontology-detail-body">${esc(selected.description)}</div>` : ''}
    <div class="ontology-detail-grid">
      <div class="ontology-detail-panel">
        <div class="ontology-detail-panel-title">归属</div>
        <div class="ontology-detail-panel-body">
          ${selected.category === 'concept'
            ? ((selected.parent_labels || []).length ? selected.parent_labels.map(label => `<span class="ontology-chip">${esc(label)}</span>`).join('') : '<div class="ontology-inline-empty">顶层概念</div>')
            : ((selected.concept_labels || []).length ? selected.concept_labels.map(label => `<span class="ontology-chip">${esc(label)}</span>`).join('') : '<div class="ontology-inline-empty">未绑定概念</div>')}
        </div>
      </div>
      <div class="ontology-detail-panel">
        <div class="ontology-detail-panel-title">属性</div>
        <div class="ontology-detail-panel-body ontology-properties">
          ${formatOntologyProperties(selected.properties)}
        </div>
      </div>
    </div>
    <div class="ontology-detail-panel">
      <div class="ontology-detail-panel-title">连接关系</div>
      <div class="ontology-detail-panel-body ontology-edges-list">${relatedMarkup}</div>
    </div>
  `;
}

let ontologySearchTimer = null;
dom.ontologySearch?.addEventListener('input', () => {
  clearTimeout(ontologySearchTimer);
  ontologySearchTimer = setTimeout(() => renderOntology(), 180);
});
dom.ontologyFilter?.addEventListener('change', () => renderOntology());
$('#btn-refresh-ontology')?.addEventListener('click', loadOntology);
