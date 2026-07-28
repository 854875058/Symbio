/* ============================================
   Symbio UI — Skills 页：本地技能、技能市场、技能详情
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Skills Page ============
async function loadSkills(query) {
  showLoading(dom.skillsGrid, query ? '搜索 Skills...' : '加载 Skills...');
  try {
    const url = query ? `${API}/skills/search?q=${encodeURIComponent(query)}` : `${API}/skills`;
    const res = await fetch(url);
    const data = await res.json();
    state.skills = data.skills || [];
    renderSkills(query);
  } catch (e) {
    toast('error', '加载 Skills 失败', e.message);
    dom.skillsGrid.innerHTML = `
      <div class="empty-block is-error">
        <p class="empty-block-title">无法加载 Skills</p>
        <p class="empty-block-hint">${esc(e.message)}</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="loadSkills()">重试</button>
        </div>
      </div>`;
  }
}

function renderSkills(query) {
  if (state.skills.length === 0) {
    dom.skillsGrid.innerHTML = query ? `
      <div class="empty-block">
        <p class="empty-block-title">没有匹配「${esc(query)}」的 Skill</p>
        <p class="empty-block-hint">可以清空搜索框浏览全部已安装的 Skill，或去下方的市场看看有没有现成的。</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="document.getElementById('skills-search').value='';loadSkills()">查看全部</button>
        </div>
      </div>` : `
      <div class="empty-block">
        <p class="empty-block-title">还没有安装任何 Skill</p>
        <p class="empty-block-hint">Skill 是 Agent 的可复用能力包：一段写好的流程 + 它需要的脚本和资源。装上之后，Agent 遇到对应场景会自己调用，不需要你每次重复交代。可以自动检测本机已有的，也可以从目录导入或去市场安装。</p>
        <div class="empty-block-actions">
          <button class="btn-primary" type="button" onclick="document.getElementById('btn-auto-detect')?.click()">自动检测已安装的 Skills</button>
          <button class="btn-outline" type="button" onclick="document.getElementById('btn-import-dir')?.click()">从目录导入</button>
        </div>
      </div>`;
    return;
  }

  dom.skillsGrid.innerHTML = state.skills.map(sk => `
    <div class="skill-card" data-id="${sk.id}" onclick="showSkillDetailPage('${escJs(sk.id)}')">
      <div class="skill-card-header">
        <div class="skill-card-info">
          <div class="skill-card-name">
            <span class="skill-icon-wrap">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
            </span>
            <span class="skill-name-text" title="${esc(sk.name)}">${esc(sk.name)}</span>
          </div>
          <div class="skill-card-version">v${esc(sk.version)}</div>
        </div>
        <span class="skill-source-badge skill-source-${sk.source}" title="${esc(sk.source)}">${esc(sk.source)}</span>
      </div>
      <div class="skill-card-desc">${esc(sk.description || '暂无描述')}</div>
      <div class="skill-card-meta">
        <div class="skill-keywords">
          ${(sk.trigger_keywords || []).slice(0, 4).map(k => `<span class="skill-keyword">${esc(k)}</span>`).join('')}
          ${(sk.trigger_keywords || []).length > 4 ? `<span class="skill-keyword skill-keyword-more">+${(sk.trigger_keywords || []).length - 4}</span>` : ''}
        </div>
        <span class="badge ${sk.enabled ? 'badge-green' : 'badge-gray'}">${sk.enabled ? '启用' : '禁用'}</span>
      </div>
      ${sk.relevance !== undefined ? `<div class="skill-relevance">匹配度 ${(sk.relevance * 100).toFixed(0)}%</div>` : ''}
      <div class="skill-card-actions" onclick="event.stopPropagation()">
        <button class="skill-action-btn" onclick="showSkillDetail('${escJs(sk.id)}')" title="查看详情" aria-label="查看技能 ${esc(sk.name || sk.id)} 详情">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
        <button class="skill-action-btn" onclick="editSkill('${escJs(sk.id)}')" title="编辑" aria-label="编辑技能 ${esc(sk.name || sk.id)}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
        <button class="skill-action-btn skill-action-danger" onclick="deleteSkill('${escJs(sk.id)}')" title="删除" aria-label="删除技能 ${esc(sk.name || sk.id)}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
        </button>
      </div>
    </div>
  `).join('');
}

function setSkillsMode(mode) {
  const nextMode = mode === 'marketplace' ? 'marketplace' : 'local';
  state.skillMode = nextMode;

  dom.skillsModeTabs?.querySelectorAll('[data-skill-mode]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.skillMode === nextMode);
    btn.setAttribute('aria-selected', btn.dataset.skillMode === nextMode ? 'true' : 'false');
  });

  if (dom.skillsGrid) dom.skillsGrid.style.display = nextMode === 'local' ? '' : 'none';
  if (dom.marketplaceShell) dom.marketplaceShell.style.display = nextMode === 'marketplace' ? 'flex' : 'none';
  const detailPage = document.getElementById('skill-detail-page');
  if (detailPage && nextMode === 'marketplace') detailPage.style.display = 'none';

  ['btn-auto-detect', 'btn-import-dir', 'btn-create-skill'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.style.display = nextMode === 'local' ? '' : 'none';
  });

  const query = dom.skillsSearch?.value.trim() || undefined;
  if (nextMode === 'marketplace') {
    loadMarketplace(query);
    // 首次进入 Marketplace 自动拉一次官方 anthropics/skills 网络列表，
    // 否则网络区一直空着，看起来像没接入。只自动拉一次，避免重复请求。
    if (!state.marketplace.remoteAutoLoaded) {
      state.marketplace.remoteAutoLoaded = true;
      searchRemoteSkills();
    }
  } else {
    loadSkills(query);
  }
}

async function loadMarketplace(query) {
  if (!dom.marketplaceGrid) return;
  showLoading(dom.marketplaceGrid, query ? '搜索 Skill 市场...' : '加载 Skill 市场...');
  try {
    const url = query ? `${API}/skills/marketplace?q=${encodeURIComponent(query)}` : `${API}/skills/marketplace`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.marketplace = {
      packages: data.packages || [],
      stats: data.stats || {},
      installed: data.installed || [],
      categories: data.categories || [],
      popularTags: data.popular_tags || [],
      total: data.total || 0,
      remoteAutoLoaded: state.marketplace.remoteAutoLoaded,
    };
    renderMarketplace(query);
  } catch (e) {
    toast('error', '加载市场失败', e.message);
    dom.marketplaceGrid.innerHTML = `
      <div class="empty-block is-error">
        <p class="empty-block-title">无法加载 Skill 市场</p>
        <p class="empty-block-hint">${esc(e.message)}</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="loadMarketplace()">重试</button>
        </div>
      </div>`;
  }
}

function renderMarketplace(query) {
  const packages = state.marketplace.packages || [];
  const installedIds = new Set((state.marketplace.installed || [])
    .filter(record => record.status === 'installed')
    .map(record => record.package_id));
  const stats = state.marketplace.stats || {};

  if (dom.marketplaceSummary) {
    const categories = Object.keys(stats.packages_by_category || {}).slice(0, 4);
    dom.marketplaceSummary.innerHTML = `
      <div class="marketplace-stat">
        <span class="marketplace-stat-value">${stats.total_packages ?? packages.length}</span>
        <span class="marketplace-stat-label">可安装</span>
      </div>
      <div class="marketplace-stat">
        <span class="marketplace-stat-value">${state.marketplace.installed?.length || 0}</span>
        <span class="marketplace-stat-label">已安装</span>
      </div>
      <div class="marketplace-stat marketplace-stat-wide">
        <span class="marketplace-stat-value">${categories.length ? categories.map(esc).join(' / ') : '内置registry'}</span>
        <span class="marketplace-stat-label">分类</span>
      </div>
    `;
  }

  if (packages.length === 0) {
    dom.marketplaceGrid.innerHTML = query ? `
      <div class="empty-block">
        <p class="empty-block-title">市场里没有匹配「${esc(query)}」的包</p>
        <p class="empty-block-hint">本地 registry 收录的包有限。可以在上方「网络 Skills」里填 GitHub 仓库（默认 anthropics/skills），从真实仓库拉取更多。</p>
      </div>` : `
      <div class="empty-block">
        <p class="empty-block-title">市场暂无可安装的包</p>
        <p class="empty-block-hint">本地 registry 是空的。在上方「网络 Skills」填入一个 GitHub 仓库即可接入真实的 Agent Skills 列表。</p>
      </div>`;
    return;
  }

  dom.marketplaceGrid.innerHTML = packages.map(pkg => {
    const installed = installedIds.has(pkg.package_id);
    const tags = (pkg.tags || []).slice(0, 5);
    const categories = (pkg.categories || []).slice(0, 3);
    const title = pkg.display_name || pkg.name;
    return `
      <div class="marketplace-card" data-package-id="${esc(pkg.package_id)}">
        <div class="marketplace-card-main">
          <div class="marketplace-card-head">
            <div class="marketplace-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/></svg>
            </div>
            <div class="marketplace-title-wrap">
              <div class="marketplace-title" title="${esc(title)}">${esc(title)}</div>
              <div class="marketplace-subtitle">v${esc(pkg.version || '1.0.0')} · ${esc(pkg.author || 'Symbio')}</div>
            </div>
            <span class="marketplace-status ${installed ? 'installed' : ''}">${installed ? '已安装' : '可安装'}</span>
          </div>
          <div class="marketplace-description">${esc(pkg.description || '（无描述）')}</div>
          <div class="marketplace-tags">
            ${categories.map(tag => `<span class="marketplace-tag category">${esc(tag)}</span>`).join('')}
            ${tags.map(tag => `<span class="marketplace-tag">${esc(tag)}</span>`).join('')}
          </div>
        </div>
        <div class="marketplace-card-footer">
          <div class="marketplace-metrics">
            <span>${Number(pkg.downloads || 0).toLocaleString()} 次下载</span>
            <span>评分 ${Number(pkg.rating || 0).toFixed(1)}</span>
          </div>
          <button class="btn-primary marketplace-install-btn" type="button" data-package-install="${esc(pkg.package_id)}" ${installed ? 'disabled' : ''}>
            ${installed ? '已安装' : '安装'}
          </button>
        </div>
      </div>
    `;
  }).join('');

  dom.marketplaceGrid.querySelectorAll('[data-package-install]').forEach(btn => {
    btn.addEventListener('click', () => installMarketplaceSkill(btn.dataset.packageInstall, btn));
  });
}

async function installMarketplaceSkill(packageId, button) {
  if (!packageId) return;
  const previousText = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = 'Installing...';
  }
  try {
    const res = await fetch(`${API}/skills/marketplace/${encodeURIComponent(packageId)}/install`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.detail || data.record?.error || `HTTP ${res.status}`);
    toast('success', 'Skill installed', `${data.record.package_name} is ready locally`);
    await loadMarketplace(dom.skillsSearch?.value.trim() || undefined);
    await loadSkills();
  } catch (e) {
    toast('error', 'Install failed', e.message);
    if (button) {
      button.disabled = false;
      button.textContent = previousText || 'Install';
    }
  }
}

// ===== 网络 Skills（从 GitHub 接入，批次D2c） =====
async function searchRemoteSkills() {
  const box = $('#marketplace-remote-results');
  if (!box) return;
  const repo = ($('#remote-skill-repo')?.value || '').trim();
  const q = ($('#remote-skill-q')?.value || '').trim();
  box.innerHTML = `<div class="empty-hint" style="padding:8px">正在从 GitHub 拉取 Skills…（默认官方 anthropics/skills）</div>`;
  try {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (repo) params.set('repo', repo);
    // GitHub git-tree 走代理可能较慢，给 45s 超时避免无限转圈
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45000);
    let res;
    try {
      res = await fetch(`${API}/skills/marketplace/remote?${params.toString()}`, { signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderRemoteSkills(data.skills || [], data.repo);
  } catch (e) {
    const msg = e.name === 'AbortError' ? '拉取超时（网络/代理较慢），可点「搜索网络」重试' : `搜索失败：${e.message}`;
    box.innerHTML = `<div class="empty-hint" style="padding:8px">${esc(msg)}</div>`;
  }
}

function renderRemoteSkills(skills, repo) {
  const box = $('#marketplace-remote-results');
  if (!box) return;
  if (!skills.length) {
    box.innerHTML = `<div class="empty-hint" style="padding:8px">没有匹配的网络 Skills</div>`;
    return;
  }
  box.innerHTML = skills.map((s, i) => `
    <div class="remote-skill-card">
      <div class="remote-skill-info">
        <a class="remote-skill-name" href="${esc(s.html_url || '#')}" target="_blank" rel="noopener">${esc(s.name)}</a>
        <span class="remote-skill-repo">${esc(s.repo)}</span>
      </div>
      <button class="btn-outline remote-skill-install" type="button" data-i="${i}">接入</button>
    </div>`).join('');
  box.querySelectorAll('.remote-skill-install').forEach(btn => {
    btn.addEventListener('click', () => installRemoteSkill(skills[Number(btn.dataset.i)], btn));
  });
}

async function installRemoteSkill(skill, button) {
  if (!skill) return;
  const prev = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = '接入中…'; }
  // 大技能（如 docx，含 scripts/references 多文件）逐个走 GitHub raw 拉取较慢，
  // 给 180s 超时；超时不代表失败，服务端可能仍在拉，故文案区分对待。
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 180000);
  try {
    const res = await fetch(`${API}/skills/marketplace/remote/install`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo: skill.repo, path: skill.path, name: skill.name, ref: skill.ref || 'main', html_url: skill.html_url || '' }),
      signal: ctrl.signal,
    });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.detail || data.record?.error || `HTTP ${res.status}`);
    toast('success', '已接入网络 Skill', `${skill.name} 已拉取并安装到本地市场`);
    if (button) button.textContent = '已接入';
    await loadMarketplace(dom.skillsSearch?.value.trim() || undefined);
  } catch (e) {
    const msg = e.name === 'AbortError'
      ? '接入超时：该技能文件较多、网络较慢，服务端可能仍在拉取，稍后刷新市场看是否已装上'
      : e.message;
    toast('error', '接入失败', msg);
    if (button) { button.disabled = false; button.textContent = prev || '接入'; }
  } finally {
    clearTimeout(timer);
  }
}

$('#btn-remote-search')?.addEventListener('click', searchRemoteSkills);
$('#remote-skill-q')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') searchRemoteSkills(); });
$('#remote-skill-repo')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') searchRemoteSkills(); });

// Skills search
let skillsSearchTimer = null;
dom.skillsSearch?.addEventListener('input', () => {
  clearTimeout(skillsSearchTimer);
  skillsSearchTimer = setTimeout(() => {
    const q = dom.skillsSearch.value.trim();
    if (state.skillMode === 'marketplace') {
      loadMarketplace(q || undefined);
    } else {
      loadSkills(q || undefined);
    }
  }, 300);
});

dom.skillsModeTabs?.addEventListener('click', (event) => {
  const btn = event.target.closest('[data-skill-mode]');
  if (!btn) return;
  setSkillsMode(btn.dataset.skillMode);
});

// Skills action buttons
document.getElementById('btn-auto-detect')?.addEventListener('click', autoDetectSkills);
document.getElementById('btn-import-dir')?.addEventListener('click', showImportDirModal);
document.getElementById('btn-create-skill')?.addEventListener('click', showCreateSkillModal);

async function autoDetectSkills() {
  toast('info', '正在扫描...', '检测已安装的 Claude Code、Codex 等 Skills');
  try {
    const res = await fetch(`${API}/skills/auto-detect`, { method: 'POST' });
    const data = await res.json();
    if (data.found > 0) {
      toast('success', '发现 Skills', `找到 ${data.found} 个新 Skill，已导入`);
      loadSkills();
    } else {
      toast('info', '未发现新 Skills', '未检测到新的已安装 Skills');
    }
  } catch (e) {
    toast('error', '检测失败', e.message);
  }
}

function showImportDirModal() {
  document.querySelector('.modal-overlay')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>从目录导入 Skills</h3>
        <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>目录路径</label>
          <input type="text" id="modal-dir-path" placeholder="例: /home/user/.claude/skills 或 C:\Users\skills">
        </div>
        <p style="font-size:var(--fs-xs);color:var(--text-tertiary);margin-top:8px;">
          支持导入 Claude Code、Codex 等工具的 Skills 目录
        </p>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">导入</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const dirPath = overlay.querySelector('#modal-dir-path').value.trim();
    if (!dirPath) { toast('error', '验证失败', '请输入目录路径'); return; }
    try {
      const res = await fetch(`${API}/skills/import-dir`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: dirPath }),
      });
      const data = await res.json();
      overlay.remove();
      if (data.imported > 0) {
        toast('success', '导入成功', `从 ${dirPath} 导入了 ${data.imported} 个 Skills`);
        loadSkills();
      } else {
        toast('info', '未发现 Skills', '该目录下未找到有效的 Skill 定义文件');
      }
    } catch (e) {
      toast('error', '导入失败', e.message);
    }
  });
}

function showCreateSkillModal() {
  showImportSkillModal();
}

function showImportSkillModal() {
  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>导入 Skill</h3>
        <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Skill 名称</label>
          <input type="text" id="modal-skill-name" placeholder="例: my-custom-skill">
        </div>
        <div class="form-group">
          <label>描述</label>
          <input type="text" id="modal-skill-desc" placeholder="简要描述 Skill 功能">
        </div>
        <div class="form-group">
          <label>版本</label>
          <input type="text" id="modal-skill-version" placeholder="1.0.0" value="1.0.0">
        </div>
        <div class="form-group">
          <label>来源</label>
          <select id="modal-skill-source">
            <option value="custom">自定义</option>
            <option value="external">外部</option>
            <option value="builtin">内置</option>
          </select>
        </div>
        <div class="form-group">
          <label>触发关键词（逗号分隔）</label>
          <input type="text" id="modal-skill-keywords" placeholder="关键词1, 关键词2">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">导入</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const name = overlay.querySelector('#modal-skill-name').value.trim();
    const description = overlay.querySelector('#modal-skill-desc').value.trim();
    const version = overlay.querySelector('#modal-skill-version').value.trim() || '1.0.0';
    const source = overlay.querySelector('#modal-skill-source').value;
    const keywordsRaw = overlay.querySelector('#modal-skill-keywords').value.trim();
    const keywords = keywordsRaw ? keywordsRaw.split(/[,，]/).map(k => k.trim()).filter(Boolean) : [];

    if (!name) {
      toast('error', '验证失败', 'Skill 名称不能为空');
      return;
    }

    try {
      const res = await fetch(`${API}/skills/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          description,
          version,
          source,
          enabled: true,
          trigger_keywords: keywords,
        }),
      });

      if (res.ok) {
        overlay.remove();
        toast('success', '已导入', `Skill ${name} 已导入`);
        loadSkills();
      } else {
        const data = await res.json();
        toast('error', '导入失败', data.detail || '未知错误');
      }
    } catch (e) {
      toast('error', '导入失败', e.message);
    }
  });
}

// Skill Detail
function showSkillDetail(id) {
  const sk = state.skills.find(s => s.id === id);
  if (!sk) return;
  document.querySelector('.modal-overlay')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal modal-wide">
      <div class="modal-header">
        <h3>${esc(sk.name)}</h3>
        <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-grid">
          <div class="detail-item"><label>版本</label><span>v${esc(sk.version)}</span></div>
          <div class="detail-item"><label>来源</label><span class="skill-source-badge skill-source-${sk.source}">${esc(sk.source)}</span></div>
          <div class="detail-item"><label>状态</label><span class="badge ${sk.enabled ? 'badge-green' : 'badge-gray'}">${sk.enabled ? '启用' : '禁用'}</span></div>
          <div class="detail-item"><label>创建时间</label><span>${esc(sk.created_at || '未知')}</span></div>
        </div>
        <div class="detail-section">
          <label>描述</label>
          <p>${esc(sk.description || '暂无描述')}</p>
        </div>
        ${(sk.trigger_keywords && sk.trigger_keywords.length) ? `
        <div class="detail-section">
          <label>触发关键词</label>
          <div class="skill-keywords">${sk.trigger_keywords.map(k => `<span class="skill-keyword">${esc(k)}</span>`).join('')}</div>
        </div>` : ''}
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">关闭</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// Edit Skill
function editSkill(id) {
  const sk = state.skills.find(s => s.id === id);
  if (!sk) return;
  document.querySelector('.modal-overlay')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h3>编辑 Skill</h3>
        <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Skill 名称</label>
          <input type="text" id="edit-skill-name" value="${esc(sk.name)}">
        </div>
        <div class="form-group">
          <label>描述</label>
          <textarea id="edit-skill-desc">${esc(sk.description || '')}</textarea>
        </div>
        <div class="form-group">
          <label>版本</label>
          <input type="text" id="edit-skill-version" value="${esc(sk.version)}">
        </div>
        <div class="form-group">
          <label>触发关键词（逗号分隔）</label>
          <input type="text" id="edit-skill-keywords" value="${(sk.trigger_keywords || []).join(', ')}">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">保存</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const name = overlay.querySelector('#edit-skill-name').value.trim();
    const description = overlay.querySelector('#edit-skill-desc').value.trim();
    const version = overlay.querySelector('#edit-skill-version').value.trim();
    const keywordsRaw = overlay.querySelector('#edit-skill-keywords').value.trim();
    const keywords = keywordsRaw ? keywordsRaw.split(/[,，]/).map(k => k.trim()).filter(Boolean) : [];
    if (!name) { toast('error', '验证失败', '名称不能为空'); return; }
    try {
      const res = await fetch(`${API}/skills/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description, version, trigger_keywords: keywords }),
      });
      if (res.ok) {
        overlay.remove();
        toast('success', '已更新', `Skill ${name} 已更新`);
        loadSkills();
      } else {
        const data = await res.json();
        toast('error', '更新失败', data.detail || '未知错误');
      }
    } catch (e) { toast('error', '更新失败', e.message); }
  });
}

// Delete Skill
async function deleteSkill(id) {
  const sk = state.skills.find(s => s.id === id);
  if (!sk) return;
  if (!confirm(`确定要删除 Skill "${sk.name}" 吗？`)) return;
  try {
    const res = await fetch(`${API}/skills/${id}`, { method: 'DELETE' });
    if (res.ok) {
      toast('success', '已删除', `Skill ${sk.name} 已删除`);
      loadSkills();
    } else {
      toast('error', '删除失败', '无法删除该 Skill');
    }
  } catch (e) { toast('error', '删除失败', e.message); }
}

// ============ Skill Detail Page ============
async function showSkillDetailPage(id) {
  const grid = document.getElementById('skills-grid');
  const detail = document.getElementById('skill-detail-page');
  if (!grid || !detail) return;

  grid.style.display = 'none';
  detail.style.display = 'flex';

  // Fetch detail from API
  let skillData = null;
  try {
    const res = await fetch(`${API}/skills/${id}/detail`);
    if (res.ok) skillData = await res.json();
  } catch(e) {}

  // Fallback to local data
  if (!skillData) {
    const sk = state.skills.find(s => s.id === id);
    if (sk) skillData = { skill: sk, files: [], readme: null, manifest: null, prompts: [], tests: [] };
  }
  if (!skillData || !skillData.skill) {
    toast('error', '加载失败', '无法获取 Skill 详情');
    backToSkillsGrid();
    return;
  }

  state.skillDetail = skillData;
  renderSkillDetailHeader(skillData.skill);
  renderSkillOverview(skillData);
  renderSkillDocs(skillData);
  renderSkillFiles(skillData, id);
  renderSkillConfig(skillData);
  renderSkillTests(skillData);
}

function backToSkillsGrid() {
  document.getElementById('skills-grid').style.display = '';
  document.getElementById('skill-detail-page').style.display = 'none';
  state.skillDetail = null;
}

function renderSkillDetailHeader(sk) {
  const el = document.getElementById('skill-detail-header');
  if (!el) return;
  el.innerHTML = `
    <div class="sdh-info">
      <div class="sdh-title">
        <h1>${esc(sk.name)}</h1>
        <span class="skill-version-badge">v${esc(sk.version)}</span>
        <span class="skill-source-badge skill-source-${sk.source}">${esc(sk.source)}</span>
      </div>
      <p class="sdh-desc">${esc(sk.description || '暂无描述')}</p>
      <div class="sdh-meta">
        <span class="sdh-meta-item">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          ${esc(sk.created_at || '未知')}
        </span>
        <span class="sdh-meta-item badge ${sk.enabled ? 'badge-green' : 'badge-gray'}">${sk.enabled ? '已启用' : '已禁用'}</span>
      </div>
      ${(sk.trigger_keywords && sk.trigger_keywords.length) ? `
      <div class="sdh-keywords">${sk.trigger_keywords.map(k => `<span class="skill-keyword">${esc(k)}</span>`).join('')}</div>
      ` : ''}
    </div>
  `;
}

function renderSkillOverview(data) {
  const el = document.getElementById('panel-overview');
  if (!el) return;
  const sk = data.skill;
  const manifest = data.manifest || {};

  let html = `<div class="skill-overview-grid">`;

  // Description card
  html += `<div class="so-card"><h4>描述</h4><p>${esc(sk.description || '暂无描述')}</p></div>`;

  // Metadata card
  html += `<div class="so-card"><h4>基本信息</h4>
    <div class="so-meta"><span>名称</span><span>${esc(sk.name)}</span></div>
    <div class="so-meta"><span>版本</span><span>v${esc(sk.version)}</span></div>
    <div class="so-meta"><span>来源</span><span>${esc(sk.source)}</span></div>
    <div class="so-meta"><span>状态</span><span>${sk.enabled ? '启用' : '禁用'}</span></div>
    <div class="so-meta"><span>创建时间</span><span>${esc(sk.created_at || '未知')}</span></div>
  </div>`;

  // Manifest info
  if (manifest.author || manifest.license || manifest.dependencies) {
    html += `<div class="so-card"><h4>包信息</h4>`;
    if (manifest.author) html += `<div class="so-meta"><span>作者</span><span>${esc(manifest.author)}</span></div>`;
    if (manifest.license) html += `<div class="so-meta"><span>许可证</span><span>${esc(manifest.license)}</span></div>`;
    if (manifest.dependencies) {
      html += `<div class="so-deps"><h5>依赖</h5>`;
      for (const [dep, ver] of Object.entries(manifest.dependencies)) {
        html += `<span class="so-dep-tag">${esc(dep)}: ${esc(ver)}</span>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
  }

  // Directory info
  html += `<div class="so-card"><h4>目录</h4><p class="so-path">${esc(data.directory || '未找到本地目录')}</p></div>`;

  html += `</div>`;
  el.innerHTML = html;
}

function renderSkillDocs(data) {
  const el = document.getElementById('panel-docs');
  if (!el) return;
  if (data.readme) {
    el.innerHTML = `<div class="skill-doc-content">${formatContent(data.readme)}</div>`;
  } else {
    el.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">这个 Skill 没有说明文档</p>
        <p class="empty-block-hint">在 Skill 目录下创建 skill.md 或 README.md，内容会渲染在这里。Agent 也会读它来判断何时该用这个 Skill，所以写清楚适用场景很有价值。</p>
      </div>`;
  }
}

function renderSkillFiles(data, skillId) {
  const el = document.getElementById('panel-files');
  if (!el) return;

  if (!data.files || data.files.length === 0) {
    el.innerHTML = data.directory ? `
      <div class="empty-block is-inline">
        <p class="empty-block-title">Skill 目录是空的</p>
        <p class="empty-block-hint">目录存在但没有任何文件：<code>${esc(data.directory)}</code>。把脚本、模板、参考资料放进去，它们会一并作为这个 Skill 的资源被 Agent 使用。</p>
      </div>` : `
      <div class="empty-block is-inline is-error">
        <p class="empty-block-title">找不到这个 Skill 的目录</p>
        <p class="empty-block-hint">元数据里记录的路径在磁盘上不存在，可能是目录被移动或删除了。这个 Skill 现在无法真正执行，建议重新导入。</p>
      </div>`;
    return;
  }

  el.innerHTML = `
    <div class="file-split">
      <div class="file-tree" id="file-tree"></div>
      <div class="file-viewer" id="file-viewer">
        <div class="file-viewer-placeholder">&larr; 选择文件查看内容</div>
      </div>
    </div>
  `;

  renderFileTree(data.files, skillId);
}

function renderFileTree(files, skillId) {
  const tree = document.getElementById('file-tree');
  if (!tree) return;

  // Build tree structure
  const root = {};
  files.forEach(f => {
    const parts = f.name.split(/[\\/]/);
    let node = root;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        node[part] = { file: f };
      } else {
        if (!node[part] || node[part].file) node[part] = {};
        node = node[part];
      }
    });
  });

  tree.innerHTML = renderTreeNode(root, skillId, 0);
}

function renderTreeNode(node, skillId, depth) {
  let html = '';
  const entries = Object.entries(node).sort((a, b) => {
    const aIsDir = !a[1].file;
    const bIsDir = !b[1].file;
    if (aIsDir && !bIsDir) return -1;
    if (!aIsDir && bIsDir) return 1;
    return a[0].localeCompare(b[0]);
  });

  for (const [name, val] of entries) {
    const indent = depth * 16;
    if (val.file) {
      const icon = getFileIcon(val.file.type);
      html += `<div class="ft-item ft-file" style="padding-left:${indent + 8}px" onclick="loadSkillFile('${escJs(skillId)}', '${escJs(val.file.name)}')">
        ${icon}<span class="ft-name">${esc(name)}</span>
        <span class="ft-size">${formatFileSize(val.file.size)}</span>
      </div>`;
    } else {
      html += `<div class="ft-item ft-folder" style="padding-left:${indent}px" onclick="this.classList.toggle('ft-collapsed')">
        <span class="ft-arrow">&#9660;</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>
        <span class="ft-name">${esc(name)}</span>
      </div>
      <div class="ft-children">${renderTreeNode(val, skillId, depth + 1)}</div>`;
    }
  }
  return html;
}

function getFileIcon(type) {
  const icons = {
    markdown: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>',
    code: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 18l6-6-6-6"/><path d="M8 6l-6 6 6 6"/></svg>',
    config: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    text: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
    script: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    other: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  };
  return icons[type] || icons.other;
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function loadSkillFile(skillId, filePath) {
  const viewer = document.getElementById('file-viewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="file-viewer-loading">加载中...</div>';

  try {
    const res = await fetch(`${API}/skills/${skillId}/file?path=${encodeURIComponent(filePath)}`);
    if (!res.ok) throw new Error('加载失败');
    const data = await res.json();

    const isCode = /\.(py|js|ts|json|yaml|yml|sh|html|css|md)$/.test(filePath);

    viewer.innerHTML = `
      <div class="fv-header">
        <span class="fv-path">${esc(filePath)}</span>
        <span class="fv-size">${formatFileSize(data.size)}</span>
        <div class="fv-actions">
          <button class="fv-btn fv-copy" onclick="navigator.clipboard.writeText(document.querySelector('.fv-content-edit')?.value || document.querySelector('.fv-content')?.textContent)">复制</button>
          <button class="fv-btn fv-edit" onclick="toggleSkillFileEdit('${escJs(skillId)}', '${escJs(filePath)}')">编辑</button>
        </div>
      </div>
      <div class="fv-content" data-raw="${esc(data.content).replace(/"/g, '&quot;')}">${isCode ? highlightSyntax(data.content) : esc(data.content)}</div>
    `;
  } catch(e) {
    viewer.innerHTML = `<div class="file-viewer-error">${esc(e.message)}</div>`;
  }
}

function toggleSkillFileEdit(skillId, filePath) {
  const viewer = document.getElementById('file-viewer');
  if (!viewer) return;

  const contentEl = viewer.querySelector('.fv-content');
  const editBtn = viewer.querySelector('.fv-edit');
  const headerEl = viewer.querySelector('.fv-header');

  // Check if already in edit mode
  const textarea = viewer.querySelector('.fv-content-edit');
  if (textarea) {
    // Switch back to view mode - restore original content
    const raw = contentEl?.dataset?.raw || '';
    const isCode = /\.(py|js|ts|json|yaml|yml|sh|html|css|md)$/.test(filePath);
    const contentDiv = document.createElement('div');
    contentDiv.className = 'fv-content';
    contentDiv.dataset.raw = raw;
    contentDiv.innerHTML = isCode ? highlightSyntax(raw) : esc(raw);
    textarea.replaceWith(contentDiv);
    editBtn.textContent = '编辑';
    editBtn.classList.remove('fv-editing');
    // Remove save/cancel buttons
    viewer.querySelector('.fv-save')?.remove();
    viewer.querySelector('.fv-cancel')?.remove();
    return;
  }

  if (!contentEl) return;
  const raw = contentEl.dataset.raw || contentEl.textContent;

  // Create textarea
  const textareaEl = document.createElement('textarea');
  textareaEl.className = 'fv-content-edit';
  textareaEl.value = raw;
  textareaEl.spellcheck = false;
  contentEl.replaceWith(textareaEl);

  // Update edit button
  editBtn.textContent = '取消';
  editBtn.classList.add('fv-editing');

  // Add save button
  const actionsEl = viewer.querySelector('.fv-actions');
  if (!viewer.querySelector('.fv-save')) {
    const saveBtn = document.createElement('button');
    saveBtn.className = 'fv-btn fv-save';
    saveBtn.textContent = '保存';
    saveBtn.onclick = () => saveSkillFile(skillId, filePath, textareaEl.value);
    actionsEl.insertBefore(saveBtn, editBtn);
  }

  textareaEl.focus();
}

async function saveSkillFile(skillId, filePath, content) {
  try {
    const res = await fetch(`${API}/skills/${skillId}/file`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '保存失败');
    }
    toast('success', '已保存', `${filePath} 已更新`);
    // Reload the file view
    loadSkillFile(skillId, filePath);
  } catch(e) {
    toast('error', '保存失败', e.message);
  }
}

function renderSkillConfig(data) {
  const el = document.getElementById('panel-config');
  if (!el) return;
  const manifest = data.manifest;
  if (!manifest) {
    el.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">这个 Skill 没有配置文件</p>
        <p class="empty-block-hint">没有配置也能正常工作。如果需要声明依赖、参数或触发条件，在 Skill 目录下创建 skill.yaml 或 manifest.json。</p>
      </div>`;
    return;
  }

  el.innerHTML = `
    <div class="skill-config-section">
      <h4>Manifest 配置</h4>
      <div class="skill-config-viewer">${highlightSyntax(JSON.stringify(manifest, null, 2))}</div>
    </div>
  `;
}

function renderSkillTests(data) {
  const el = document.getElementById('panel-tests');
  if (!el) return;
  if (!data.tests || data.tests.length === 0) {
    el.innerHTML = `
      <div class="empty-block is-inline">
        <p class="empty-block-title">这个 Skill 没有自带测试</p>
        <p class="empty-block-hint">在 Skill 目录下放 test_*.py 或 *.test.js，就能在这一页直接跑，用来确认它改动之后还能正常工作。</p>
      </div>`;
    return;
  }

  el.innerHTML = data.tests.map(t => `
    <div class="skill-test-item">
      <div class="sti-header">
        <span class="sti-name">${esc(t.name)}</span>
      </div>
      <pre class="sti-content"><code>${highlightSyntax(t.content)}</code></pre>
    </div>
  `).join('');
}

// Skill Detail Tab switching
document.addEventListener('click', (e) => {
  const tab = e.target.closest('.skill-tab');
  if (!tab) return;
  const tabName = tab.dataset.tab;
  document.querySelectorAll('.skill-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.skill-tab-panel').forEach(p => p.classList.remove('active'));
  tab.classList.add('active');
  document.getElementById(`panel-${tabName}`)?.classList.add('active');
});
