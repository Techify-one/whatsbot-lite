import { h } from 'preact';
import htm from 'htm';
import { formatBubbleTime } from './utils.js';
import { CARD_VARIANTS, collapsedPreview } from '../../services/messageView.js';

const html = htm.bind(h);

const LOCK_PATH = 'M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 1.39 3.1 3.1v2z';
const TOOL_PATH = 'M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6 6 9 1.6 4.7C.4 7.1.9 10.1 2.9 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.5-.4.5-1.1.1-1.4z';
const iconPath = (icon) => (icon === 'tool' ? TOOL_PATH : LOCK_PATH);

// Centered, panel-only cards for `transcription` and `tool_call` messages.
// Minimized to a one-line chip by default (like whatsbot-pro); tap to expand,
// tap the header to minimize again. The component is CONTROLLED: the container
// owns which cards are expanded (see cardStateKey), so state follows the message
// when older history is prepended.
export function SystemMessageCard({ message: m, fmt, collapsed = true, onToggleCollapse = null }) {
  const variant = CARD_VARIANTS[m.role];
  if (!variant) return null;

  const onKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (onToggleCollapse) onToggleCollapse();
    }
  };

  if (collapsed) {
    const preview = collapsedPreview(m.role, m.content);
    return html`
      <div data-mid=${m._id} class="wa-card flex justify-center mt-[4px]">
        <div
          role="button" tabIndex="0" aria-expanded="false"
          title=${m.content || ''}
          onClick=${onToggleCollapse}
          onKeyDown=${onKeyDown}
          class="wa-card-body max-w-[75%] rounded-[7.5px] px-[10px] py-[4px] text-[12px] leading-[16px] cursor-pointer flex items-center gap-1 min-w-0"
          style=${variant.style}>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" class="shrink-0 opacity-80"><path d=${iconPath(variant.icon)}></path></svg>
          <span class="truncate min-w-0">
            <span class="font-semibold opacity-90">${variant.label}</span>${preview ? html`<span class="opacity-50" aria-hidden="true"> · </span><span class="opacity-80">${preview}</span>` : ''}
          </span>
          <span class="shrink-0 opacity-70 ml-[2px]" aria-hidden="true">▸</span>
        </div>
      </div>
    `;
  }

  return html`
    <div data-mid=${m._id} class="wa-card flex justify-center mt-[4px]">
      <div class="wa-card-body max-w-[75%] rounded-[7.5px] px-[10px] pt-[5px] pb-[6px] text-[12.5px] leading-[17px] whitespace-pre-wrap relative"
           style=${variant.style}>
        <span class="flex items-center gap-1 text-[10px] font-semibold mb-[2px] opacity-80 cursor-pointer"
              role="button" tabIndex="0" aria-expanded="true"
              onClick=${onToggleCollapse}
              onKeyDown=${onKeyDown}>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d=${iconPath(variant.icon)}></path></svg>
          ${variant.label}
          <span class="ml-auto pl-2 opacity-70" aria-hidden="true">▾</span>
        </span>
        <span dangerouslySetInnerHTML=${{ __html: fmt(m.content) }}></span>
        <span class="float-right ml-[8px] mt-[2px] text-[10px] leading-[14px] whitespace-nowrap opacity-60">
          ${formatBubbleTime(m.ts)}
        </span>
      </div>
    </div>
  `;
}
