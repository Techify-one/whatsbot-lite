import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { authHeaders, handleUnauthorized } from '../services/api.js';
import { newIntent, normalizeIntent, availableTools, taskLabels } from '../utils/intentsLogic.js';
const html = htm.bind(h);
const fieldClass = 'wa-field w-full border border-wa-border rounded px-3 py-2 text-sm';
const buttonClass = 'border border-wa-border rounded px-3 py-2 text-sm hover:bg-wa-hover disabled:opacity-50';
async function api(path='', method='GET', body, signal) {
  const response = await fetch('/api/intents'+path, {method, headers:authHeaders({'Content-Type':'application/json'}), body:body === undefined ? undefined : JSON.stringify(body), signal});
  if(response.status === 401) handleUnauthorized();
  const result = await response.json();
  if(!response.ok || !result.ok) throw Error(result.error || 'Não foi possível concluir a solicitação.');
  return result.data;
}
function Field({label, value, onChange, multiline=false, ...props}) {
  return html`<label class="block text-sm space-y-1"><span>${label}</span>${multiline
    ? html`<textarea class=${fieldClass} rows="3" value=${Array.isArray(value)?value.join('\n'):value || ''} onInput=${e=>onChange(e.target.value)} ...${props} />`
    : html`<input class=${fieldClass} value=${value ?? ''} onInput=${e=>onChange(e.target.value)} ...${props} />`}</label>`;
}
function IntentEditor({initial, tools, busy, onSave, onCancel}) {
  const [draft,setDraft] = useState({...newIntent(),...initial});
  const [parameters,setParameters] = useState(JSON.stringify(initial.tool_parameters || {},null,2));
  const [error,setError] = useState('');
  const change = (key,value)=>setDraft(d=>({...d,[key]:value}));
  const selected = draft.tools || [];
  function save(e) { e.preventDefault(); try {setError(''); onSave(normalizeIntent(draft,parameters));} catch(e) {setError(e.message);} }
  return html`<form onSubmit=${save} class="bg-wa-bg border border-wa-border rounded-lg p-4 space-y-4" aria-label="Editor de intenção">
    <h2 class="font-semibold text-lg">${initial.id?'Editar intenção':'Nova intenção'}</h2>
    ${error && html`<p role="alert" class="text-red-600">${error}</p>`}
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <${Field} label="Nome" value=${draft.name} onChange=${v=>change('name',v)} required maxlength="120" />
      <${Field} label="Identificador único" value=${draft.id} onChange=${v=>change('id',v)} disabled=${!!initial.id} required placeholder="agendar_reuniao" />
      <${Field} label="Descrição para classificação" value=${draft.description} onChange=${v=>change('description',v)} multiline required />
      <${Field} label="Instruções específicas ao agente" value=${draft.instructions} onChange=${v=>change('instructions',v)} multiline />
      <${Field} label="Exemplos positivos (um por linha)" value=${draft.positive_examples} onChange=${v=>change('positive_examples',v)} multiline />
      <${Field} label="Exemplos negativos (um por linha)" value=${draft.negative_examples} onChange=${v=>change('negative_examples',v)} multiline />
      <${Field} label="Conversas autorizadas (um identificador por linha)" value=${draft.contacts} onChange=${v=>change('contacts',v)} multiline />
      <${Field} label="Capacidades necessárias (uma por linha)" value=${draft.required_capabilities} onChange=${v=>change('required_capabilities',v)} multiline placeholder="calendar.availability&#10;calendar.create" />
      <${Field} label="Dados necessários (um por linha)" value=${draft.required_data} onChange=${v=>change('required_data',v)} multiline />
      <${Field} label="Critérios de sucesso / conclusão" value=${draft.success_criteria} onChange=${v=>change('success_criteria',v)} multiline />
      <${Field} label="Critérios de cancelamento / transferência" value=${draft.cancel_criteria} onChange=${v=>change('cancel_criteria',v)} multiline />
      <label class="block text-sm space-y-1">Em caso de ambiguidade<select class=${fieldClass} value=${draft.ambiguous_policy || 'silent'} onChange=${e=>change('ambiguous_policy',e.target.value)}><option value="silent">Permanecer silencioso</option><option value="owner_approval">Solicitar decisão do proprietário</option></select></label>
      <${Field} label="Prioridade" type="number" min="-100" max="100" step="1" value=${draft.priority} onChange=${v=>change('priority',v)} />
      <${Field} label="Confiança mínima (0,5 a 1)" type="number" min="0.5" max="1" step="0.01" value=${draft.min_confidence} onChange=${v=>change('min_confidence',v)} />
      <${Field} label="Expirar tarefa após (segundos)" type="number" min="60" max="86400" value=${draft.timeout_seconds} onChange=${v=>change('timeout_seconds',v)} />
      <label class="block text-sm space-y-1">Política de execução<select class=${fieldClass} value=${draft.policy} onChange=${e=>change('policy',e.target.value)}>
        <option value="owner_approval">Exigir aprovação do proprietário</option><option value="contact_confirmation">Exigir confirmação do contato</option><option value="automatic">Automática (apenas ações autorizadas)</option><option value="deny">Não executar</option>
      </select></label>
    </div>
    <div class="flex flex-wrap gap-4"><label><input type="checkbox" checked=${draft.enabled} onChange=${e=>change('enabled',e.target.checked)} /> Habilitada</label><label><input type="checkbox" checked=${draft.allow_all_contacts} onChange=${e=>change('allow_all_contacts',e.target.checked)} /> Autorizar todos os contatos</label><label><input type="checkbox" checked=${draft.allow_groups} onChange=${e=>change('allow_groups',e.target.checked)} /> Permitir grupos autorizados</label></div>
    <fieldset class="border border-wa-border rounded p-3 space-y-2"><legend class="px-2 font-medium">Ferramentas permitidas</legend>
      <p class="text-sm text-wa-secondary">Selecione explicitamente cada ferramenta. Nenhuma recebe acesso automaticamente. Ferramentas indisponíveis impedem a execução.</p>
      ${availableTools(tools,selected).map(tool=>html`<label key=${tool.name} class="flex gap-2 items-start"><input type="checkbox" checked=${selected.includes(tool.name)} disabled=${tool.available===false && !selected.includes(tool.name)} onChange=${e=>change('tools',e.target.checked?[...selected,tool.name]:selected.filter(n=>n!==tool.name))} /><span>${tool.label || tool.name} <span class="text-sm text-wa-secondary">(${tool.plugin_id || 'core'}) · ${tool.capability || tool.name}${tool.available===false?' — indisponível':''}</span></span></label>`)}
      ${!tools.length && html`<p class="text-sm text-wa-secondary">Nenhuma ferramenta disponível. Configure um plugin antes de habilitar ações.</p>`}
      <${Field} label="Parâmetros fixos autorizados por ferramenta (JSON)" value=${parameters} onChange=${setParameters} multiline spellcheck="false" />
    </fieldset>
    <div class="flex gap-2"><button class="bg-wa-teal text-white rounded px-4 py-2 disabled:opacity-50" disabled=${busy} type="submit">Salvar intenção</button><button class=${buttonClass} type="button" onClick=${onCancel} disabled=${busy}>Cancelar edição</button></div>
  </form>`;
}
export function IntentsManager() {
  const [intents,setIntents]=useState([]), [tools,setTools]=useState([]), [tasks,setTasks]=useState([]), [decisions,setDecisions]=useState([]);
  const [settings,setSettings]=useState(null), [editor,setEditor]=useState(null), [error,setError]=useState(''), [notice,setNotice]=useState(''), [busy,setBusy]=useState(false);
  const [testText,setTestText]=useState(''), [testPhone,setTestPhone]=useState(''), [testResult,setTestResult]=useState(null);
  const [key,setKey]=useState(''), [removeKey,setRemoveKey]=useState(false);
  const mounted=useRef(true), operation=useRef(false);
  async function refresh(signal) {
    const [i,t,s,r,d]=await Promise.all([api('', 'GET',undefined,signal),api('/tools','GET',undefined,signal),api('/settings','GET',undefined,signal),api('/tasks','GET',undefined,signal),api('/decisions','GET',undefined,signal)]);
    if(!mounted.current)return;
    setIntents(i || []);setTools(t || []);setSettings(s);setTasks(r || []);setDecisions(d || []);
  }
  useEffect(()=>{mounted.current=true;const controller=new AbortController();refresh(controller.signal).catch(e=>{if(e.name!=='AbortError')setError(e.message);});return()=>{mounted.current=false;controller.abort();};},[]);
  async function run(fn, message) {
    if(operation.current)return;operation.current=true;setBusy(true);setError('');setNotice('');
    try{await fn();if(mounted.current){await refresh();setNotice(message || 'Salvo.');}}catch(e){if(mounted.current)setError(e.message);}finally{operation.current=false;if(mounted.current)setBusy(false);}
  }
  const changeSetting=(k,v)=>setSettings(s=>({...s,[k]:v}));
  const saveSettings=()=>run(async()=>{const payload={...settings};delete payload.key_configured;delete payload.available;delete payload.unavailable_reason;delete payload.executor_configured;delete payload.panel_protected;if(key)payload.api_key=key;if(removeKey)payload.clear_api_key=true;await api('/settings','PUT',payload);setKey('');setRemoveKey(false);},'Configuração aplicada.');
  return html`<div class="space-y-5 text-wa-text">
    <p class="text-sm text-wa-secondary">No modo assistido, a IA permanece silenciosa até reconhecer uma intenção habilitada e autorizada. Configurações são aplicadas sem reinício.</p>
    ${error && html`<div role="alert" class="border border-red-400 rounded p-3 text-red-600">${error}</div>`}${notice && html`<p role="status" class="text-wa-teal">${notice}</p>`}
    ${!settings?html`<p>Carregando configurações…</p>`:html`<section class="bg-wa-bg border border-wa-border rounded-lg p-4 space-y-4" aria-label="Configuração do modo assistido">
      <h2 class="font-semibold text-lg">Modo e classificador</h2>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <label class="block text-sm space-y-1">Modo da IA<select class=${fieldClass} value=${settings.mode || 'disabled'} onChange=${e=>changeSetting('mode',e.target.value)}><option value="disabled">Desativado</option><option value="assisted">Assistido por Intenções</option><option value="automatic">Automático</option></select></label>
        <label class="block text-sm space-y-1">Provedor<select class=${fieldClass} value=${settings.provider || 'jev'} onChange=${e=>changeSetting('provider',e.target.value)}><option value="jev">Jev · TypeSafe AI</option><option value="openai">LLM com JSON estruturado</option></select></label>
        <${Field} label="URL HTTPS do provedor" value=${settings.base_url} onChange=${v=>changeSetting('base_url',v)} type="url" />
        <${Field} label="Modelo" value=${settings.model} onChange=${v=>changeSetting('model',v)} />
        <${Field} label=${settings.key_configured?'Nova chave (deixe em branco para manter)':'Chave da API'} value=${key} onChange=${setKey} type="password" autocomplete="new-password" />
        <${Field} label="Janela de contexto (mensagens)" value=${settings.context_limit ?? 20} onChange=${v=>changeSetting('context_limit',Number(v))} type="number" min="10" max="50" />
        <${Field} label="Timeout do classificador (segundos)" value=${settings.timeout_seconds ?? 15} onChange=${v=>changeSetting('timeout_seconds',Number(v))} type="number" min="5" max="120" />
        <${Field} label="Máximo de chamadas por minuto" value=${settings.max_calls_per_minute ?? 20} onChange=${v=>changeSetting('max_calls_per_minute',Number(v))} type="number" min="1" />
        <${Field} label="Limite diário estimado (USD)" value=${settings.daily_budget_usd ?? 1} onChange=${v=>changeSetting('daily_budget_usd',Number(v))} type="number" min="0.01" max="100" step="0.01" />
      </div>
      <p class="text-sm text-wa-secondary">A classificação envia ao provedor escolhido apenas a janela de contexto configurada. Não há troca automática de provedor pago. Sem credenciais válidas, não é possível ativar o modo assistido.</p>
      ${!settings.key_configured && html`<p class="text-sm text-amber-600">Classificador sem chave: salve as credenciais antes de ativar o modo assistido.</p>`}
      ${settings.executor_configured===false && html`<p class="text-sm text-amber-600">Configure o provedor de IA do executor nas Configurações antes de ativar tarefas.</p>`}
      ${settings.panel_protected===false && html`<p class="text-sm text-amber-600">Defina uma senha de acesso ao painel nas Configurações antes de ativar tarefas.</p>`}
      ${settings.unavailable_reason && html`<p role="status" class="text-amber-600">${settings.unavailable_reason}</p>`}
      ${settings.key_configured && html`<label class="block text-sm"><input type="checkbox" checked=${removeKey} onChange=${e=>setRemoveKey(e.target.checked)} /> Remover a chave salva</label>`}
      <button class=${buttonClass} disabled=${busy} onClick=${saveSettings}>Salvar configuração</button>
    </section>`}
    <section class="space-y-3"><div class="flex justify-between gap-2 items-center"><h2 class="font-semibold text-lg">Intenções cadastradas</h2><button class=${buttonClass} disabled=${busy || editor!==null} onClick=${()=>setEditor({})}>Nova intenção</button></div>
      ${editor!==null && html`<${IntentEditor} key=${editor.id || 'new'} initial=${editor} tools=${tools} busy=${busy} onCancel=${()=>setEditor(null)} onSave=${draft=>run(async()=>{await api(editor.id?'/'+encodeURIComponent(editor.id):'',editor.id?'PUT':'POST',draft);setEditor(null);},'Intenção salva.')} />`}
      ${intents.map(intent=>html`<article key=${intent.id} class="border border-wa-border rounded p-3 bg-wa-bg"><div class="flex flex-wrap justify-between gap-2"><div><strong>${intent.name}</strong><p class="text-sm text-wa-secondary">${intent.id} · ${intent.plugin_id?'Plugin '+intent.plugin_id:'Personalizada'} · ${intent.enabled?'Habilitada':'Desabilitada'}</p></div><div class="flex flex-wrap gap-2"><button class=${buttonClass} disabled=${busy || editor!==null} onClick=${()=>setEditor(intent)}>Editar</button><button class=${buttonClass} disabled=${busy} onClick=${()=>run(()=>api('/'+encodeURIComponent(intent.id),'PUT',{...intent,enabled:!intent.enabled}),'Estado atualizado.')}>${intent.enabled?'Desativar':'Ativar'}</button><button class=${buttonClass} disabled=${busy} onClick=${()=>{if(window.confirm('Excluir a intenção '+intent.name+'?'))run(()=>api('/'+encodeURIComponent(intent.id),'DELETE'),'Intenção excluída.');}}>Excluir</button></div></div><p class="text-sm mt-2">${intent.description}</p></article>`)}
      ${!intents.length && html`<p class="text-wa-secondary">Nenhuma intenção cadastrada. Crie uma intenção e associe somente as ferramentas necessárias.</p>`}
    </section>
    <section class="border border-wa-border rounded-lg p-4 space-y-3 bg-wa-bg"><h2 class="font-semibold text-lg">Teste de classificação</h2><p class="text-sm text-wa-secondary">Não envia mensagens nem executa ferramentas. A consulta ao classificador pode consumir crédito no provedor configurado.</p>
      <${Field} label="Conversa para testar elegibilidade" value=${testPhone} onChange=${setTestPhone} placeholder="Identificador do contato ou grupo" />
      <${Field} label="Mensagem de exemplo" value=${testText} onChange=${setTestText} multiline />
      <button class=${buttonClass} disabled=${busy || !testText.trim()} onClick=${()=>run(async()=>setTestResult(await api('/test','POST',{text:testText,phone:testPhone})),'Simulação concluída, sem mensagens ou ferramentas.')}>Testar sem executar</button>
      ${testResult && html`<pre class="whitespace-pre-wrap break-words text-sm p-3 border border-wa-border rounded" aria-label="Resultado da simulação">${JSON.stringify(testResult,null,2)}</pre>`}
    </section>
    <details class="border border-wa-border rounded p-3"><summary>Últimas decisões silenciosas</summary><pre class="whitespace-pre-wrap break-words text-xs">${JSON.stringify(decisions,null,2)}</pre></details>
    <section class="space-y-3"><div class="flex justify-between items-center"><h2 class="font-semibold text-lg">Tarefas e histórico</h2><button class=${buttonClass} disabled=${busy} onClick=${()=>run(async()=>{},'Histórico atualizado.')}>Atualizar</button></div>
      ${tasks.map(task=>html`<article key=${task.id} class="border border-wa-border rounded bg-wa-bg p-3 space-y-2"><div><strong>${task.intent_name || task.intent_id}</strong> · ${taskLabels[task.state || task.status] || task.state || task.status}<p class="text-sm text-wa-secondary">Conversa: ${task.phone || task.conversation_id} · ${task.created_at || ''}</p></div><p class="text-sm">${task.reason || task.error || ''}</p>${task.payload?.outcome_unknown && html`<p role="alert" class="text-red-600">Resultado externo incerto. A conversa está bloqueada para novas tarefas. Verifique a integração antes de cancelar ou transferir; aprovar novamente não é permitido.</p>`}${task.payload?.pending_operation && html`<section class="border border-amber-400 rounded p-3 space-y-2" aria-label="Operação aguardando aprovação"><strong>Revise a operação exata antes de aprovar</strong><p class="text-sm">Ferramenta: ${task.payload.pending_operation.tool} · Conversa: ${task.phone}</p><pre class="whitespace-pre-wrap break-words text-sm">${JSON.stringify(task.payload.pending_operation.args,null,2)}</pre><p class="text-sm">A aprovação autoriza esta ferramenta com estes parâmetros. Confira destinatários, datas, valores e demais efeitos.</p></section>`}<div class="flex flex-wrap gap-2">${['pause','cancel','transfer','approve'].map(action=>html`<button class=${buttonClass} disabled=${busy || ['completed','cancelled','expired','transferred','failed'].includes(task.state || task.status) || (action==='approve' && (task.payload?.outcome_unknown || !['waiting_owner','paused'].includes(task.state || task.status)))} onClick=${()=>{if(task.payload?.outcome_unknown && !window.confirm('Verificou o resultado real na integração e reconciliou a operação? Liberar a conversa sem conferir pode duplicar ações.'))return;if(action==='approve' && !window.confirm(task.payload?.pending_operation ? 'Autorizar '+task.payload.pending_operation.tool+' com os parâmetros exibidos para '+task.phone+'?' : 'Iniciar esta tarefa com as ferramentas selecionadas? Operações de escrita terão aprovação específica.'))return;run(()=>api('/tasks/'+encodeURIComponent(task.id)+'/'+action,'POST',{reconciled:!!task.payload?.outcome_unknown}),'Tarefa atualizada.');}}>${{pause:'Pausar',cancel:'Cancelar',transfer:'Transferir para humano',approve:'Aprovar'}[action]}</button>`)}</div>${task.audit && html`<details><summary class="text-sm cursor-pointer">Decisões e diagnóstico</summary><pre class="whitespace-pre-wrap break-words text-xs">${JSON.stringify(task.audit,null,2)}</pre></details>`}</article>`)}
      ${!tasks.length && html`<p class="text-wa-secondary">Nenhuma execução registrada.</p>`}
    </section>
  </div>`;
}
