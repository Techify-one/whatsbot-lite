import { h } from 'preact';
import { useState, useEffect, useRef } from 'preact/hooks';
import htm from 'htm';
import { SearchIcon, DefaultAvatar, GroupAvatar, SingleCheckIcon, DoubleCheckIcon, ClockIcon, ArchiveIcon } from './icons.js';
import { formatTime, avatarUrl, formatPhoneDisplay } from './utils.js';
import { TagPicker } from './TagPicker.js';

const html = htm.bind(h);

// Kebab (3-dots) menu icon, shared by the header menus. Defined as components
// (functions returning a vnode) so they can be used as <${KebabIcon} />.
const KebabIcon = () => html`
  <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor">
    <path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/>
  </svg>
`;
const PinIcon = () => html`
  <svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">
    <path d="M16 9V4h1c.55 0 1-.45 1-1s-.45-1-1-1H7c-.55 0-1 .45-1 1s.45 1 1 1h1v5c0 1.66-1.34 3-3 3v2h5.97v7l1 1 1-1v-7H19v-2c-1.66 0-3-1.34-3-3z"/>
  </svg>
`;

function normalizePhone(input) {
  const digits = input.replace(/\D/g, '');
  if (digits.length < 10) return null;
  if (digits.startsWith('55')) return digits;
  return '55' + digits;
}

function looksLikePhone(input) {
  return input.replace(/\D/g, '').length >= 10;
}

// Casefold + strip accents, mirroring the backend `_fold` so highlighting matches
// the same way the search does.
function foldStr(s) {
  return (s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
}

// Split `text` into {s, hit} segments around occurrences of `query`
// (accent/case-insensitive). Falls back to one plain segment when folding changes
// the length (then folded indices can't be mapped back to the original text).
function highlightParts(text, query) {
  const t = text || '';
  const q = foldStr(query);
  if (!q) return [{ s: t, hit: false }];
  const f = foldStr(t);
  if (f.length !== t.length) return [{ s: t, hit: false }];
  const parts = [];
  let i = 0;
  while (i <= t.length) {
    const idx = f.indexOf(q, i);
    if (idx === -1) { if (i < t.length) parts.push({ s: t.slice(i), hit: false }); break; }
    if (idx > i) parts.push({ s: t.slice(i, idx), hit: false });
    parts.push({ s: t.slice(idx, idx + q.length), hit: true });
    i = idx + q.length;
  }
  return parts;
}

// ── Contact List (WhatsApp Web sidebar) ──────────────────────────

export function ContactList({ contacts, loading, search, onSearchChange, selected, onSelect, onContextMenu, typingState, showArchived, onToggleArchived, globalTags, onStartConversation, checkingPhone, checkPhoneError, wsConnected, autoReply, onToggleAutoReply,
  selectionMode, selectedPhones, onEnterSelection, onExitSelection, onToggleSelect, onSelectAll, onClearSelection, onBulkAI, onBulkArchive, onBulkTag, onBulkRemoveAllTags, onBulkPin, onBulkMarkRead, onBulkMarkUnread, onCreateTag }) {
  const headerBg = wsConnected === false ? 'bg-[#6b2c2c]' : showArchived ? 'bg-[#2a3942]' : 'bg-wa-teal';
  const selCount = (selectedPhones || []).length;
  const selectedSet = new Set(selectedPhones || []);
  // For the bulk-tag toggle indicator: does every selected conversation have this tag?
  const selectedContacts = (contacts || []).filter(c => selectedSet.has(c.phone));
  const allSelectedHaveTag = (name) =>
    selectedContacts.length > 0 && selectedContacts.every(c => (c.tags || []).includes(name));
  // Pin toggle: when every selected is already pinned, the action unpins all.
  const allSelectedPinned = selectedContacts.length > 0 && selectedContacts.every(c => c.is_pinned);

  // Header dropdown state (one menu visible at a time given selectionMode).
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const [bulkMenuOpen, setBulkMenuOpen] = useState(false);
  const [bulkTagsOpen, setBulkTagsOpen] = useState(false);
  const menuRef = useRef(null);

  function closeMenus() {
    setHeaderMenuOpen(false);
    setBulkMenuOpen(false);
    setBulkTagsOpen(false);
  }

  useEffect(() => {
    if (!headerMenuOpen && !bulkMenuOpen) return;
    function onDoc(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) closeMenus();
    }
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [headerMenuOpen, bulkMenuOpen]);

  // Leaving selection mode collapses any open bulk menu.
  useEffect(() => { if (!selectionMode) closeMenus(); }, [selectionMode]);

  return html`
    <div class="flex flex-col h-full bg-wa-bg">
      ${selectionMode ? html`
      <!-- Selection header -->
      <div class="h-[59px] flex items-center justify-between px-4 bg-[#2a3942] shrink-0">
        <div class="flex items-center gap-3 min-w-0">
          <button
            onClick=${onExitSelection}
            class="w-[40px] h-[40px] rounded-full flex items-center justify-center hover:bg-white/10 text-white shrink-0"
            title="Sair da seleção"
          >
            <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M18.3 5.71L12 12.01l-6.3-6.3-1.42 1.42 6.3 6.29-6.3 6.3 1.42 1.41 6.3-6.29 6.29 6.29 1.41-1.41-6.29-6.3 6.3-6.29z"/></svg>
          </button>
          <span class="text-white text-[16px] font-medium truncate">Selecionadas: ${selCount}</span>
        </div>
        <div ref=${menuRef} class="relative shrink-0">
          <button
            onClick=${() => { setBulkMenuOpen(o => !o); setBulkTagsOpen(false); }}
            class="w-[40px] h-[40px] rounded-full flex items-center justify-center hover:bg-white/10 text-white"
            title="Ações em massa"
          ><${KebabIcon} /></button>
          ${bulkMenuOpen ? html`
            <div class="absolute right-0 top-[46px] z-[60] bg-wa-panel rounded-lg shadow-lg border border-wa-border py-[4px] min-w-[238px]">
              <button
                disabled=${selCount === 0}
                onClick=${() => { if (confirm(`Ativar a IA para ${selCount} conversa(s) selecionada(s)?`)) { onBulkAI && onBulkAI(true); } closeMenus(); }}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="#00a884"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                Ativar IA
              </button>
              <button
                disabled=${selCount === 0}
                onClick=${() => { if (confirm(`Desativar a IA para ${selCount} conversa(s) selecionada(s)?`)) { onBulkAI && onBulkAI(false); } closeMenus(); }}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="#ef4444"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm5 13.59L15.59 17 12 13.41 8.41 17 7 15.59 10.59 12 7 8.41 8.41 7 12 10.59 15.59 7 17 8.41 13.41 12 17 15.59z"/></svg>
                Desativar IA
              </button>
              <button
                disabled=${selCount === 0}
                onClick=${() => { onBulkPin && onBulkPin(!allSelectedPinned); closeMenus(); }}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M16 9V4h1c.55 0 1-.45 1-1s-.45-1-1-1H7c-.55 0-1 .45-1 1s.45 1 1 1h1v5c0 1.66-1.34 3-3 3v2h5.97v7l1 1 1-1v-7H19v-2c-1.66 0-3-1.34-3-3z"/></svg>
                ${allSelectedPinned ? 'Desafixar conversas' : 'Fixar conversas'}
              </button>
              <button
                disabled=${selCount === 0}
                onClick=${() => { onBulkMarkRead && onBulkMarkRead(); closeMenus(); }}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>
                Marcar como lidas
              </button>
              <button
                disabled=${selCount === 0}
                onClick=${() => { onBulkMarkUnread && onBulkMarkUnread(); closeMenus(); }}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="#00a884"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>
                Marcar como não lidas
              </button>
              <button
                disabled=${selCount === 0}
                onClick=${() => setBulkTagsOpen(o => !o)}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M21.41 11.58l-9-9C12.05 2.22 11.55 2 11 2H4c-1.1 0-2 .9-2 2v7c0 .55.22 1.05.59 1.42l9 9c.36.36.86.58 1.41.58.55 0 1.05-.22 1.41-.59l7-7c.37-.36.59-.86.59-1.41 0-.55-.23-1.06-.59-1.42zM5.5 7C4.67 7 4 6.33 4 5.5S4.67 4 5.5 4 7 4.67 7 5.5 6.33 7 5.5 7z"/></svg>
                Adicionar tags
                <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" class="ml-auto transition-transform ${bulkTagsOpen ? 'rotate-180' : ''}"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>
              </button>
              ${(bulkTagsOpen && selCount > 0) ? html`
                <div class="border-t border-wa-border">
                  <button
                    onClick=${() => { if (confirm(`Remover TODAS as tags de ${selCount} conversa(s) selecionada(s)?`)) onBulkRemoveAllTags && onBulkRemoveAllTags(); }}
                    class="w-full text-left px-4 py-[8px] text-[13px] text-wa-text hover:bg-wa-hover transition-colors flex items-center gap-3"
                  >
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="#ef4444"><path d="M19 13H5v-2h14v2z"/></svg>
                    Remover todas as tags
                  </button>
                  <${TagPicker}
                    globalTags=${globalTags}
                    isActive=${allSelectedHaveTag}
                    onToggle=${(name) => onBulkTag && onBulkTag(name)}
                    onCreateTag=${onCreateTag}
                  />
                </div>
              ` : ''}
              <button
                disabled=${selCount === 0}
                onClick=${() => { onBulkArchive && onBulkArchive(); closeMenus(); }}
                class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
              >
                <span class="text-wa-text"><${ArchiveIcon} /></span>
                ${showArchived ? 'Desarquivar conversas' : 'Arquivar conversas'}
              </button>
              <div class="border-t border-wa-border">
                <button
                  onClick=${() => { onSelectAll && onSelectAll(); }}
                  class="w-full text-left px-4 py-[10px] text-[14px] text-wa-text hover:bg-wa-hover transition-colors flex items-center gap-3"
                >
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-9 14l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                  Selecionar todas
                </button>
                <button
                  disabled=${selCount === 0}
                  onClick=${() => { onClearSelection && onClearSelection(); closeMenus(); }}
                  class="w-full text-left px-4 py-[10px] text-[14px] hover:bg-wa-hover transition-colors flex items-center gap-3 ${selCount === 0 ? 'opacity-40 cursor-not-allowed text-wa-secondary' : 'text-wa-text'}"
                >
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zM7 13v-2h10v2H7z"/></svg>
                  Limpar conversas selecionadas
                </button>
              </div>
            </div>
          ` : ''}
        </div>
      </div>
      ` : html`
      <!-- Green header bar -->
      <div class="h-[59px] flex items-center justify-between px-4 ${headerBg} shrink-0 transition-colors">
        <div class="flex items-center gap-3">
          <button
            onClick=${onToggleArchived}
            class="w-[40px] h-[40px] rounded-full flex items-center justify-center hover:bg-white/10 transition-colors ${showArchived ? 'bg-white/15' : ''}"
            title=${showArchived ? 'Voltar às conversas' : 'Ver arquivados'}
          >
            <span class="text-white"><${ArchiveIcon} /></span>
          </button>
          <button
            onClick=${() => {
              const msg = autoReply
                ? 'Deseja DESATIVAR a IA para responder mensagens?'
                : 'Deseja ATIVAR a IA para responder mensagens?';
              if (confirm(msg) && onToggleAutoReply) {
                onToggleAutoReply(!autoReply);
              }
            }}
            class="flex items-center gap-[5px] rounded-full px-[10px] py-[4px] text-[11px] font-semibold cursor-pointer transition-colors ${autoReply ? 'bg-green-500/25 text-green-300 hover:bg-green-500/35' : 'bg-red-500/25 text-red-300 hover:bg-red-500/35'}"
            title=${autoReply ? 'IA ativada globalmente — clique para desativar' : 'IA desativada globalmente — clique para ativar'}
          >
            <span class="inline-block w-[6px] h-[6px] rounded-full ${autoReply ? 'bg-green-400' : 'bg-red-400'}"></span>
            ${autoReply ? 'IA Ativada' : 'IA Desativada'}
          </button>
        </div>
        <div class="flex items-center gap-2">
          ${wsConnected === false ? html`
            <span class="text-white/80 text-[13px] animate-pulse">Sem conexão</span>
            <span class="inline-block w-2 h-2 rounded-full bg-red-400 animate-pulse" title="Offline"></span>
          ` : html`
            <span class="text-white text-[15px] font-medium opacity-90">${showArchived ? 'Arquivados' : 'Whatsbot-lite'}</span>
            <span class="inline-block w-2 h-2 rounded-full bg-green-400" title="Online"></span>
          `}
          <div ref=${menuRef} class="relative">
            <button
              onClick=${() => setHeaderMenuOpen(o => !o)}
              class="w-[34px] h-[34px] rounded-full flex items-center justify-center text-white hover:bg-white/10 transition-colors"
              title="Mais opções"
            ><${KebabIcon} /></button>
            ${headerMenuOpen ? html`
              <div class="absolute right-0 top-[42px] z-[60] bg-wa-panel rounded-lg shadow-lg border border-wa-border py-[4px] min-w-[210px]">
                <button
                  onClick=${() => { closeMenus(); onEnterSelection && onEnterSelection(); }}
                  class="w-full text-left px-4 py-[10px] text-[14px] text-wa-text hover:bg-wa-hover transition-colors flex items-center gap-3"
                >
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-9 14l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                  Selecionar conversas
                </button>
              </div>
            ` : ''}
          </div>
        </div>
      </div>
      `}

      <!-- Search bar -->
      <div class="py-[6px] px-[12px] bg-wa-bg border-b border-wa-border">
        <div class="flex items-center bg-wa-panel rounded-lg h-[35px] px-[8px] gap-[20px]">
          <${SearchIcon} />
          <input
            type="text"
            placeholder="Pesquisar ou começar uma nova conversa"
            value=${search}
            onInput=${(e) => onSearchChange(e.target.value)}
            class="bg-transparent border-none outline-none text-wa-text text-[14px] w-full placeholder-wa-secondary"
          />
        </div>
      </div>

      <!-- Contact rows -->
      <div class="flex-1 overflow-y-auto wa-scrollbar bg-wa-bg">
        ${loading && contacts.length === 0
          ? html`<div class="text-center text-wa-secondary py-8 animate-pulse-slow text-[14px]">Carregando...</div>`
          : contacts.length === 0
            ? html`<div class="text-center py-8 px-4">
                <div class="text-wa-secondary text-[14px]">Nenhum contato encontrado</div>
                ${search && looksLikePhone(search) ? html`
                  <div class="mt-4">
                    ${checkingPhone
                      ? html`<div class="text-wa-secondary text-[13px] animate-pulse-slow">
                          Verificando se o número possui WhatsApp...
                        </div>`
                      : checkPhoneError
                        ? html`<div class="text-red-400 text-[13px] mb-2">${checkPhoneError}</div>
                               <button
                                 onClick=${() => onStartConversation(normalizePhone(search))}
                                 class="text-wa-teal text-[13px] hover:underline cursor-pointer"
                               >Tentar novamente</button>`
                        : html`<button
                            onClick=${() => onStartConversation(normalizePhone(search))}
                            class="mt-2 px-4 py-[6px] bg-wa-teal/10 text-wa-teal text-[13px] rounded-lg hover:bg-wa-teal/20 transition-colors cursor-pointer border border-wa-teal/30"
                          >
                            Iniciar conversa com ${formatPhoneDisplay(normalizePhone(search))}
                          </button>`
                    }
                  </div>
                ` : null}
              </div>`
            : contacts.map(c => html`
                <div
                  key=${c.phone}
                  onClick=${() => selectionMode ? onToggleSelect(c.phone) : onSelect(c.phone, c.match_msg_id)}
                  onContextMenu=${(e) => { if (selectionMode) return; e.preventDefault(); onContextMenu && onContextMenu({ x: e.clientX, y: e.clientY, phone: c.phone, aiEnabled: c.ai_enabled !== false, tags: c.tags || [], isArchived: !!c.is_archived, isUnread: (c.unread_count > 0 || c.unread_ai_count > 0), isPinned: !!c.is_pinned }); }}
                  class="wa-contact-row flex items-center pl-[13px] pr-[15px] cursor-pointer ${
                    (selectionMode && selectedSet.has(c.phone)) ? 'bg-wa-selected'
                      : (!selectionMode && selected === c.phone) ? 'bg-wa-selected' : 'hover:bg-wa-hover'
                  }"
                >
                  ${selectionMode ? html`
                    <div class="shrink-0 mr-[10px] flex items-center justify-center">
                      <span class="w-[22px] h-[22px] rounded-full border-2 flex items-center justify-center transition-colors ${selectedSet.has(c.phone) ? 'bg-wa-teal border-wa-teal' : 'border-wa-secondary'}">
                        ${selectedSet.has(c.phone) ? html`
                          <svg viewBox="0 0 24 24" width="14" height="14" fill="white"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
                        ` : ''}
                      </span>
                    </div>
                  ` : ''}
                  <!-- Avatar -->
                  <div class="w-[49px] h-[49px] rounded-full overflow-hidden shrink-0 mr-[13px]">
                    ${c.is_group
                      ? html`<${GroupAvatar} size=${49} avatarUrl=${avatarUrl(c.phone, c.avatar_v)} />`
                      : html`<${DefaultAvatar} size=${49} avatarUrl=${avatarUrl(c.phone, c.avatar_v)} />`
                    }
                  </div>

                  <!-- Text content with bottom border -->
                  <div class="flex-1 min-w-0 border-b border-wa-border py-[13px]">
                    <div class="flex justify-between items-baseline">
                      <span class="text-wa-text text-[17px] truncate leading-[21px]">
                        ${c.is_group ? (c.group_name || c.name || c.phone) : ((c.name || '').replace(/^~/, '') || c.phone)}
                        ${!c.is_group && c.name && c.name.startsWith('~')
                          ? html`<span class="ml-[6px] text-[10px] font-semibold text-blue-400 bg-blue-500/15 rounded px-[5px] py-[1px] align-middle" title="Nome obtido do WhatsApp">WA</span>`
                          : null
                        }
                        ${c.archived_by_app
                          ? html`<span class="ml-[6px] text-[10px] font-semibold text-amber-400 bg-amber-500/15 rounded px-[5px] py-[1px] align-middle" title="Arquivado pela aplicação">APP</span>`
                          : null
                        }
                        ${c.ai_enabled === false
                          ? html`<span class="ml-[6px] text-[10px] font-semibold text-red-400 bg-red-500/15 rounded px-[5px] py-[1px] align-middle">IA OFF</span>`
                          : html`<span class="ml-[6px] text-[10px] font-semibold text-green-400 bg-green-500/15 rounded px-[5px] py-[1px] align-middle">IA</span>`
                        }
                      </span>
                      <span class="flex items-center gap-[4px] ml-[6px] shrink-0">
                        ${c.is_pinned ? html`<span class="text-wa-secondary" title="Conversa fixada"><${PinIcon} /></span>` : ''}
                        <span class="text-wa-secondary text-[12px] leading-[14px]">${formatTime(c.last_message_ts)}</span>
                      </span>
                    </div>
                    ${(c.tags && c.tags.length > 0) ? html`
                      <div class="flex items-center gap-[3px] mt-[2px] flex-wrap">
                        ${c.tags.slice(0, 3).map(tagName => {
                          const tagInfo = globalTags && globalTags[tagName];
                          const color = tagInfo ? tagInfo.color : '#6b7280';
                          return html`<span
                            class="text-[9px] font-semibold rounded px-[4px] py-[0.5px] max-w-[70px] truncate leading-[14px]"
                            style="background: ${color}20; color: ${color}; border: 1px solid ${color}40;"
                            title=${tagName}
                          >${tagName}</span>`;
                        })}
                        ${c.tags.length > 3 ? html`<span class="text-[9px] text-wa-secondary">+${c.tags.length - 3}</span>` : null}
                      </div>
                    ` : null}
                    <div class="flex justify-between items-center mt-[3px]">
                      ${typingState && typingState[c.phone]
                        ? html`<span class="text-[14px] truncate leading-[20px] text-wa-teal font-medium">
                            ${typingState[c.phone] === 'audio' ? 'gravando áudio...' : 'digitando...'}
                          </span>`
                        : c.match_snippet
                          ? html`<span class="text-wa-secondary text-[14px] truncate leading-[20px]">
                              ${highlightParts(c.match_snippet, search).map(p =>
                                p.hit ? html`<span class="font-semibold text-wa-text">${p.s}</span>` : p.s
                              )}
                            </span>`
                          : html`<span class="text-wa-secondary text-[14px] truncate leading-[20px]">
                            ${c.last_message_role === 'assistant' ? (() => {
                              const st = c.last_message_status;
                              if (st === 'sent') return html`<${SingleCheckIcon} />`;
                              if (st === 'delivered' || st === 'operator') return html`<${DoubleCheckIcon} color="#92a58c" />`;
                              if (st === 'read') return html`<${DoubleCheckIcon} />`;
                              return html`<${DoubleCheckIcon} color="#92a58c" />`;
                            })() : ''}${c.last_message ? c.last_message.substring(0, 80) : ''}
                          </span>`
                      }
                      ${(c.unread_ai_count > 0 || c.unread_count > 0 || c.has_unread_mention) ? html`
                        <div class="flex items-center gap-[4px] ml-auto pl-[6px] shrink-0">
                          ${c.has_unread_mention ? html`
                            <span class="text-wa-badge font-bold text-[17px] leading-none" title="Você foi mencionado">@</span>
                          ` : null}
                          ${c.unread_ai_count > 0 ? html`
                            <span class="bg-blue-500 text-white text-[11px] font-bold min-w-[20px] h-[20px] rounded-full flex items-center justify-center px-[3px]">
                              ${c.unread_ai_count}
                            </span>
                          ` : null}
                          ${c.unread_count > 0 ? html`
                            <span class="bg-wa-badge text-white text-[11px] font-bold min-w-[20px] h-[20px] rounded-full flex items-center justify-center px-[3px]">
                              ${c.unread_count}
                            </span>
                          ` : null}
                        </div>
                      ` : null}
                    </div>
                  </div>
                </div>
              `)
        }
      </div>
    </div>
  `;
}
