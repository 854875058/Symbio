/* ============================================
   Symbio UI — 模型页 + LLM 配置
   由 web/app.js 拆分而来，index.html 按 00 → 99 的顺序加载；
   经典脚本共享同一个全局作用域，函数/常量可跨文件互相引用。
   ============================================ */

// ============ Models Page ============
async function loadModels() {
  showLoading(dom.modelsGrid, '加载模型...');
  try {
    const res = await fetch(`${API}/models`);
    const data = await res.json();
    state.models = data.models || [];
    loadChatModelOptions();
    renderModels();
  } catch (e) {
    toast('error', '加载模型失败', e.message);
    dom.modelsGrid.innerHTML = `
      <div class="empty-block is-error">
        <p class="empty-block-title">无法加载模型列表</p>
        <p class="empty-block-hint">${esc(e.message)}</p>
        <div class="empty-block-actions">
          <button class="btn-outline" type="button" onclick="loadModels()">重试</button>
        </div>
      </div>`;
  }
}

function selectedChatModel() {
  return dom.chatModelSelect?.value || state.selectedChatModel || '';
}

function loadChatModelOptions() {
  if (!dom.chatModelSelect) return;
  const configuredDefault = state.config?.model_medium || '';
  const selected = state.selectedChatModel || dom.chatModelSelect.value || configuredDefault;
  const options = [
    { value: '', label: configuredDefault ? `默认模型 (${configuredDefault})` : '默认模型' },
  ];
  const seen = new Set(['']);

  for (const model of state.models || []) {
    const value = model.model_id || model.id || '';
    if (!value || seen.has(value)) continue;
    seen.add(value);
    options.push({
      value,
      label: `${model.display_name || value}${model.enabled === false ? '（已停用）' : ''}`,
    });
  }

  if (selected && !seen.has(selected)) {
    options.push({ value: selected, label: selected });
  }

  dom.chatModelSelect.innerHTML = options.map(option => `
    <option value="${esc(option.value)}" ${option.value === selected ? 'selected' : ''}>${esc(option.label)}</option>
  `).join('');
  state.selectedChatModel = dom.chatModelSelect.value;
}

function renderModels() {
  if (state.models.length === 0) {
    dom.modelsGrid.innerHTML = `
      <div class="empty-block">
        <p class="empty-block-title">还没有添加任何模型</p>
        <p class="empty-block-hint">模型是 Symbio 的动力来源，没有模型就无法对话。添加多个之后，可以在下方「模型路由」里按任务难度分档：简单任务走便宜的小模型，复杂任务才用贵的大模型——这是控制成本的主要手段。</p>
        <div class="empty-block-actions">
          <button class="btn-primary" type="button" onclick="document.getElementById('btn-add-model')?.click()">添加模型</button>
        </div>
      </div>
    `;
    return;
  }

  dom.modelsGrid.innerHTML = state.models.map(m => `
    <div class="model-card" data-id="${m.id}">
      <div class="model-card-header">
        <div class="model-card-info">
          <div class="model-card-name">${esc(m.display_name || m.model_id)}</div>
          <div class="model-card-provider">${esc(m.provider)} / ${esc(m.model_id)}</div>
        </div>
        <div class="model-card-actions">
          <button class="btn-icon model-test-btn" data-id="${m.id}" title="测试连接" aria-label="测试 ${esc(m.name || m.id)} 的连接">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          </button>
          <button class="btn-icon btn-icon-danger model-delete-btn" data-id="${m.id}" title="删除" aria-label="删除模型 ${esc(m.name || m.id)}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
          </button>
        </div>
      </div>
      <div class="model-card-meta">
        <span class="badge ${m.enabled ? 'badge-green' : 'badge-gray'}">${m.enabled ? '启用' : '禁用'}</span>
        <span class="badge">${esc(m.base_url || '默认')}</span>
      </div>
      <div class="model-card-test-result" id="test-result-${m.id}"></div>
    </div>
  `).join('');

  // Attach event listeners
  dom.modelsGrid.querySelectorAll('.model-test-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      testModel(btn.dataset.id);
    });
  });

  dom.modelsGrid.querySelectorAll('.model-delete-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteModel(btn.dataset.id);
    });
  });
}

async function testModel(modelId) {
  const resultEl = document.getElementById(`test-result-${modelId}`);
  if (resultEl) {
    resultEl.innerHTML = '<div class="testing">测试中...</div>';
  }

  try {
    const res = await fetch(`${API}/models/${modelId}/test`, { method: 'POST' });
    const data = await res.json();
    if (resultEl) {
      resultEl.innerHTML = `<div class="test-result ${data.success ? 'test-ok' : 'test-fail'}">${esc(data.message)}</div>`;
    }
    if (data.success) {
      toast('success', '连接测试', data.message);
    } else {
      toast('error', '连接测试', data.message);
    }
  } catch (e) {
    if (resultEl) {
      resultEl.innerHTML = `<div class="test-result test-fail">请求失败: ${esc(e.message)}</div>`;
    }
    toast('error', '测试失败', e.message);
  }
}

async function deleteModel(modelId) {
  const model = state.models.find(m => m.id === modelId);
  const name = model?.display_name || model?.model_id || modelId;
  if (!confirm(`确定要删除模型 "${name}" 吗？`)) return;

  try {
    const res = await fetch(`${API}/models/${modelId}`, { method: 'DELETE' });
    if (res.ok) {
      toast('success', '已删除', `模型 ${name} 已删除`);
      loadModels();
    } else {
      const data = await res.json();
      toast('error', '删除失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '删除失败', e.message);
  }
}

// Add Model Modal
const btnAddModel = $('#btn-add-model');
btnAddModel?.addEventListener('click', showAddModelModal);

function showAddModelModal() {
  // Remove existing modal
  document.querySelector('.modal-overlay')?.remove();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h2>添加模型</h2>
        <button class="icon-btn modal-close-btn" title="关闭" aria-label="关闭对话框">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>提供商</label>
          <select id="modal-provider">
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI 兼容</option>
            <option value="ollama">Ollama 本地</option>
          </select>
        </div>
        <div class="form-group">
          <label>模型 ID</label>
          <input type="text" id="modal-model-id" placeholder="例: claude-sonnet-4-20250514">
        </div>
        <div class="form-group">
          <label>显示名称</label>
          <input type="text" id="modal-display-name" placeholder="例: Claude Sonnet 4">
        </div>
        <div class="form-group">
          <label>API Key</label>
          <input type="password" id="modal-api-key" placeholder="sk-...">
        </div>
        <div class="form-group">
          <label>Base URL</label>
          <input type="text" id="modal-base-url" placeholder="https://api.anthropic.com" value="https://api.anthropic.com">
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-outline modal-cancel-btn">取消</button>
        <button class="btn-primary modal-save-btn">保存</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  // Provider change updates base URL placeholder
  const providerEl = overlay.querySelector('#modal-provider');
  const baseUrlEl = overlay.querySelector('#modal-base-url');
  providerEl.addEventListener('change', () => {
    const urlMap = {
      anthropic: 'https://api.anthropic.com',
      openai: 'https://api.openai.com/v1',
      ollama: 'http://localhost:11434',
    };
    baseUrlEl.value = urlMap[providerEl.value] || '';
    baseUrlEl.placeholder = urlMap[providerEl.value] || '';
  });

  // Close
  overlay.querySelector('.modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.querySelector('.modal-cancel-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  // Save
  overlay.querySelector('.modal-save-btn').addEventListener('click', async () => {
    const modelId = overlay.querySelector('#modal-model-id').value.trim();
    const provider = providerEl.value;
    const displayName = overlay.querySelector('#modal-display-name').value.trim();
    const apiKey = overlay.querySelector('#modal-api-key').value.trim();
    const baseUrl = baseUrlEl.value.trim();

    if (!modelId) {
      toast('error', '验证失败', '模型 ID 不能为空');
      return;
    }

    try {
      const res = await fetch(`${API}/models`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model_id: modelId,
          provider: provider,
          display_name: displayName,
          api_key: apiKey,
          base_url: baseUrl,
        }),
      });

      if (res.ok) {
        overlay.remove();
        toast('success', '已添加', `模型 ${displayName || modelId} 已添加`);
        loadModels();
      } else {
        const data = await res.json();
        toast('error', '添加失败', data.detail || '未知错误');
      }
    } catch (e) {
      toast('error', '添加失败', e.message);
    }
  });
}

// ============ LLM Config ============
async function loadConfig() {
  try {
    const res = await fetch(`${API}/config`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.config = data;
    loadChatModelOptions();
    renderConfig();
  } catch (e) {
    console.warn('加载 LLM 配置失败:', e.message);
    toast('error', '加载配置失败', e.message);
    // 不能只弹 toast 就留下一片空白：用户会以为这一页本来就没内容，
    // 而此时点「保存」会把空表单写回后端。
    if (dom.configSection) {
      dom.configSection.innerHTML = `
        <div class="empty-block is-error">
          <p class="empty-block-title">配置读取失败</p>
          <p class="empty-block-hint">${esc(e.message)} · 服务可能未运行或鉴权失败。修复后点下方按钮重试；<strong>在加载成功之前不要保存</strong>，否则会覆盖现有配置。</p>
          <div class="empty-block-actions">
            <button class="btn-outline" type="button" onclick="loadConfig()">重新加载配置</button>
          </div>
        </div>`;
    }
  }
}

async function saveConfig() {
  // 表单未渲染时绝不能保存：所有 ?.value / ?.checked 都会回落成空值或 false，
  // 尤其 hitl.enabled 会被静默写成 false —— 把人类审批这道闸门关掉。
  if (!state.config || !document.getElementById('config-hitl-enabled')) {
    toast('error', '无法保存', '配置尚未加载成功，保存会清空现有设置。请先点右上角刷新重新加载配置。');
    return;
  }

  const anthropicKey = document.getElementById('config-anthropic-key')?.value?.trim() || '';
  const anthropicUrl = document.getElementById('config-anthropic-url')?.value?.trim() || '';
  const openaiKey = document.getElementById('config-openai-key')?.value?.trim() || '';
  const openaiUrl = document.getElementById('config-openai-url')?.value?.trim() || '';
  const modelLow = document.getElementById('config-model-low')?.value || '';
  const modelMedium = document.getElementById('config-model-medium')?.value || '';
  const modelHigh = document.getElementById('config-model-high')?.value || '';
  const hitlTargetsRaw = document.getElementById('config-hitl-targets')?.value?.trim() || '[]';
  let hitlTargets = [];

  try {
    hitlTargets = JSON.parse(hitlTargetsRaw || '[]');
    if (!Array.isArray(hitlTargets)) {
      toast('error', '审批配置错误', '通知目标必须是 JSON 数组');
      return;
    }
  } catch (e) {
    toast('error', '审批配置错误', `通知目标 JSON 无法解析：${e.message}`);
    return;
  }

  try {
    const body = {
      anthropic_base_url: anthropicUrl,
      openai_base_url: openaiUrl,
      hitl: {
        enabled: document.getElementById('config-hitl-enabled')?.checked || false,
        high_risk_auto_suspend: document.getElementById('config-hitl-high-risk')?.checked || false,
        approval_timeout: Number(document.getElementById('config-hitl-approval-timeout')?.value || 300),
        callback_base_url: document.getElementById('config-hitl-callback-base-url')?.value?.trim() || '',
        im_webhook_token: document.getElementById('config-hitl-im-token')?.value?.trim() || '',
        notify_timeout: Number(document.getElementById('config-hitl-notify-timeout')?.value || 5),
        notify_targets: hitlTargets,
      },
    };
    // Only send API keys if user entered new values (non-empty)
    if (anthropicKey) body.anthropic_api_key = anthropicKey;
    if (openaiKey) body.openai_api_key = openaiKey;

    // 一个模型都没配时，三个路由下拉框是 disabled 的空值。
    // 空值照样提交，会把后端现有的路由配置清成空串——
    // 表单"没得选"不等于用户"想清空"，所以只在真有值时才带上这三个字段。
    if (modelLow) body.model_low = modelLow;
    if (modelMedium) body.model_medium = modelMedium;
    if (modelHigh) body.model_high = modelHigh;

    const res = await fetch(`${API}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (res.ok) {
      const data = await res.json();
      if (data.success) {
        toast('success', '配置已保存', 'LLM 配置已更新');
        // Reload config to reflect saved state
        await loadConfig();
      } else {
        toast('error', '保存失败', '服务器返回异常');
      }
    } else {
      const data = await res.json();
      toast('error', '保存失败', data.detail || '未知错误');
    }
  } catch (e) {
    toast('error', '保存失败', e.message);
  }
}

function renderConfig() {
  if (!dom.configSection) return;
  const c = state.config;
  const h = c.hitl || {};
  const hitlTargetsJson = JSON.stringify(h.notify_targets || [], null, 2);

  // Build model options from state.models
  const modelOptions = state.models.map(m =>
    `<option value="${esc(m.model_id)}">${esc(m.display_name || m.model_id)}</option>`
  ).join('');

  // Helper to create a <select> with pre-selected value
  function tierSelect(id, selectedValue, label) {
    // 候选只来自真实配置的模型。此前这里硬编码了三个 Claude 型号做兜底，
    // 于是一个模型都没配的机器上，路由下拉框照样列着 Haiku/Sonnet/Opus，
    // 看起来像是"已经能用了"——选中并保存后，调用时才在别处报错。
    // 真相是：没有模型就没有可选项，这一点必须在这里就说出来。
    const seen = new Set();
    const allOptions = [];
    for (const m of state.models) {
      if (!seen.has(m.model_id)) {
        seen.add(m.model_id);
        allOptions.push({ value: m.model_id, label: m.display_name || m.model_id });
      }
    }

    // 已保存的值不在候选里时，必须显式补一项并选中。
    // 否则 select 静默落到第一项，界面显示的模型和后端存的不是一回事，
    // 下一次保存就把这个错误值写回去。
    const orphan = selectedValue && !seen.has(selectedValue);
    if (orphan) {
      allOptions.unshift({ value: selectedValue, label: `${selectedValue}（未在模型列表中）` });
    }

    if (!allOptions.length) {
      return `
        <div class="form-group">
          <label>${label}</label>
          <select id="${id}" disabled>
            <option value="">尚无可选模型</option>
          </select>
          <p class="form-hint form-hint-warn">还没有配置任何模型，这一档无从指派。请先在上方「模型」区添加模型（填好 API Key 后点「添加模型」），这里才会出现候选。</p>
        </div>
      `;
    }

    const optionsHtml = allOptions.map(opt =>
      `<option value="${esc(opt.value)}" ${opt.value === selectedValue ? 'selected' : ''}>${esc(opt.label)}</option>`
    ).join('');

    return `
      <div class="form-group">
        <label>${label}</label>
        <select id="${id}">${optionsHtml}</select>
        ${orphan ? `<p class="form-hint form-hint-warn">当前值 <code>${esc(selectedValue)}</code> 不在已配置的模型中，调用时会失败。请重新选择或先添加该模型。</p>` : ''}
      </div>
    `;
  }

  dom.configSection.innerHTML = `
    <!-- 只有一个保存按钮。此前两张卡各有一个按钮（「保存配置」/「保存审批配置」），
         但它们绑的是同一个 saveConfig()，一次 POST 写入两张卡的全部字段——
         按钮的措辞暗示了它不存在的独立范围：改了审批却点上面那个，
         也会把下面的一起写进去，反之亦然。范围既然是"全部"，就只留一个按钮说清楚。 -->
    <div class="config-save-bar">
      <div class="config-save-bar-text">
        <strong>下面两张卡是一份配置</strong>
        <span>保存会一次性提交 LLM 与审批的全部字段（API Key 留空表示保持原值不变）。</span>
      </div>
      <button class="btn-primary" id="btn-save-config" data-save-config>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        保存全部配置
      </button>
    </div>
    <div class="config-card">
      <div class="config-card-header">
        <h2>LLM 配置</h2>
      </div>
      <div class="config-card-body">
        <div class="config-row">
          <div class="config-group">
            <div class="config-section-title">Anthropic</div>
            <div class="form-group">
              <label>API Key ${c.has_anthropic_key ? '<span style="color:var(--green);font-size:var(--fs-xs)">✓ 已配置</span>' : '<span style="color:var(--red);font-size:var(--fs-xs)">✗ 未配置</span>'}</label>
              <input type="password" id="config-anthropic-key" value="" placeholder="${c.has_anthropic_key ? '留空保持不变，输入新值覆盖' : 'sk-ant-...'}">
            </div>
            <div class="form-group">
              <label>Base URL</label>
              <input type="text" id="config-anthropic-url" value="${esc(c.anthropic_base_url || 'https://api.anthropic.com')}">
            </div>
          </div>
          <div class="config-group">
            <div class="config-section-title">OpenAI 兼容</div>
            <div class="form-group">
              <label>API Key ${c.has_openai_key ? '<span style="color:var(--green);font-size:var(--fs-xs)">✓ 已配置</span>' : '<span style="color:var(--red);font-size:var(--fs-xs)">✗ 未配置</span>'}</label>
              <input type="password" id="config-openai-key" value="" placeholder="${c.has_openai_key ? '留空保持不变，输入新值覆盖' : 'sk-...'}">
            </div>
            <div class="form-group">
              <label>Base URL</label>
              <input type="text" id="config-openai-url" value="${esc(c.openai_base_url || 'https://api.openai.com/v1')}">
            </div>
          </div>
        </div>
        <div class="config-section-title">模型路由</div>
        <div class="config-tier-row">
          ${tierSelect('config-model-low', c.model_low, '简单任务 (low)')}
          ${tierSelect('config-model-medium', c.model_medium, '中等任务 (medium)')}
          ${tierSelect('config-model-high', c.model_high, '复杂任务 (high)')}
        </div>
      </div>
    </div>
    <div class="config-card">
      <div class="config-card-header">
        <h2>外部审批配置</h2>
      </div>
      <div class="config-card-body">
        <div class="config-row">
          <div class="config-group">
            <div class="config-section-title">审批策略</div>
            <label class="config-switch-row">
              <input type="checkbox" id="config-hitl-enabled" ${h.enabled !== false ? 'checked' : ''}>
              <span>启用人类审批</span>
            </label>
            <label class="config-switch-row">
              <input type="checkbox" id="config-hitl-high-risk" ${h.high_risk_auto_suspend !== false ? 'checked' : ''}>
              <span>高风险任务自动暂停等待审批</span>
            </label>
            <div class="form-group">
              <label>审批超时（秒）</label>
              <input type="number" id="config-hitl-approval-timeout" min="30" value="${esc(String(h.approval_timeout || 300))}">
            </div>
          </div>
          <div class="config-group">
            <div class="config-section-title">回调与安全</div>
            <div class="form-group">
              <label>公网回调地址</label>
              <input type="text" id="config-hitl-callback-base-url" value="${esc(h.callback_base_url || '')}" placeholder="https://symbio.example.com">
              <div class="config-help">飞书、企业微信卡片按钮会调用这个地址下的 /api/hitl/action。</div>
            </div>
            <div class="form-group">
              <label>IM 回调共享 Token</label>
              <input type="password" id="config-hitl-im-token" value="${esc(h.im_webhook_token || '')}" placeholder="用于 QQ、微信桥接回调校验">
            </div>
            <div class="form-group">
              <label>通知超时（秒）</label>
              <input type="number" id="config-hitl-notify-timeout" min="1" step="0.5" value="${esc(String(h.notify_timeout || 5))}">
            </div>
          </div>
        </div>
        <div class="config-section-title">通知目标</div>
        <div class="form-group">
          <label>目标 JSON</label>
          <textarea id="config-hitl-targets" class="config-targets-textarea" spellcheck="false" placeholder='[{"platform":"feishu","endpoint":"https://...","chat_id":"ops","enabled":true}]'>${esc(hitlTargetsJson)}</textarea>
          <div class="config-help">支持 platform: feishu/lark、wechat/wecom、qq/onebot、wechaty。配置 callback_base_url 后，飞书和企业微信会收到同意/拒绝按钮卡片。</div>
        </div>
      </div>
    </div>
  `;

  // Attach save handler
  document.querySelectorAll('[data-save-config]').forEach(btn => btn.addEventListener('click', saveConfig));
}
