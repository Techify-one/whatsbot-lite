// Pure view helpers for chat messages (no DOM, no Preact).
// Tested with: node --test tests/messageView.test.cjs

/** Roles rendered as centered "system cards" that can be minimized to a chip. */
export const COLLAPSIBLE_ROLES = ['transcription', 'tool_call'];

export const CARD_VARIANTS = {
  transcription: {
    label: 'Transcrição privada', icon: 'lock',
    style: 'background: #2d1b4e; color: #d4bfff; border: 1px solid #4a2d7a;',
  },
  tool_call: {
    label: 'Ferramenta IA', icon: 'tool',
    style: 'background: #2d1b0e; color: #fbbf24; border: 1px solid #78350f;',
  },
};

/** Whether a message with this role renders as a card that may be minimized. */
export function isCollapsibleRole(role) {
  return COLLAPSIBLE_ROLES.includes(role);
}

/**
 * One-line plain-text preview for a minimized card. Receives the RAW content
 * (never formatted HTML — truncating HTML would cut a tag in half) and is
 * rendered by htm interpolation, which escapes it.
 * `tool_call` uses its first non-empty line ("🔧 <tool_name>").
 */
export function collapsedPreview(role, content, { maxLen = 70 } = {}) {
  if (content == null) return '';
  const str = String(content);
  const base = role === 'tool_call'
    ? (str.split('\n').find(line => line.trim() !== '') || '').trim()
    : str.replace(/\s+/g, ' ').trim();
  if (!base) return '';
  if (base.length <= maxLen) return base;
  const slice = base.slice(0, maxLen);
  const lastSpace = slice.lastIndexOf(' ');
  return (lastSpace > 0 ? slice.slice(0, lastSpace) : slice) + '…';
}

/**
 * Stable key for the expanded-cards Set. The thread is keyed by index and older
 * history is prepended, so state must follow the message, not the position.
 * Precedence: `_id` → `msg_id` → `role:ts` → index (degraded fallback).
 */
export function cardStateKey(m, index) {
  if (m) {
    if (m._id != null) return `id:${m._id}`;
    if (m.msg_id != null && m.msg_id !== '') return `mid:${m.msg_id}`;
    if (m.ts != null) return `rt:${m.role}:${m.ts}`;
  }
  return `ix:${index}`;
}

// ── Text the AI writes into a media message's `content` ─────────────────────
// For the LLM's benefit the webhook rewrites the media row's content with
// "[Descrição da imagem]: …", "[Transcrição do áudio]: …" or
// "[Conteúdo do documento]: …". That text is NOT what the customer typed: the
// operator already sees it in the private "Transcrição privada" card, so the
// bubble must never show it as if it were the customer's own message.

const AI_LABELS = [
  'Descrição da imagem',
  'Transcrição do áudio',
  'Conteúdo do documento',
  'Descrição do vídeo',
];

/** Whether `label` (the text inside the first […]) is one the AI writes. */
export function isAiContentLabel(label) {
  const norm = String(label || '').trim().toLowerCase();
  return AI_LABELS.some(l => l.toLowerCase() === norm);
}

const PLACEHOLDER_RE = /^\[(?:Imagem|Áudio|Vídeo|Documento|Sticker)[^\]]*\]\s*/i;

/**
 * The caption the customer actually wrote for a media message ('' when none),
 * with AI-written blocks and the "[Imagem recebida]"-style placeholder removed.
 *
 * Handled shapes (a legacy row has no dedicated caption column):
 *  - "[Descrição da imagem]: desc"            → ''
 *  - "[Descrição da imagem]: desc\nlegenda"   → 'legenda' is not recoverable
 *    reliably (the description is multi-line), so → ''
 *  - "[Transcrição do áudio]: texto"          → ''
 *  - "texto\n[Conteúdo do documento]: texto"  → 'texto' (caption stays in front)
 *  - "[Imagem recebida]\nlegenda"             → 'legenda'
 */
export function mediaCaptionOf(content) {
  if (content == null) return '';
  let text = String(content);

  const lead = text.match(/^\[([^\]]+)\]:/);
  if (lead && isAiContentLabel(lead[1])) return '';

  const docAt = text.search(/\n?\[Conteúdo do documento\]:/i);
  if (docAt !== -1) text = text.slice(0, docAt);

  return text.replace(PLACEHOLDER_RE, '').trim();
}
