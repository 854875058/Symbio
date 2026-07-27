/* ============================================
   Symbio UI — MCP 页：MCP 服务器管理
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ MCP Page ============
const mcpState = { servers: [] };

async function loadMCP() {
  await loadMCPServers();
}

async function loadMCPServers() {
  const list = document.getElementById('mcp-servers-list');
  if (!list) return;
  try {
    const res = await fetch(`${API}/mcp/servers`);
    const data = await res.json();
    mcpState.servers = data.servers || [];
    renderMCPServers(list, data.servers || []);
  } catch (e) {
    list.innerHTML = `<div class="empty-state-lg"><p>加载失败: ${esc(e.message)}</p></div>`;
  }
}

function renderMCPServers(container, servers) {
  if (!servers.length) {
    container.innerHTML = `<div class="empty-state-lg">
      <p>暂无 MCP Server 配置</p>
      <span class="empty-hint">添加 MCP server 让 Agent 使用标准 MCP 工具（如 filesystem、browser、database 等）</span>
    </div>`;
    return;
  }
  container.innerHTML = servers.map(s => `
    <div class="a2a-session-item">
      <div class="a2a-session-meta">
        <div class="a2a-session-name">${esc(s.name)}</div>
        <div class="a2a-session-url">${esc([s.command, ...(s.args || [])].join(' '))}</div>
        <div class="a2a-session-time">${esc(s.description || '')} ${s.source === 'yaml' ? '(来自 symbio.yaml)' : ''}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;align-items:center;flex-wrap:wrap;justify-content:flex-end">
        <button class="btn-outline" style="padding:4px 10px;font-size:var(--fs-xs)" onclick="probeMCPTools('${esc(s.id || '')}', '${esc(s.name)}')">探测工具</button>
        <button class="btn-outline" style="padding:4px 10px;font-size:var(--fs-xs)" onclick="probeMCPExtra('${esc(s.id || '')}', '${esc(s.name)}', 'resources')">资源</button>
        <button class="btn-outline" style="padding:4px 10px;font-size:var(--fs-xs)" onclick="probeMCPExtra('${esc(s.id || '')}', '${esc(s.name)}', 'prompts')">Prompts</button>
        <button class="btn-primary" style="padding:4px 10px;font-size:var(--fs-xs)" onclick="mountMCPServer('${esc(s.id || '')}', '${esc(s.name)}')">挂载到 Agent</button>
        ${s.source !== 'yaml' ? `<button class="btn-outline" style="padding:4px 10px;font-size:var(--fs-xs)" onclick="deleteMCPServer('${esc(s.id || '')}')">删除</button>` : ''}
      </div>
    </div>
  `).join('');
}

async function probeMCPTools(serverId, serverName) {
  if (!serverId) { toast('error', '无 server ID', '内置 yaml 配置暂不支持探测'); return; }
  const panel = document.getElementById('mcp-tools-panel');
  const titleEl = document.getElementById('mcp-tools-title');
  const listEl = document.getElementById('mcp-tools-list');
  if (!panel || !listEl) return;
  panel.style.display = 'block';
  if (titleEl) titleEl.textContent = `${serverName} — 探测中...`;
  listEl.innerHTML = '<div class="empty-state-lg"><p>连接中...</p></div>';
  try {
    const res = await fetch(`${API}/mcp/servers/${encodeURIComponent(serverId)}/tools`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const caps = (data.capabilities || []).join(' · ');
    if (titleEl) titleEl.textContent = `${serverName} — ${data.total} 个工具${caps ? '（能力：' + caps + '）' : ''}`;
    listEl.innerHTML = (data.tools || []).map(t => `
      <div class="a2a-task-item">
        <div class="a2a-task-meta">
          <div class="a2a-task-prompt" style="font-family:var(--font-mono);color:var(--accent)">${esc(t.name)}</div>
          <div style="font-size:var(--fs-sm);color:var(--text-secondary);margin-top:2px">${esc(t.description || '—')}</div>
        </div>
      </div>
    `).join('') || '<div class="empty-state-lg"><p>无可用工具</p></div>';
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state-lg"><p>探测失败: ${esc(e.message)}</p></div>`;
  }
}

async function probeMCPExtra(serverId, serverName, kind) {
  if (!serverId) { toast('error', '无 server ID', '内置 yaml 配置暂不支持探测'); return; }
  const panel = document.getElementById('mcp-tools-panel');
  const titleEl = document.getElementById('mcp-tools-title');
  const listEl = document.getElementById('mcp-tools-list');
  if (!panel || !listEl) return;
  panel.style.display = 'block';
  const label = kind === 'resources' ? '资源' : 'Prompts';
  if (titleEl) titleEl.textContent = `${serverName} — ${label} 探测中...`;
  listEl.innerHTML = '<div class="empty-state-lg"><p>连接中...</p></div>';
  try {
    const res = await fetch(`${API}/mcp/servers/${encodeURIComponent(serverId)}/${kind}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const items = data[kind] || [];
    if (titleEl) titleEl.textContent = `${serverName} — ${items.length} 个${label}${data.supported === false ? '（服务器未声明此能力）' : ''}`;
    listEl.innerHTML = items.map(it => `
      <div class="a2a-task-item">
        <div class="a2a-task-meta">
          <div class="a2a-task-prompt" style="font-family:var(--font-mono);color:var(--accent)">${esc(it.name || it.uri || '—')}</div>
          <div style="font-size:var(--fs-sm);color:var(--text-secondary);margin-top:2px">${esc(it.description || it.uri || '—')}</div>
        </div>
      </div>
    `).join('') || `<div class="empty-state-lg"><p>无${label}</p></div>`;
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state-lg"><p>探测失败: ${esc(e.message)}</p></div>`;
  }
}

async function mountMCPServer(serverId, serverName) {
  if (!serverId) { toast('error', '无 server ID', '内置 yaml 配置暂不支持挂载'); return; }
  try {
    const res = await fetch(`${API}/mcp/servers/${encodeURIComponent(serverId)}/mount`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    toast('success', `已挂载 ${data.total} 个工具`, `${serverName} 的工具现在可被 Agent 调用：${(data.mounted || []).join(', ')}`);
  } catch (e) {
    toast('error', '挂载失败', e.message);
  }
}

async function deleteMCPServer(serverId) {
  try {
    const res = await fetch(`${API}/mcp/servers/${encodeURIComponent(serverId)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('success', 'MCP Server 已删除', '');
    await loadMCPServers();
  } catch (e) { toast('error', '删除失败', e.message); }
}

document.getElementById('btn-add-mcp-server')?.addEventListener('click', () => {
  const form = document.getElementById('mcp-add-form');
  if (form) form.style.display = form.style.display === 'none' ? 'block' : 'none';
});
document.getElementById('btn-cancel-mcp-server')?.addEventListener('click', () => {
  document.getElementById('mcp-add-form').style.display = 'none';
});
document.getElementById('btn-save-mcp-server')?.addEventListener('click', async () => {
  const name = document.getElementById('mcp-name')?.value?.trim();
  const cmd = document.getElementById('mcp-command')?.value?.trim();
  const argsRaw = document.getElementById('mcp-args')?.value?.trim();
  const desc = document.getElementById('mcp-description')?.value?.trim();
  if (!name || !cmd) { toast('error', '请填写名称和命令', ''); return; }
  const args = argsRaw ? argsRaw.split(/\s+/) : [];
  try {
    const res = await fetch(`${API}/mcp/servers`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name, command: cmd, args, description: desc || '' }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    toast('success', 'MCP Server 已添加', name);
    document.getElementById('mcp-add-form').style.display = 'none';
    await loadMCPServers();
  } catch (e) { toast('error', '添加失败', e.message); }
});
document.getElementById('btn-refresh-mcp')?.addEventListener('click', loadMCP);
