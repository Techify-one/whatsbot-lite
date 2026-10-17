// "Conversa" filter of the Arquivos screen: as the person types a name or a
// number, a list shows who each match is (photo, name and number), so two
// clients with the same name are never mixed up. Picking one filters by that
// exact conversation; text that is not picked still works as a plain search.

import { h } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';
import { getFileConversations } from '../services/api.js';
import { DefaultAvatar, GroupAvatar } from './contacts/icons.js';
import { avatarUrl } from './contacts/utils.js';
import { formatPhone } from './QRCode.js';

const html = htm.bind(h);

const LIST_ID = 'files-conversation-list';
const optionId = (i) => `files-conversation-opt-${i}`;

function Avatar({ c, size }) {
  const url = avatarUrl(c.phone, c.avatar_v);
  return html`
    <div class="rounded-full overflow-hidden shrink-0" style=${`width:${size}px;height:${size}px`}>
      ${c.is_group
        ? html`<${GroupAvatar} size=${size} avatarUrl=${url} />`
        : html`<${DefaultAvatar} size=${size} avatarUrl=${url} />`}
    </div>`;
}

// A group's id is an internal code nobody types, so only people show a number.
function secondLine(c) {
  return c.is_group ? 'Grupo' : formatPhone(c.phone);
}

function Identity({ c, nameClass }) {
  return html`
    <div class="min-w-0 flex-1">
      <div class="${nameClass} text-wa-text truncate">${c.name || secondLine(c)}</div>
      ${c.name ? html`<div class="text-[12px] text-wa-secondary truncate">${secondLine(c)}</div>` : null}
    </div>`;
}

export function ConversationPicker({ text, picked, onText, onPick, onClear }) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState([]);
  const [active, setActive] = useState(-1);
  const [loading, setLoading] = useState(false);
  const reqRef = useRef(0);
  const term = text.trim();

  // Looks up the matches once typing pauses; a slow answer to an older text is dropped.
  useEffect(() => {
    const req = ++reqRef.current;
    if (!term || picked) {
      setOptions([]);
      setLoading(false);
      return undefined;
    }
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await getFileConversations(term);
        if (req !== reqRef.current) return;
        setOptions(res && res.ok ? res.data : []);
        setActive(-1);
      } catch {
        if (req === reqRef.current) setOptions([]);
      } finally {
        if (req === reqRef.current) setLoading(false);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [term, picked]);

  function choose(c) {
    setOpen(false);
    setActive(-1);
    onPick(c);
  }

  function onKeyDown(e) {
    if (e.key === 'Escape') { setOpen(false); return; }
    if (!options.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      setOpen(true);
      const step = e.key === 'ArrowDown' ? 1 : -1;
      setActive((a) => (a < 0 ? (step > 0 ? 0 : options.length - 1) : (a + step + options.length) % options.length));
    } else if (e.key === 'Enter' && open && active >= 0) {
      e.preventDefault();
      choose(options[active]);
    }
  }

  const showList = open && !picked && !!term;
  const empty = showList && !loading && options.length === 0;

  return html`
    <div class="relative flex flex-col gap-1 text-[12px] text-wa-secondary">
      <label for="files-conversation" id="files-conversation-label">Conversa (nome, número ou grupo)</label>
      ${picked ? html`
        <div role="group" aria-labelledby="files-conversation-label"
          class="wa-field rounded-lg px-3 py-1 min-h-[44px] flex items-center gap-2.5 ring-2 ring-wa-teal">
          <${Avatar} c=${picked} size=${32} />
          <${Identity} c=${picked} nameClass="text-[14px]" />
          <button type="button" onClick=${onClear} aria-label="Remover o filtro de conversa" title="Trocar de conversa"
            class="shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-[18px] leading-none text-wa-secondary hover:bg-wa-hover hover:text-wa-text">×</button>
        </div>` : html`
        <input id="files-conversation" type="text" autocomplete="off" value=${text}
          role="combobox" aria-expanded=${showList} aria-controls=${LIST_ID} aria-autocomplete="list"
          aria-activedescendant=${showList && active >= 0 ? optionId(active) : undefined}
          onInput=${(e) => { onText(e.target.value); setOpen(true); }}
          onFocus=${() => setOpen(true)}
          onBlur=${() => setOpen(false)}
          onKeyDown=${onKeyDown}
          placeholder="Ex.: Maria, 11 99999-0001, Grupo da obra"
          class="wa-field rounded-lg px-3 py-2 text-[14px] outline-none focus:ring-2 focus:ring-wa-teal" />`}
      ${showList && options.length ? html`
        <ul id=${LIST_ID} role="listbox" aria-label="Conversas encontradas"
          class="absolute left-0 right-0 top-full mt-1 max-h-[320px] overflow-y-auto bg-wa-panel border border-wa-border rounded-lg shadow-lg py-1 z-30 wa-scrollbar">
          ${options.map((c, i) => html`
            <li key=${c.id} id=${optionId(i)} role="option" aria-selected=${i === active}
              onMouseDown=${(e) => { e.preventDefault(); choose(c); }}
              onMouseEnter=${() => setActive(i)}
              class="px-3 py-2 flex items-center gap-3 cursor-pointer ${i === active ? 'bg-wa-hover' : ''}">
              <${Avatar} c=${c} size=${40} />
              <${Identity} c=${c} nameClass="text-[14px]" />
            </li>`)}
        </ul>` : null}
      ${empty ? html`
        <div role="status" class="absolute left-0 right-0 top-full mt-1 bg-wa-panel border border-wa-border rounded-lg shadow-lg px-3 py-2.5 z-30 text-[13px] text-wa-secondary">
          Nenhuma conversa encontrada.
        </div>` : null}
    </div>`;
}
