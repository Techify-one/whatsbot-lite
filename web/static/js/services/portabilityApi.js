// Cliente HTTP do Importar/Exportar. Fala só com /api/portability/*.
//
// - JSON por `request` (mesmo tratamento de 401 do resto do painel, mas devolvendo também `status` e
//   `error_code`: a tela distingue "defina uma senha" de um erro qualquer).
// - UPLOAD por XMLHttpRequest: o `fetch` não reporta o progresso do ENVIO, e um pacote de GBs sem barra
//   parece travado.
// - DOWNLOAD por POST + blob (a rota é POST para não cair em cache/prefetch/histórico).
//
// A senha NUNCA vai em query string nem é guardada aqui: só atravessa o corpo da chamada.

import { authHeaders, handleUnauthorized } from './api.js';

const UNAUTHORIZED = { ok: false, error: 'Não autenticado.', status: 401 };

async function request(method, path, body) {
  const opts = { method, headers: authHeaders({ 'Content-Type': 'application/json' }), cache: 'no-store' };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let res;
  try {
    res = await fetch(path, opts);
  } catch (_) {
    return { ok: false, error: 'Sem resposta do servidor.', status: 0 };
  }
  if (res.status === 401) { handleUnauthorized(); return { ...UNAUTHORIZED }; }
  let json = null;
  try { json = await res.json(); } catch (_) { json = null; }
  if (!json || typeof json !== 'object') return { ok: false, error: `Erro ${res.status}`, status: res.status };
  return { ...json, status: res.status };
}

const enc = encodeURIComponent;

export const listJobs = (limit = 20) => request('GET', `/api/portability/jobs?limit=${limit}`);
export const getJob = (id) => request('GET', `/api/portability/jobs/${enc(id)}`);
export const cancelJob = (id) => request('POST', `/api/portability/jobs/${enc(id)}/cancel`);
export const createExport = (body) => request('POST', '/api/portability/exports', body);
export const confirmImport = (id, body) => request('POST', `/api/portability/imports/${enc(id)}/confirm`, body);
export const recalculateImport = (id, body) =>
  request('POST', `/api/portability/imports/${enc(id)}/recalculate`, body || {});
export const resumeImport = (id, body) => request('POST', `/api/portability/jobs/${enc(id)}/resume`, body || {});
export const getJobErrors = (id, { entity = '', limit = 100, after = null } = {}) => {
  const q = new URLSearchParams({ limit: String(limit) });
  if (entity) q.set('entity', entity);
  if (after != null) q.set('after', String(after));
  return request('GET', `/api/portability/jobs/${enc(id)}/errors?${q}`);
};

/**
 * Envia o pacote com progresso de envio.
 * @returns {Promise<any>} o envelope `{ok, data|error}`
 */
export function uploadImport(file, { password = '', onProgress, signal = null } = {}) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/portability/imports');
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);
    xhr.responseType = 'json';
    if (onProgress) {
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    }
    xhr.onload = () => {
      if (xhr.status === 401) { handleUnauthorized(); resolve({ ...UNAUTHORIZED }); return; }
      const body = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300 && body) { resolve(body); return; }
      const error = (body && (body.error || body.detail)) || `Erro ${xhr.status}`;
      resolve({ ok: false, error, status: xhr.status, error_code: body && body.error_code });
    };
    xhr.onerror = () => resolve({ ok: false, error: 'Falha de rede durante o envio.' });
    xhr.onabort = () => resolve({ ok: false, error: 'Envio cancelado.', aborted: true });
    if (signal) {
      if (signal.aborted) { resolve({ ok: false, error: 'Envio cancelado.', aborted: true }); return; }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }
    const form = new FormData();
    form.append('file', file, file.name || 'pacote.zip');
    if (password) form.append('password', password);
    xhr.send(form);
  });
}

/** Nome do arquivo do `Content-Disposition`, ou null. Só aceita nome "seguro". */
export function filenameFrom(header) {
  const m = /filename="?([^";]+)"?/i.exec(header || '');
  const name = m ? m[1].trim() : '';
  return /^[\w.\-]{1,120}$/.test(name) ? name : null;
}

/** Baixa o pacote de um export concluído e dispara o "salvar como". */
export async function downloadExport(id) {
  let res;
  try {
    res = await fetch(`/api/portability/jobs/${enc(id)}/download`, {
      method: 'POST', headers: authHeaders(), cache: 'no-store',
    });
  } catch (_) {
    return { ok: false, error: 'Sem resposta do servidor.' };
  }
  if (res.status === 401) { handleUnauthorized(); return { ...UNAUTHORIZED }; }
  if (!res.ok) {
    let error = `Erro ${res.status}`;
    try { error = (await res.json()).error || error; } catch (_) { /* corpo não é JSON */ }
    return { ok: false, error };
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filenameFrom(res.headers.get('content-disposition'))
    || `whatsbot-lite-export-${String(id).slice(0, 8)}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { ok: true };
}
