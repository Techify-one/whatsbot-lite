// Pure form helpers shared by the administrative screen and offline tests.
export const lines = value => Array.isArray(value) ? value : String(value || '').split('\n').map(v => v.trim()).filter(Boolean);
export function newIntent() {
  return {id:'', name:'', description:'', positive_examples:[], negative_examples:[], enabled:false,
    priority:0, min_confidence:0.85, contacts:[], allow_all_contacts:false, allow_groups:false, tools:[], tool_parameters:{},
    instructions:'', required_capabilities:[], required_data:[], success_criteria:'', cancel_criteria:'', timeout_seconds:900,
    policy:'owner_approval', ambiguous_policy:'silent', agent_id:null};
}
export function normalizeIntent(draft, parameters) {
  const data = {...draft};
  for (const key of ['positive_examples','negative_examples','contacts','required_data','required_capabilities']) data[key] = lines(data[key]);
  if (!/^[a-z][a-z0-9_]{0,99}$/.test(data.id)) throw Error('Identificador: use 1–100 letras minúsculas, números e sublinhado, começando por letra.');
  if (!data.name.trim() || !data.description.trim()) throw Error('Informe nome e descrição.');
  data.priority = Number(data.priority); data.min_confidence = Number(data.min_confidence); data.timeout_seconds = Number(data.timeout_seconds);
  if (!Number.isFinite(data.min_confidence) || data.min_confidence < 0.5 || data.min_confidence > 1) throw Error('Confiança deve estar entre 0,5 e 1.');
  if (!Number.isInteger(data.priority) || !Number.isFinite(data.timeout_seconds) || data.timeout_seconds < 60 || data.timeout_seconds > 86400 || data.priority < -100 || data.priority > 100) throw Error('Informe prioridade inteira de -100 a 100 e timeout de 60 a 86400 segundos.');
  if (parameters !== undefined) {
    try { data.tool_parameters = JSON.parse(parameters || '{}'); } catch { throw Error('Parâmetros das ferramentas: JSON inválido.'); }
  }
  if (!data.tool_parameters || Array.isArray(data.tool_parameters) || typeof data.tool_parameters !== 'object') throw Error('Parâmetros devem ser um objeto JSON.');
  data.tools = [...new Set(data.tools || [])];
  return data;
}
export function availableTools(catalog, selected) {
  const names = new Set(catalog.map(t => t.name));
  return [...catalog, ...selected.filter(n => !names.has(n)).map(name => ({name, label:name, plugin_id:'indisponível', available:false}))];
}
export const taskLabels = {monitoring:'Monitorando',detected:'Intenção detectada',validating:'Validando',executing:'Executando',completed:'Concluída',cancelled:'Cancelada',expired:'Expirada',transferred:'Transferida',failed:'Falhou',paused:'Pausada',waiting_owner:'Aguardando proprietário',waiting_contact:'Aguardando contato'};
