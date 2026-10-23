/**
 * REST API client for WhatsBot-Lite backend.
 */

const BASE = '';

function _getToken() {
  return localStorage.getItem('whatsbot_token') || '';
}

function _authHeaders(headers = {}) {
  const token = _getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

export function authHeaders(extra = {}) {
  return _authHeaders({ ...extra });
}

export function handleUnauthorized() {
  localStorage.removeItem('whatsbot_token');
  window.dispatchEvent(new Event('whatsbot:unauthorized'));
}

async function request(method, path, body, signal) {
  const opts = {
    method,
    headers: _authHeaders({ 'Content-Type': 'application/json' }),
  };
  if (body) opts.body = JSON.stringify(body);
  if (signal) opts.signal = signal;
  const res = await fetch(`${BASE}${path}`, opts);
  if (res.status === 401) {
    localStorage.removeItem('whatsbot_token');
    window.dispatchEvent(new Event('whatsbot:unauthorized'));
    return { ok: false, error: 'Não autenticado.' };
  }
  return res.json();
}

export async function getConfig() {
  return request('GET', '/api/config');
}

export async function saveConfig(config) {
  return request('PUT', '/api/config', config);
}

export async function testApiKey(apiKey) {
  return request('POST', '/api/config/test-key', { api_key: apiKey });
}

export async function getStatus() {
  return request('GET', '/api/status');
}

export async function reconnect() {
  return request('POST', '/api/whatsapp/reconnect');
}

export async function logout() {
  return request('POST', '/api/whatsapp/logout');
}

export async function fetchQrBlob() {
  const res = await fetch(`${BASE}/api/qr?t=${Date.now()}`, {
    headers: _authHeaders(),
  });
  if (!res.ok) return null;
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

export async function refreshQr() {
  return request('POST', '/api/qr/refresh');
}

// ── Setup wizard ───────────────────────────────────────────────────

export async function setupRequestKey() {
  return request('POST', '/api/setup/request-key');
}

export async function setupKeyStatus() {
  return request('GET', '/api/setup/key-status');
}

// ── Sandbox ────────────────────────────────────────────────────────

export async function sandboxSend(phone, message) {
  return request('POST', '/api/sandbox/send', { phone, message });
}

export async function sandboxClear(phone) {
  return request('POST', '/api/sandbox/clear', { phone: phone || '' });
}

async function _sandboxUpload(path, fields) {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    if (value instanceof Blob) form.append(key, value, value.name || 'file');
    else form.append(key, value ?? '');
  }
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: _authHeaders(),
    body: form,
  });
  if (res.status === 401) {
    localStorage.removeItem('whatsbot_token');
    window.dispatchEvent(new Event('whatsbot:unauthorized'));
    return { ok: false, error: 'Não autenticado.' };
  }
  return res.json();
}

export async function sandboxSendImage(phone, file, caption = '') {
  return _sandboxUpload('/api/sandbox/send-image', { phone, caption, image: file });
}

export async function sandboxSendAudio(phone, blob, filename = 'voice.ogg') {
  const named = blob instanceof File ? blob : new File([blob], filename, { type: blob.type || 'audio/ogg' });
  return _sandboxUpload('/api/sandbox/send-audio', { phone, audio: named });
}

export async function sandboxSendDocument(phone, file, caption = '') {
  return _sandboxUpload('/api/sandbox/send-document', { phone, caption, document: file });
}

// ── Contacts ──────────────────────────────────────────────────────

export async function getContacts(q = '', archived = false) {
  const params = [];
  if (archived) params.push('archived=true');
  if (q) params.push(`q=${encodeURIComponent(q)}`);
  const query = params.length ? `?${params.join('&')}` : '';
  return request('GET', `/api/contacts${query}`);
}

// Number of conversations with unread messages (for the browser-tab badge).
export async function getUnreadCount() {
  return request('GET', '/api/contacts/unread-count');
}

// `limit` asks for one page of history (newest first window) instead of the
// whole conversation; `around` centers that page on a message id (search hit).
export async function getContact(phone, markRead = true, { limit = null, around = null, signal = undefined } = {}) {
  const params = [];
  if (!markRead) params.push('mark_read=false');
  if (limit != null) params.push(`limit=${encodeURIComponent(limit)}`);
  if (around != null) params.push(`around=${encodeURIComponent(around)}`);
  const qs = params.length ? `?${params.join('&')}` : '';
  return request('GET', `/api/contacts/${encodeURIComponent(phone)}${qs}`, undefined, signal);
}

// One page of an already-open conversation: pass `before` / `after` / `around`
// (a message id, the `_id` of a loaded message) or none for the newest page.
export async function getContactMessages(phone, { before = null, after = null, around = null, limit = null } = {}) {
  const params = [];
  if (before != null) params.push(`before=${encodeURIComponent(before)}`);
  if (after != null) params.push(`after=${encodeURIComponent(after)}`);
  if (around != null) params.push(`around=${encodeURIComponent(around)}`);
  if (limit != null) params.push(`limit=${encodeURIComponent(limit)}`);
  const qs = params.length ? `?${params.join('&')}` : '';
  return request('GET', `/api/contacts/${encodeURIComponent(phone)}/messages${qs}`);
}

export async function deleteContact(phone) {
  return request('DELETE', `/api/contacts/${encodeURIComponent(phone)}`);
}

export async function archiveContact(phone, archived) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/archive`, { archived });
}

export async function pinContact(phone, pinned) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/pin`, { pinned });
}

export async function sendMessage(phone, message, replyTo = null) {
  const body = { message };
  if (replyTo) body.reply_to = replyTo;
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/send`, body);
}

export async function retrySend(phone, message) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/retry-send`, { message });
}

// Delete a message. scope='me' (local) or scope='all' (revoke for everyone).
// Pass msgId (GOWA id) and/or dbId (DB row id, for local messages without a msg_id).
export async function deleteMessage(phone, { msgId = null, dbId = null, scope = 'me' } = {}) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/messages/delete`, {
    msg_id: msgId, db_id: dbId, scope,
  });
}

// React to a message with an emoji. Empty emoji removes the operator's reaction.
export async function reactToMessage(phone, msgId, emoji) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/messages/react`, {
    msg_id: msgId, emoji,
  });
}

// Fetch again from WhatsApp a file the download policy dropped (best effort).
export async function downloadMessageMedia(phone, msgId) {
  return request('POST',
    `/api/contacts/${encodeURIComponent(phone)}/messages/${encodeURIComponent(msgId)}/download-media`);
}

// File manager (Arquivos). The server only ever receives opaque ids.
export async function getFilesUsage(refresh = false) {
  return request('GET', `/api/files/usage${refresh ? '?refresh=true' : ''}`);
}

export async function listFiles(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '' && v !== false) qs.set(k, String(v));
  });
  return request('GET', `/api/files?${qs.toString()}`);
}

// Contacts/groups a typed name or number points at (photo + number), for the
// "Conversa" filter of the Arquivos screen.
export async function getFileConversations(q, limit = 8) {
  const qs = new URLSearchParams({ q, limit: String(limit) });
  return request('GET', `/api/files/conversations?${qs.toString()}`);
}

export async function deleteFiles(ids) {
  return request('POST', '/api/files/delete', { ids });
}

export async function sendPrivateMessage(phone, text, opts = {}) {
  const body = { text };
  if (opts.aiRead !== undefined) body.ai_read = !!opts.aiRead;
  if (opts.aiReply !== undefined) body.ai_reply = !!opts.aiReply;
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/private-message`, body);
}

export async function generateImprovement(phone, { message, feedback } = {}) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/improve`, {
    message: message || null,
    feedback: feedback || '',
  });
}

export async function markAsRead(phone) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/read`);
}

export async function markAsUnread(phone) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/unread`);
}

export async function markAllUnread() {
  return request('POST', `/api/contacts/mark-all-unread`);
}

export async function markAllRead() {
  return request('POST', `/api/contacts/mark-all-read`);
}

export async function updateContactInfo(phone, info) {
  return request('PUT', `/api/contacts/${encodeURIComponent(phone)}/info`, info);
}

export async function toggleContactAI(phone, enabled) {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/toggle-ai`, { enabled });
}

export async function getGroupMembers(groupJid, force = false) {
  const qs = force ? '?force=true' : '';
  return request('GET', `/api/contacts/${encodeURIComponent(groupJid)}/members${qs}`);
}

export async function sendImage(phone, file, caption = '') {
  const form = new FormData();
  form.append('image', file);
  form.append('caption', caption);
  const res = await fetch(`${BASE}/api/contacts/${encodeURIComponent(phone)}/send-image`, {
    method: 'POST',
    headers: _authHeaders(),
    body: form,
  });
  if (res.status === 401) {
    localStorage.removeItem('whatsbot_token');
    window.dispatchEvent(new Event('whatsbot:unauthorized'));
    return { ok: false, error: 'Não autenticado.' };
  }
  return res.json();
}

export async function sendAudio(phone, blob, filename = 'voice.ogg') {
  const form = new FormData();
  form.append('audio', blob, filename);
  const res = await fetch(`${BASE}/api/contacts/${encodeURIComponent(phone)}/send-audio`, {
    method: 'POST',
    headers: _authHeaders(),
    body: form,
  });
  if (res.status === 401) {
    localStorage.removeItem('whatsbot_token');
    window.dispatchEvent(new Event('whatsbot:unauthorized'));
    return { ok: false, error: 'Não autenticado.' };
  }
  return res.json();
}

export async function sendDocument(phone, file, caption = '') {
  const form = new FormData();
  form.append('document', file);
  form.append('caption', caption);
  const res = await fetch(`${BASE}/api/contacts/${encodeURIComponent(phone)}/send-document`, {
    method: 'POST',
    headers: _authHeaders(),
    body: form,
  });
  if (res.status === 401) {
    localStorage.removeItem('whatsbot_token');
    window.dispatchEvent(new Event('whatsbot:unauthorized'));
    return { ok: false, error: 'Não autenticado.' };
  }
  return res.json();
}

export async function sendPresence(phone, action = 'start') {
  return request('POST', `/api/contacts/${encodeURIComponent(phone)}/presence`, { action });
}

export async function checkPhone(phone) {
  return request('POST', '/api/contacts/check-phone', { phone });
}

// Read-only: says whether `phone` is already a contact. Unlike checkPhone it
// never creates one. Resolves to { exists, contact } in `data`.
export async function lookupContactByPhone(phone) {
  return request('GET', '/api/contacts/lookup?phone=' + encodeURIComponent(phone));
}

// Sends a wa.me link for `phone` to the connected account's own chat, so the
// operator can start the conversation from the official app instead of here.
export async function sendSelfLink(phone) {
  return request('POST', '/api/contacts/send-self-link', { phone });
}

// ── Tags ─────────────────────────────────────────────────────────────

export async function getTags() {
  return request('GET', '/api/tags');
}

export async function createTag(name, color) {
  return request('POST', '/api/tags', { name, color });
}

export async function updateTag(name, data) {
  return request('PUT', `/api/tags/${encodeURIComponent(name)}`, data);
}

export async function deleteTag(name) {
  return request('DELETE', `/api/tags/${encodeURIComponent(name)}`);
}

export async function updateContactTags(phone, tags) {
  return request('PUT', `/api/contacts/${encodeURIComponent(phone)}/tags`, { tags });
}

// ── Models ──────────────────────────────────────────────────────────

export async function getModels() {
  return request('GET', '/api/models');
}

// ── Logs ───────────────────────────────────────────────────────────

export async function getLogs(limit = 200) {
  return request('GET', `/api/logs?limit=${limit}`);
}

export async function clearLogs() {
  return request('DELETE', '/api/logs');
}

// ── Executions ───────────────────────────────────────────────────

export async function getExecutions(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return request('GET', `/api/executions${qs ? '?' + qs : ''}`);
}

export async function getExecution(id) {
  return request('GET', `/api/executions/${id}`);
}

// ── Usage / Costs ─────────────────────────────────────────────────

export async function getUsageSummary(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return request('GET', `/api/usage/summary${qs ? '?' + qs : ''}`);
}

export async function getUsageByContact(params = {}) {
  const qs = new URLSearchParams(params).toString();
  return request('GET', `/api/usage/by-contact${qs ? '?' + qs : ''}`);
}

export async function getUsageContactDetail(phone, params = {}) {
  const qs = new URLSearchParams(params).toString();
  return request('GET', `/api/usage/contact/${encodeURIComponent(phone)}${qs ? '?' + qs : ''}`);
}

// ── Update ────────────────────────────────────────────────────────

export async function checkForUpdates(force = false) {
  return request('GET', `/api/update/check${force ? '?force=true' : ''}`);
}

export async function getLocalVersionInfo() {
  return request('GET', '/api/update/local-version');
}

export async function performUpdate() {
  return request('POST', '/api/update');
}

export async function getCloudUpdateStatus(taskId) {
  return request('GET', `/api/update/status/${encodeURIComponent(taskId)}`);
}

// WhatsBot Cloud: the server recreates this installation, so the backend goes
// offline for a few seconds mid-task. Polls until the task ends, tolerating that
// gap. Resolves to { ok: true, version } or { ok: false, error }.
export async function waitForCloudUpdate(taskId, { intervalMs = 3000, timeoutMs = 15 * 60 * 1000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = '';
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, intervalMs));
    try {
      const res = await getCloudUpdateStatus(taskId);
      if (res && res.ok) {
        if (res.data.status === 'succeeded') return { ok: true, version: res.data.version };
        if (res.data.status === 'failed') return { ok: false, error: res.data.error || 'A atualização falhou.' };
      } else if (res && res.error) {
        lastError = res.error;
      }
    } catch (_) {
      // Container being recreated: keep waiting.
    }
  }
  return { ok: false, error: lastError || 'A atualização demorou mais que o esperado.' };
}

export async function skipWhatsBotVersion(version) {
  return request('POST', '/api/update/skip-version', { version });
}

export async function markUpdatePopupSeen() {
  return request('POST', '/api/update/popup-seen');
}

// ── GOWA (motor do WhatsApp) ──────────────────────────────────────

export async function getGowaVersion() {
  return request('GET', '/api/gowa/version');
}

export async function checkGowaUpdate(force = false) {
  return request('GET', `/api/gowa/update/check${force ? '?force=true' : ''}`);
}

export async function installGowaUpdate(version, forceUnsupported = false) {
  return request('POST', '/api/gowa/update', {
    version: version || '',
    force_unsupported: !!forceUnsupported,
  });
}

export async function rollbackGowa() {
  return request('POST', '/api/gowa/rollback');
}

export async function getGowaProxy() {
  return request('GET', '/api/gowa/proxy');
}

export async function saveGowaProxy(payload) {
  return request('PUT', '/api/gowa/proxy', payload);
}

export async function testGowaProxy(payload) {
  return request('POST', '/api/gowa/proxy/test', payload);
}

export async function skipGowaVersion(version) {
  return request('POST', '/api/gowa/skip-version', { version });
}

// "Agora não" on the modal for the version the WhatsBot-Lite release recommends.
export async function skipRecommendedGowa(version) {
  return request('POST', '/api/gowa/skip-recommended', { version });
}

// ── Auth ──────────────────────────────────────────────────────────

export async function login(password) {
  return request('POST', '/api/auth/login', { password });
}

export async function checkAuth() {
  // checkAuth needs to send token but not trigger unauthorized event on 401
  const opts = {
    method: 'GET',
    headers: _authHeaders({ 'Content-Type': 'application/json' }),
  };
  const res = await fetch(`${BASE}/api/auth/check`, opts);
  return res.json();
}
