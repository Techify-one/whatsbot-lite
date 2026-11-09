// Pure helpers to decide whether two chat messages are the same one.
// No DOM, no Preact: tested with `node --test tests/messages.test.cjs`.

/** Window (seconds) within which a same-role, same-content message is a dup. */
export const DEDUP_WINDOW_S = 30;

/**
 * Whether `a` and `b` are the "same" message (optimistic send / WS replay / buffer merge).
 *
 * Stable identity decides FIRST: when both sides carry a `msg_id` (GOWA id) that
 * field is authoritative — equal means the same message, different means two real
 * rows. Same for the DB row id `_id`. Without this, two images sent in a row with
 * no caption (both `content === ''`) collapsed into a single bubble until reload.
 *
 * Only when neither side settles identity do we fall back to the legacy heuristic:
 * same role AND either the exact same `ts` or identical content within
 * {@link DEDUP_WINDOW_S} seconds.
 */
export function sameMessage(a, b) {
  if (!a || !b) return false;
  if (a.role !== b.role) return false;
  if (a.msg_id && b.msg_id) return a.msg_id === b.msg_id;
  if (a._id != null && b._id != null) return a._id === b._id;
  if (a.ts === b.ts) return true;
  return a.content === b.content
    && Math.abs((a.ts || 0) - (b.ts || 0)) < DEDUP_WINDOW_S;
}

/**
 * Index of the message in `list` that `message` duplicates, or -1. Entries that
 * already have a `msg_id` are only matched by that id (see {@link sameMessage}).
 */
export function findDuplicateIndex(message, list) {
  if (!Array.isArray(list)) return -1;
  return list.findIndex(m => sameMessage(m, message));
}

/** Whether `message` already exists in `list`. */
export function isDuplicateMessage(message, list) {
  return findDuplicateIndex(message, list) !== -1;
}
