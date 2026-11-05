import { h } from 'preact';
import { useState, useEffect, useRef, useCallback } from 'preact/hooks';
import htm from 'htm';
import { getModels } from '../services/api.js';

const html = htm.bind(h);

// Shared cache across all ModelSelect instances
let _modelsCache = null;
let _modelsFetching = false;
let _modelsFetchCallbacks = [];

async function fetchModelsOnce() {
  if (_modelsCache) return _modelsCache;
  if (_modelsFetching) {
    return new Promise(resolve => _modelsFetchCallbacks.push(resolve));
  }
  _modelsFetching = true;
  try {
    const res = await getModels();
    if (res.ok) {
      _modelsCache = res.data;
    } else {
      _modelsCache = [];
    }
  } catch {
    _modelsCache = [];
  }
  _modelsFetching = false;
  _modelsFetchCallbacks.forEach(cb => cb(_modelsCache));
  _modelsFetchCallbacks = [];
  return _modelsCache;
}

const PICKER_LIMIT = 200;

/**
 * Compact searchable model picker for the chat composer.
 * Replaces a native <select> that listed hundreds of models with no way to search.
 * Desktop: popover opening upward (the composer sits at the bottom of the screen).
 * Phone: bottom sheet with a sticky search field. Models come from the caller (Chat.js
 * already fetches them), so there is no second request.
 * @param {object} props
 * @param {Array<{id:string,name?:string}>} props.models
 * @param {string} props.value - current model id
 * @param {function} props.onChange - called with the new model id
 * @param {boolean} [props.disabled]
 * @param {{current:any}} [props.controlRef] - receives { focus() } so "/model" can open it
 */
export function ModelPicker({ models, value, onChange, disabled, controlRef }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [highlight, setHighlight] = useState(0);
  const wrapperRef = useRef(null);
  const searchRef = useRef(null);
  const listRef = useRef(null);

  const ql = query.trim().toLowerCase();
  const matches = ql
    ? models.filter(m => m.id.toLowerCase().includes(ql) || (m.name || '').toLowerCase().includes(ql))
    : models;
  const shown = matches.slice(0, PICKER_LIMIT);
  const current = models.find(m => m.id === value);
  const label = current ? (current.name || current.id) : (value || 'Modelo');

  function openPicker() {
    if (disabled) return;
    setQuery('');
    setOpen(true);
  }
  function close() { setOpen(false); setQuery(''); }
  function choose(id) { onChange(id); close(); }

  useEffect(() => {
    if (controlRef) controlRef.current = { focus: openPicker };
    return () => { if (controlRef) controlRef.current = null; };
  });

  useEffect(() => { setHighlight(0); }, [query]);

  // On open: focus the search on desktop only (on a phone it would raise the keyboard
  // and hide the list), and bring the selected model into view.
  useEffect(() => {
    if (!open) return;
    const touch = window.matchMedia?.('(pointer: coarse)')?.matches;
    if (!touch) searchRef.current?.focus();
    const idx = shown.findIndex(m => m.id === value);
    if (idx >= 0) {
      setHighlight(idx);
      requestAnimationFrame(() => listRef.current?.children[idx]?.scrollIntoView({ block: 'center' }));
    }
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = e => { if (wrapperRef.current && !wrapperRef.current.contains(e.target)) close(); };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  function move(next) {
    const idx = Math.max(0, Math.min(next, shown.length - 1));
    setHighlight(idx);
    listRef.current?.children[idx]?.scrollIntoView({ block: 'nearest' });
  }
  function onKeyDown(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(highlight + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(highlight - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (shown[highlight]) choose(shown[highlight].id); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  }

  return html`
    <div ref=${wrapperRef} class="model-picker">
      <button type="button" class="chat-model-select model-picker-trigger" disabled=${disabled}
        aria-haspopup="listbox" aria-expanded=${open} title=${value ? `Modelo desta conversa: ${value}` : 'Modelo desta conversa'}
        onClick=${() => (open ? close() : openPicker())}>
        <span class="model-picker-label">${label}</span><span class="model-picker-caret" aria-hidden="true">▾</span>
      </button>
      ${open && html`
        <div class="model-picker-backdrop" aria-hidden="true"></div>
        <div class="model-picker-panel" role="dialog" aria-label="Escolher modelo">
          <div class="model-picker-head">
            <input ref=${searchRef} type="search" class="model-picker-search wa-field" value=${query}
              placeholder="Buscar modelo…" autocomplete="off" autocapitalize="off" spellcheck="false"
              onInput=${e => setQuery(e.target.value)} onKeyDown=${onKeyDown} />
            <button type="button" class="model-picker-close" aria-label="Fechar" onClick=${close}>×</button>
          </div>
          <div ref=${listRef} class="model-picker-list wa-scrollbar" role="listbox">
            ${shown.length === 0
              ? html`<div class="model-picker-empty">Nenhum modelo encontrado</div>`
              : shown.map((m, i) => html`
                  <button type="button" role="option" key=${m.id} aria-selected=${m.id === value}
                    class="model-picker-item ${i === highlight ? 'is-active' : ''} ${m.id === value ? 'is-selected' : ''}"
                    onClick=${() => choose(m.id)}>
                    <span class="model-picker-name">${m.name || m.id}</span>
                    <span class="model-picker-id">${m.id}</span>
                  </button>`)}
          </div>
          ${matches.length > shown.length
            ? html`<div class="model-picker-foot">Mostrando ${shown.length} de ${matches.length} — digite para refinar a busca</div>`
            : null}
        </div>
      `}
    </div>
  `;
}

/**
 * Searchable model selector dropdown.
 * @param {object} props
 * @param {string} props.value - Current model ID
 * @param {function} props.onChange - Called with new model ID
 * @param {string} [props.filterModality] - Filter by input modality ("audio", "image", or null for all)
 * @param {string} [props.placeholder] - Placeholder text
 */
export function ModelSelect({ value, onChange, filterModality, placeholder }) {
  const [models, setModels] = useState([]);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [highlightIdx, setHighlightIdx] = useState(0);
  const wrapperRef = useRef(null);
  const listRef = useRef(null);
  const inputRef = useRef(null);

  // Load models on first open
  const loadModels = useCallback(async () => {
    const data = await fetchModelsOnce();
    let filtered = data;
    if (filterModality) {
      filtered = data.filter(m =>
        m.input_modalities && m.input_modalities.includes(filterModality)
      );
    }
    setModels(filtered);
  }, [filterModality]);

  useEffect(() => { loadModels(); }, [loadModels]);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClick(e) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const ql = query.toLowerCase();
  const filtered = query
    ? models.filter(m => m.id.toLowerCase().includes(ql) || m.name.toLowerCase().includes(ql))
    : models;

  // Reset highlight when filtered list changes
  useEffect(() => { setHighlightIdx(0); }, [query]);

  function handleSelect(id) {
    onChange(id);
    setQuery('');
    setOpen(false);
  }

  function handleKeyDown(e) {
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'Enter') {
        setOpen(true);
        e.preventDefault();
      }
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlightIdx(prev => Math.min(prev + 1, filtered.length - 1));
      scrollToHighlight(Math.min(highlightIdx + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlightIdx(prev => Math.max(prev - 1, 0));
      scrollToHighlight(Math.max(highlightIdx - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (filtered[highlightIdx]) handleSelect(filtered[highlightIdx].id);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  }

  function scrollToHighlight(idx) {
    if (listRef.current) {
      const items = listRef.current.children;
      if (items[idx]) items[idx].scrollIntoView({ block: 'nearest' });
    }
  }

  // Find display name for current value
  const currentModel = models.find(m => m.id === value);
  const displayValue = open ? query : (currentModel ? currentModel.name : (value || ''));

  return html`
    <div ref=${wrapperRef} class="relative w-full">
      <input
        ref=${inputRef}
        type="text"
        value=${displayValue}
        placeholder=${placeholder || 'Selecione um modelo...'}
        onFocus=${() => { setOpen(true); setQuery(''); }}
        onInput=${(e) => { setQuery(e.target.value); setOpen(true); }}
        onKeyDown=${handleKeyDown}
        class="w-full bg-wa-panel text-wa-text px-3 py-2 rounded-lg text-sm border border-wa-border focus:border-wa-teal focus:outline-none"
        title=${value || ''}
      />
      ${open && html`
        <div
          ref=${listRef}
          class="absolute z-50 left-0 right-0 mt-1 bg-wa-bg border border-wa-border rounded-lg shadow-lg max-h-[240px] overflow-y-auto wa-scrollbar"
        >
          ${filtered.length === 0
            ? html`<div class="px-3 py-2 text-sm text-wa-secondary">Nenhum modelo encontrado</div>`
            : filtered.slice(0, 100).map((m, i) => html`
                <div
                  key=${m.id}
                  onClick=${() => handleSelect(m.id)}
                  class="px-3 py-[6px] cursor-pointer text-sm hover:bg-wa-hover ${
                    i === highlightIdx ? 'bg-wa-hover' : ''
                  } ${m.id === value ? 'text-wa-teal font-medium' : 'text-wa-text'}"
                >
                  <div class="truncate text-[13px]">${m.name}</div>
                  <div class="truncate text-[11px] text-wa-secondary">${m.id}</div>
                </div>
              `)
          }
        </div>
      `}
    </div>
  `;
}
