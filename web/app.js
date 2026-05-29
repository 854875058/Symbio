/* ============================================
   Symbio Web UI - JavaScript Application
   ============================================ */

const API_BASE = 'http://localhost:9090/api';

// ============================================
// State Management
// ============================================
const state = {
  currentPage: 'chat',
  sessions: [],
  currentSessionId: null,
  messages: [],
  tasks: [],
  models: [],
  memories: [],
  currentModel: null,
  currentMemory: null,
  isConnected: false,
  totalTokens: 0,
};

// ============================================
// DOM Elements
// ============================================
const elements = {
  // Navigation
  navTabs: document.querySelectorAll('.nav-tab'),
  pages: document.querySelectorAll('.page'),

  // Chat
  sessionList: document.getElementById('session-list'),
  chatTitle: document.getElementById('chat-title'),
  messagesArea: document.getElementById('messages-area'),
  messageInput: document.getElementById('message-input'),
  btnSend: document.getElementById('btn-send'),
  btnNewSession: document.getElementById('btn-new-session'),
  btnClearChat: document.getElementById('btn-clear-chat'),
  contextPanel: document.getElementById('context-panel'),
  btnClosePanel: document.getElementById('btn-close-panel'),
  memoryResults: document.getElementById('memory-results'),
  toolCalls: document.getElementById('tool-calls'),

  // Tasks
  tasksGrid: document.getElementById('tasks-grid'),
  taskFilter: document.getElementById('task-filter'),
  btnRefreshTasks: document.getElementById('btn-refresh-tasks'),

  // Models
  modelsList: document.getElementById('models-list'),
  modelDetail: document.getElementById('model-detail'),
  btnAddModel: document.getElementById('btn-add-model'),
  modalAddModel: document.getElementById('modal-add-model'),
  btnCloseModal: document.getElementById('btn-close-modal'),
  btnCancelModal: document.getElementById('btn-cancel-modal'),
  btnSaveModel: document.getElementById('btn-save-model'),

  // Model Form
  modelProvider: document.getElementById('model-provider'),
  modelName: document.getElementById('model-name'),
  modelApiKey: document.getElementById('model-api-key'),
  modelBaseUrl: document.getElementById('model-base-url'),
  modelMaxTokens: document.getElementById('model-max-tokens'),

  // Memory
  memoryList: document.getElementById('memory-list'),
  memoryDetail: document.getElementById('memory-detail'),
  memorySearch: document.getElementById('memory-search'),
  memoryTypeFilter: document.getElementById('memory-type-filter'),
  btnSearchMemory: document.getElementById('btn-search-memory'),

  // Status
  connectionStatus: document.getElementById('connection-status'),
  currentModelStatus: document.getElementById('current-model'),
  tokenUsage: document.getElementById('token-usage'),
  systemStatus: document.getElementById('system-status'),

  // Toast
  toastContainer: document.getElementById('toast-container'),
};

// ============================================
// API Helper
// ============================================
async function apiRequest(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`;
  const defaultOptions = {
    headers: {
      'Content-Type': 'application/json',
    },
  };

  try {
    const response = await fetch(url, { ...defaultOptions, ...options });
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || data.message || `HTTP ${response.status}`);
    }

    return data;
  } catch (error) {
    if (error.name === 'TypeError' && error.message.includes('fetch')) {
      showToast('error', 'Connection Error', 'Cannot connect to the server. Please check if the backend is running.');
      updateConnectionStatus(false);
    }
    throw error;
  }
}

// ============================================
// Toast Notifications
// ============================================
function showToast(type, title, message, duration = 3000) {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;

  const icons = {
    success: '&#10003;',
    error: '&#10007;',
    warning: '&#9888;',
    info: '&#8505;',
  };

  toast.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <div class="toast-content">
      <div class="toast-title">${title}</div>
      ${message ? `<div class="toast-message">${message}</div>` : ''}
    </div>
    <button class="toast-close">&times;</button>
  `;

  elements.toastContainer.appendChild(toast);

  const closeBtn = toast.querySelector('.toast-close');
  closeBtn.addEventListener('click', () => removeToast(toast));

  if (duration > 0) {
    setTimeout(() => removeToast(toast), duration);
  }

  return toast;
}

function removeToast(toast) {
  toast.style.opacity = '0';
  toast.style.transform = 'translateX(100%)';
  toast.style.transition = 'all 200ms ease';
  setTimeout(() => toast.remove(), 200);
}

// ============================================
// Connection Status
// ============================================
function updateConnectionStatus(connected) {
  state.isConnected = connected;
  const dot = elements.connectionStatus.querySelector('.status-dot');
  const text = elements.connectionStatus.querySelector('span:last-child');

  dot.className = `status-dot ${connected ? 'connected' : 'disconnected'}`;
  text.textContent = connected ? 'Connected' : 'Disconnected';
}

function updateTokenUsage(tokens) {
  state.totalTokens += tokens;
  elements.tokenUsage.querySelector('span').textContent = `Tokens: ${formatNumber(state.totalTokens)}`;
}

function updateCurrentModel(model) {
  elements.currentModelStatus.querySelector('span').textContent = `Model: ${model || '--'}`;
}

function updateSystemStatus(status) {
  elements.systemStatus.querySelector('span').textContent = status;
}

// ============================================
// Navigation
// ============================================
function switchPage(pageName) {
  state.currentPage = pageName;

  elements.navTabs.forEach(tab => {
    tab.classList.toggle('active', tab.dataset.page === pageName);
  });

  elements.pages.forEach(page => {
    page.classList.toggle('active', page.id === `page-${pageName}`);
  });

  // Load data for the page
  switch (pageName) {
    case 'tasks':
      loadTasks();
      break;
    case 'models':
      loadModels();
      break;
    case 'memory':
      loadMemories();
      break;
  }
}

// ============================================
// Chat Functions
// ============================================
async function loadSessions() {
  try {
    const data = await apiRequest('/sessions');
    state.sessions = data.sessions || [];
    renderSessions();
  } catch (error) {
    console.error('Failed to load sessions:', error);
  }
}

function renderSessions() {
  if (state.sessions.length === 0) {
    elements.sessionList.innerHTML = `
      <div class="empty-state" style="padding: var(--space-4);">
        <p>No sessions yet</p>
      </div>
    `;
    return;
  }

  elements.sessionList.innerHTML = state.sessions.map(session => `
    <div class="session-item ${session.id === state.currentSessionId ? 'active' : ''}"
         data-session-id="${session.id}">
      <div class="session-title">${escapeHtml(session.title || 'Untitled Session')}</div>
      <div class="session-time">${formatTime(session.created_at)}</div>
    </div>
  `).join('');

  // Add click handlers
  elements.sessionList.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', () => {
      const sessionId = item.dataset.sessionId;
      selectSession(sessionId);
    });
  });
}

async function selectSession(sessionId) {
  state.currentSessionId = sessionId;
  const session = state.sessions.find(s => s.id === sessionId);

  if (session) {
    elements.chatTitle.textContent = session.title || 'Untitled Session';
  }

  renderSessions();
  await loadMessages(sessionId);
}

async function createNewSession() {
  try {
    const data = await apiRequest('/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: 'New Session' }),
    });

    const newSession = {
      id: data.session_id,
      title: 'New Session',
      created_at: new Date().toISOString(),
    };

    state.sessions.unshift(newSession);
    selectSession(newSession.id);
    showToast('success', 'Session Created', 'New session started successfully.');
  } catch (error) {
    showToast('error', 'Error', 'Failed to create new session.');
  }
}

async function loadMessages(sessionId) {
  try {
    const data = await apiRequest(`/sessions/${sessionId}/messages`);
    state.messages = data.messages || [];
    renderMessages();
  } catch (error) {
    console.error('Failed to load messages:', error);
    state.messages = [];
    renderMessages();
  }
}

function renderMessages() {
  if (state.messages.length === 0) {
    elements.messagesArea.innerHTML = `
      <div class="welcome-message">
        <div class="welcome-icon">
          <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
            <circle cx="24" cy="24" r="20" stroke="#3b82f6" stroke-width="2" stroke-dasharray="4 4"/>
            <circle cx="24" cy="24" r="8" fill="#3b82f6" opacity="0.3"/>
            <circle cx="24" cy="24" r="4" fill="#3b82f6"/>
          </svg>
        </div>
        <h3>Welcome to Symbio</h3>
        <p>Start a conversation with your AI assistant. Ask anything or try one of the suggestions below.</p>
        <div class="suggestions">
          <button class="suggestion-btn" data-prompt="Help me write a Python script">Write a Python script</button>
          <button class="suggestion-btn" data-prompt="Explain quantum computing">Explain quantum computing</button>
          <button class="suggestion-btn" data-prompt="Review my code for bugs">Review my code</button>
        </div>
      </div>
    `;

    // Add suggestion click handlers
    elements.messagesArea.querySelectorAll('.suggestion-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        elements.messageInput.value = btn.dataset.prompt;
        elements.messageInput.focus();
        autoResizeTextarea();
      });
    });

    return;
  }

  elements.messagesArea.innerHTML = state.messages.map(msg => `
    <div class="message ${msg.role}" data-message-id="${msg.id}">
      <div class="message-avatar">
        ${msg.role === 'user' ? 'U' : 'AI'}
      </div>
      <div class="message-content">
        <div class="message-bubble">${formatMessageContent(msg.content)}</div>
        <div class="message-meta">
          <span>${formatTime(msg.timestamp)}</span>
          ${msg.token_usage ? `<span>${msg.token_usage} tokens</span>` : ''}
        </div>
      </div>
    </div>
  `).join('');

  // Scroll to bottom
  scrollToBottom();
}

function formatMessageContent(content) {
  if (!content) return '';

  // Escape HTML
  let html = escapeHtml(content);

  // Code blocks
  html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // Italic
  html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

  // Line breaks
  html = html.replace(/\n/g, '<br>');

  return html;
}

async function sendMessage() {
  const content = elements.messageInput.value.trim();
  if (!content) return;

  if (!state.currentSessionId) {
    await createNewSession();
  }

  // Add user message to UI immediately
  const userMessage = {
    id: `temp-${Date.now()}`,
    role: 'user',
    content: content,
    timestamp: new Date().toISOString(),
  };

  state.messages.push(userMessage);
  renderMessages();

  // Clear input
  elements.messageInput.value = '';
  autoResizeTextarea();

  // Show typing indicator
  showTypingIndicator();

  try {
    // Send to API
    const data = await apiRequest(`/sessions/${state.currentSessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    });

    // Update token usage
    if (data.token_usage) {
      updateTokenUsage(data.token_usage);
    }

    // Simulate assistant response (in real app, this would come from SSE/WebSocket)
    await simulateAssistantResponse(content);
  } catch (error) {
    removeTypingIndicator();
    showToast('error', 'Error', 'Failed to send message. Please try again.');
  }
}

async function simulateAssistantResponse(userMessage) {
  // Simulate delay
  await new Promise(resolve => setTimeout(resolve, 1000 + Math.random() * 2000));

  removeTypingIndicator();

  // Generate a mock response
  const responses = [
    `I understand you're asking about "${userMessage.substring(0, 30)}...". Let me help you with that.`,
    `That's an interesting question! Here's what I think about "${userMessage.substring(0, 20)}..."`,
    `I'll help you with that. Let me process your request.`,
  ];

  const assistantMessage = {
    id: `msg-${Date.now()}`,
    role: 'assistant',
    content: responses[Math.floor(Math.random() * responses.length)] +
      '\n\nThis is a simulated response. In production, this would come from the AI model via the backend API.',
    timestamp: new Date().toISOString(),
    token_usage: Math.floor(50 + Math.random() * 200),
  };

  state.messages.push(assistantMessage);
  renderMessages();

  // Update token usage
  if (assistantMessage.token_usage) {
    updateTokenUsage(assistantMessage.token_usage);
  }
}

function showTypingIndicator() {
  const indicator = document.createElement('div');
  indicator.className = 'message assistant';
  indicator.id = 'typing-indicator';
  indicator.innerHTML = `
    <div class="message-avatar">AI</div>
    <div class="message-content">
      <div class="message-bubble">
        <div class="typing-indicator">
          <span></span>
          <span></span>
          <span></span>
        </div>
      </div>
    </div>
  `;
  elements.messagesArea.appendChild(indicator);
  scrollToBottom();
}

function removeTypingIndicator() {
  const indicator = document.getElementById('typing-indicator');
  if (indicator) {
    indicator.remove();
  }
}

function clearChat() {
  if (!state.currentSessionId) return;

  state.messages = [];
  renderMessages();
  showToast('info', 'Chat Cleared', 'All messages have been cleared.');
}

function scrollToBottom() {
  elements.messagesArea.scrollTop = elements.messagesArea.scrollHeight;
}

function autoResizeTextarea() {
  const textarea = elements.messageInput;
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
}

// ============================================
// Tasks Functions
// ============================================
async function loadTasks() {
  const filter = elements.taskFilter.value;

  try {
    updateSystemStatus('Loading tasks...');
    const data = await apiRequest(`/tasks?status=${filter}`);
    state.tasks = data.tasks || [];
    renderTasks();
    updateSystemStatus('Ready');
  } catch (error) {
    console.error('Failed to load tasks:', error);
    renderTasksEmpty();
    updateSystemStatus('Error loading tasks');
  }
}

function renderTasks() {
  if (state.tasks.length === 0) {
    renderTasksEmpty();
    return;
  }

  elements.tasksGrid.innerHTML = state.tasks.map(task => `
    <div class="task-card" data-task-id="${task.id}">
      <div class="task-card-header">
        <span class="task-id">${task.id}</span>
        <span class="task-status ${task.status}">${formatStatus(task.status)}</span>
      </div>
      <div class="task-card-body">
        <h4>${escapeHtml(task.title || task.description || 'Untitled Task')}</h4>
        <div class="task-card-meta">
          <span>Model: ${task.model || '--'}</span>
          <span>Tokens: ${formatNumber(task.token_usage || 0)}</span>
          <span>Duration: ${formatDuration(task.duration)}</span>
        </div>
      </div>
      <div class="task-card-footer">
        <span class="task-time">${formatTime(task.created_at)}</span>
        <div class="task-card-actions">
          ${task.status === 'running' ? '<button onclick="pauseTask(\'' + task.id + '\')">Pause</button>' : ''}
          ${task.status === 'failed' ? '<button onclick="retryTask(\'' + task.id + '\')">Retry</button>' : ''}
          <button onclick="viewTaskDetails('${task.id}')">Details</button>
        </div>
      </div>
    </div>
  `).join('');
}

function renderTasksEmpty() {
  elements.tasksGrid.innerHTML = `
    <div class="empty-state" style="grid-column: 1 / -1;">
      <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
        <rect x="8" y="12" width="32" height="24" rx="2" stroke="#525252" stroke-width="2"/>
        <path d="M16 20h16M16 26h10" stroke="#525252" stroke-width="2"/>
      </svg>
      <p>No tasks found. Tasks will appear here when you start conversations.</p>
    </div>
  `;
}

async function pauseTask(taskId) {
  try {
    await apiRequest(`/tasks/${taskId}/pause`, { method: 'POST' });
    showToast('success', 'Task Paused', `Task ${taskId} has been paused.`);
    loadTasks();
  } catch (error) {
    showToast('error', 'Error', 'Failed to pause task.');
  }
}

async function retryTask(taskId) {
  try {
    await apiRequest(`/tasks/${taskId}/resume`, { method: 'POST' });
    showToast('success', 'Task Retrying', `Task ${taskId} has been resumed.`);
    loadTasks();
  } catch (error) {
    showToast('error', 'Error', 'Failed to retry task.');
  }
}

function viewTaskDetails(taskId) {
  const task = state.tasks.find(t => t.id === taskId);
  if (task) {
    showToast('info', 'Task Details', `
      ID: ${task.id}
      Status: ${task.status}
      Model: ${task.model || 'N/A'}
      Tokens: ${formatNumber(task.token_usage || 0)}
    `, 5000);
  }
}

// ============================================
// Models Functions
// ============================================
async function loadModels() {
  try {
    updateSystemStatus('Loading models...');
    const data = await apiRequest('/models');
    state.models = data.models || [];
    renderModels();
    updateSystemStatus('Ready');
  } catch (error) {
    console.error('Failed to load models:', error);
    renderModelsEmpty();
    updateSystemStatus('Error loading models');
  }
}

function renderModels() {
  if (state.models.length === 0) {
    renderModelsEmpty();
    return;
  }

  elements.modelsList.innerHTML = state.models.map(model => `
    <div class="model-card ${state.currentModel?.id === model.id ? 'selected' : ''}"
         data-model-id="${model.id}">
      <div class="model-card-header">
        <h4>${escapeHtml(model.name)}</h4>
        <span class="provider-badge">${model.provider}</span>
      </div>
      <div class="model-card-meta">
        Max Tokens: ${formatNumber(model.max_tokens || 4096)}
      </div>
      <div class="model-card-status">
        <span class="status-indicator ${model.enabled ? 'active' : 'inactive'}"></span>
        <span>${model.enabled ? 'Enabled' : 'Disabled'}</span>
      </div>
    </div>
  `).join('');

  // Add click handlers
  elements.modelsList.querySelectorAll('.model-card').forEach(card => {
    card.addEventListener('click', () => {
      const modelId = card.dataset.modelId;
      selectModel(modelId);
    });
  });
}

function renderModelsEmpty() {
  elements.modelsList.innerHTML = `
    <div class="empty-state">
      <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
        <circle cx="24" cy="24" r="16" stroke="#525252" stroke-width="2"/>
        <circle cx="24" cy="24" r="6" fill="#525252"/>
      </svg>
      <p>No models configured yet. Add your first model to get started.</p>
    </div>
  `;
}

function selectModel(modelId) {
  state.currentModel = state.models.find(m => m.id === modelId);
  renderModels();
  renderModelDetail();
}

function renderModelDetail() {
  if (!state.currentModel) {
    elements.modelDetail.innerHTML = `
      <div class="empty-state">
        <p>Select a model to view details</p>
      </div>
    `;
    return;
  }

  const model = state.currentModel;

  elements.modelDetail.innerHTML = `
    <div class="model-detail-content">
      <h3>${escapeHtml(model.name)}</h3>
      <div class="model-info-grid">
        <div class="model-info-item">
          <label>Provider</label>
          <span>${model.provider}</span>
        </div>
        <div class="model-info-item">
          <label>Status</label>
          <span>${model.enabled ? 'Enabled' : 'Disabled'}</span>
        </div>
        <div class="model-info-item">
          <label>Max Tokens</label>
          <span>${formatNumber(model.max_tokens || 4096)}</span>
        </div>
        <div class="model-info-item">
          <label>Base URL</label>
          <span>${model.base_url || 'Default'}</span>
        </div>
        <div class="model-info-item">
          <label>Input Cost</label>
          <span>${model.input_cost || '$0'}/1M tokens</span>
        </div>
        <div class="model-info-item">
          <label>Output Cost</label>
          <span>${model.output_cost || '$0'}/1M tokens</span>
        </div>
      </div>
      <div class="model-actions">
        <button class="btn-primary" onclick="testModelConnection('${model.id}')">
          Test Connection
        </button>
        <button class="btn-secondary" onclick="toggleModel('${model.id}')">
          ${model.enabled ? 'Disable' : 'Enable'}
        </button>
        <button class="btn-secondary" onclick="deleteModel('${model.id}')" style="color: var(--accent-error);">
          Delete
        </button>
      </div>
    </div>
  `;
}

async function testModelConnection(modelId) {
  try {
    updateSystemStatus('Testing connection...');
    const data = await apiRequest(`/models/${modelId}/test`, { method: 'POST' });

    if (data.success) {
      showToast('success', 'Connection Successful', `Latency: ${data.latency_ms}ms`);
    } else {
      showToast('error', 'Connection Failed', 'Could not connect to the model API.');
    }
    updateSystemStatus('Ready');
  } catch (error) {
    showToast('error', 'Connection Error', error.message);
    updateSystemStatus('Ready');
  }
}

async function toggleModel(modelId) {
  const model = state.models.find(m => m.id === modelId);
  if (!model) return;

  try {
    await apiRequest(`/models/${modelId}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: !model.enabled }),
    });

    model.enabled = !model.enabled;
    renderModels();
    renderModelDetail();
    showToast('success', 'Model Updated', `${model.name} has been ${model.enabled ? 'enabled' : 'disabled'}.`);
  } catch (error) {
    showToast('error', 'Error', 'Failed to update model.');
  }
}

async function deleteModel(modelId) {
  if (!confirm('Are you sure you want to delete this model?')) return;

  try {
    await apiRequest(`/models/${modelId}`, { method: 'DELETE' });

    state.models = state.models.filter(m => m.id !== modelId);
    if (state.currentModel?.id === modelId) {
      state.currentModel = null;
    }
    renderModels();
    renderModelDetail();
    showToast('success', 'Model Deleted', 'The model has been removed.');
  } catch (error) {
    showToast('error', 'Error', 'Failed to delete model.');
  }
}

async function saveModel() {
  const modelData = {
    provider: elements.modelProvider.value,
    name: elements.modelName.value,
    api_key: elements.modelApiKey.value,
    base_url: elements.modelBaseUrl.value,
    max_tokens: parseInt(elements.modelMaxTokens.value) || 4096,
  };

  if (!modelData.name) {
    showToast('warning', 'Validation Error', 'Please enter a model name.');
    return;
  }

  try {
    const data = await apiRequest('/models', {
      method: 'POST',
      body: JSON.stringify(modelData),
    });

    const newModel = {
      id: data.model_id,
      ...modelData,
      enabled: true,
    };

    state.models.push(newModel);
    renderModels();
    closeModal();
    showToast('success', 'Model Added', `${modelData.name} has been added successfully.`);

    // Clear form
    elements.modelName.value = '';
    elements.modelApiKey.value = '';
    elements.modelBaseUrl.value = '';
    elements.modelMaxTokens.value = '4096';
  } catch (error) {
    showToast('error', 'Error', 'Failed to add model.');
  }
}

// ============================================
// Memory Functions
// ============================================
async function loadMemories() {
  const typeFilter = elements.memoryTypeFilter.value;

  try {
    updateSystemStatus('Loading memories...');
    const data = await apiRequest(`/memory?type=${typeFilter}`);
    state.memories = data.memories || [];
    renderMemories();
    updateSystemStatus('Ready');
  } catch (error) {
    console.error('Failed to load memories:', error);
    renderMemoriesEmpty();
    updateSystemStatus('Error loading memories');
  }
}

async function searchMemories() {
  const query = elements.memorySearch.value.trim();
  if (!query) {
    loadMemories();
    return;
  }

  try {
    updateSystemStatus('Searching memories...');
    const data = await apiRequest(`/memory/search?query=${encodeURIComponent(query)}`);
    state.memories = data.memories || [];
    renderMemories();
    updateSystemStatus('Ready');
  } catch (error) {
    console.error('Failed to search memories:', error);
    showToast('error', 'Search Error', 'Failed to search memories.');
    updateSystemStatus('Ready');
  }
}

function renderMemories() {
  if (state.memories.length === 0) {
    renderMemoriesEmpty();
    return;
  }

  elements.memoryList.innerHTML = state.memories.map(memory => `
    <div class="memory-item ${state.currentMemory?.id === memory.id ? 'selected' : ''}"
         data-memory-id="${memory.id}">
      <div class="memory-item-header">
        <h4>${escapeHtml(memory.title || memory.content?.substring(0, 30) || 'Untitled')}</h4>
        <span class="memory-type-badge ${memory.type}">${formatMemoryType(memory.type)}</span>
      </div>
      <div class="memory-item-content">${escapeHtml(memory.content || '')}</div>
      <div class="memory-item-meta">
        <span>Importance: ${(memory.importance || 0).toFixed(1)}</span>
        <span>Accessed: ${memory.access_count || 0} times</span>
      </div>
    </div>
  `).join('');

  // Add click handlers
  elements.memoryList.querySelectorAll('.memory-item').forEach(item => {
    item.addEventListener('click', () => {
      const memoryId = item.dataset.memoryId;
      selectMemory(memoryId);
    });
  });
}

function renderMemoriesEmpty() {
  elements.memoryList.innerHTML = `
    <div class="empty-state">
      <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
        <circle cx="24" cy="24" r="16" stroke="#525252" stroke-width="2" stroke-dasharray="4 4"/>
        <circle cx="24" cy="24" r="4" fill="#525252"/>
      </svg>
      <p>No memories found. Search or create memories to get started.</p>
    </div>
  `;
}

function selectMemory(memoryId) {
  state.currentMemory = state.memories.find(m => m.id === memoryId);
  renderMemories();
  renderMemoryDetail();
}

function renderMemoryDetail() {
  if (!state.currentMemory) {
    elements.memoryDetail.innerHTML = `
      <div class="empty-state">
        <p>Select a memory to view details</p>
      </div>
    `;
    return;
  }

  const memory = state.currentMemory;

  elements.memoryDetail.innerHTML = `
    <div class="memory-detail-content">
      <h3>${escapeHtml(memory.title || 'Memory Details')}</h3>
      <div class="memory-detail-meta">
        <div class="memory-meta-item">
          <label>Type</label>
          <span>${formatMemoryType(memory.type)}</span>
        </div>
        <div class="memory-meta-item">
          <label>Importance</label>
          <span>${(memory.importance || 0).toFixed(2)}</span>
        </div>
        <div class="memory-meta-item">
          <label>Access Count</label>
          <span>${memory.access_count || 0}</span>
        </div>
        <div class="memory-meta-item">
          <label>Created</label>
          <span>${formatDate(memory.created_at)}</span>
        </div>
        <div class="memory-meta-item">
          <label>Last Accessed</label>
          <span>${formatDate(memory.last_accessed)}</span>
        </div>
        <div class="memory-meta-item">
          <label>Memory ID</label>
          <span style="font-family: var(--font-mono); font-size: var(--text-xs);">${memory.id}</span>
        </div>
      </div>
      <div class="memory-detail-body">
        <h4>Content</h4>
        <p>${escapeHtml(memory.content || 'No content available')}</p>
      </div>
      ${memory.entities && memory.entities.length > 0 ? `
        <div class="memory-entities">
          <h4>Associated Entities</h4>
          <div class="entity-tags">
            ${memory.entities.map(entity => `
              <span class="entity-tag">
                ${escapeHtml(entity.name)}
                <span class="entity-type">(${entity.type})</span>
              </span>
            `).join('')}
          </div>
        </div>
      ` : ''}
      <div class="memory-actions">
        <button class="btn-primary" onclick="editMemory('${memory.id}')">Edit</button>
        <button class="btn-secondary" onclick="deleteMemory('${memory.id}')" style="color: var(--accent-error);">
          Delete
        </button>
      </div>
    </div>
  `;
}

async function editMemory(memoryId) {
  showToast('info', 'Edit Memory', 'Memory editing will be available in a future update.');
}

async function deleteMemory(memoryId) {
  if (!confirm('Are you sure you want to delete this memory?')) return;

  try {
    await apiRequest(`/memory/${memoryId}`, { method: 'DELETE' });

    state.memories = state.memories.filter(m => m.id !== memoryId);
    if (state.currentMemory?.id === memoryId) {
      state.currentMemory = null;
    }
    renderMemories();
    renderMemoryDetail();
    showToast('success', 'Memory Deleted', 'The memory has been removed.');
  } catch (error) {
    showToast('error', 'Error', 'Failed to delete memory.');
  }
}

// ============================================
// Modal Functions
// ============================================
function openModal() {
  elements.modalAddModel.classList.add('active');
}

function closeModal() {
  elements.modalAddModel.classList.remove('active');
}

// ============================================
// Utility Functions
// ============================================
function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatNumber(num) {
  if (num >= 1000000) {
    return (num / 1000000).toFixed(1) + 'M';
  }
  if (num >= 1000) {
    return (num / 1000).toFixed(1) + 'K';
  }
  return num.toString();
}

function formatTime(timestamp) {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  const now = new Date();
  const diff = now - date;

  if (diff < 60000) return 'Just now';
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;

  return date.toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
  });
}

function formatDate(timestamp) {
  if (!timestamp) return 'N/A';
  return new Date(timestamp).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDuration(seconds) {
  if (!seconds) return 'N/A';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function formatStatus(status) {
  const statusMap = {
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
    pending: 'Pending',
    cancelled: 'Cancelled',
  };
  return statusMap[status] || status;
}

function formatMemoryType(type) {
  const typeMap = {
    long_term: 'Long Term',
    short_term: 'Short Term',
    episodic: 'Episodic',
    semantic: 'Semantic',
  };
  return typeMap[type] || type;
}

// ============================================
// Health Check
// ============================================
async function checkHealth() {
  try {
    await apiRequest('/health');
    updateConnectionStatus(true);
  } catch (error) {
    updateConnectionStatus(false);
  }
}

// ============================================
// Event Listeners
// ============================================
function initEventListeners() {
  // Navigation
  elements.navTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      switchPage(tab.dataset.page);
    });
  });

  // Chat - New Session
  elements.btnNewSession.addEventListener('click', createNewSession);

  // Chat - Clear
  elements.btnClearChat.addEventListener('click', clearChat);

  // Chat - Send Message
  elements.btnSend.addEventListener('click', sendMessage);

  // Chat - Input handling
  elements.messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      sendMessage();
    }
  });

  elements.messageInput.addEventListener('input', autoResizeTextarea);

  // Context Panel
  elements.btnClosePanel.addEventListener('click', () => {
    elements.contextPanel.classList.toggle('hidden');
  });

  // Tasks - Filter
  elements.taskFilter.addEventListener('change', loadTasks);

  // Tasks - Refresh
  elements.btnRefreshTasks.addEventListener('click', loadTasks);

  // Models - Add
  elements.btnAddModel.addEventListener('click', openModal);

  // Models - Modal
  elements.btnCloseModal.addEventListener('click', closeModal);
  elements.btnCancelModal.addEventListener('click', closeModal);
  elements.btnSaveModel.addEventListener('click', saveModel);

  // Close modal on overlay click
  elements.modalAddModel.addEventListener('click', (e) => {
    if (e.target === elements.modalAddModel) {
      closeModal();
    }
  });

  // Memory - Search
  elements.btnSearchMemory.addEventListener('click', searchMemories);
  elements.memorySearch.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      searchMemories();
    }
  });

  // Memory - Type Filter
  elements.memoryTypeFilter.addEventListener('change', loadMemories);

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    // Ctrl+K - Focus search
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      if (state.currentPage === 'memory') {
        elements.memorySearch.focus();
      }
    }

    // Ctrl+N - New session
    if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
      e.preventDefault();
      if (state.currentPage === 'chat') {
        createNewSession();
      }
    }

    // Escape - Close modal
    if (e.key === 'Escape') {
      closeModal();
    }

    // Ctrl+1-4 - Switch pages
    if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '4') {
      e.preventDefault();
      const pages = ['chat', 'tasks', 'models', 'memory'];
      const index = parseInt(e.key) - 1;
      if (pages[index]) {
        switchPage(pages[index]);
      }
    }
  });
}

// ============================================
// Initialization
// ============================================
async function init() {
  console.log('Initializing Symbio Web UI...');

  // Set up event listeners
  initEventListeners();

  // Check backend health
  await checkHealth();

  // Load initial data
  if (state.isConnected) {
    await loadSessions();
  }

  // Set up periodic health check
  setInterval(checkHealth, 30000);

  console.log('Symbio Web UI initialized.');
}

// Start the application
document.addEventListener('DOMContentLoaded', init);
