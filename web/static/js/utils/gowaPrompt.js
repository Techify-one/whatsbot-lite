// Decisions for the two automatic GOWA modals, kept free of DOM, network and
// Preact so tests/gowa-prompt.test.cjs can exercise them with plain node.
//
//  * "recommended": the WhatsBot-Lite release recommends a GOWA newer than the
//    one installed. Decided from local data only (GET /api/gowa/version).
//  * "compat": the release analyzer says a newer upstream GOWA fixes a
//    WhatsApp compatibility problem (GET /api/gowa/update/check, slow).
//
// Only one modal exists at a time. The two kinds never replace each other: a
// modal that is already open stays under the person's eyes, so whichever
// answers first (in practice the local, instant "recommended" one) wins and the
// other waits for the next boot / WebSocket reconnect.

export const MODE_RECOMMENDED = 'recommended';
export const MODE_COMPAT = 'compat';

/**
 * Modal data for the version recommended by this release, or null when there is
 * nothing to ask. `v` is the payload of GET /api/gowa/version.
 */
export function recommendedPrompt(v) {
  if (!v || !v.recommended_version) return null;
  if (v.recommended_update_available !== true) return null;
  // Out-of-range versions never open a proactive modal (they need the manual,
  // two-click path in the GOWA card).
  if (v.recommended_supported !== true) return null;
  if (v.recommended_skipped === true) return null;
  // No published binary for this OS/arch: the install would only fail.
  if (v.platform_supported === false) return null;
  // Do not stack a prompt on top of an update that is already running.
  if (v.busy === true) return null;
  return {
    mode: MODE_RECOMMENDED,
    latest_version: v.recommended_version,
    installed_version: v.installed_version || '',
    installed_known: v.installed_version_known !== false,
    supported: true,
    release_url: v.recommended_release_url || '',
  };
}

/**
 * True when the person already said "Agora não" to exactly this version through
 * the recommended modal. `rec` is `{version, skipped}`.
 */
export function postponedRecommendation(version, rec) {
  return !!(version && rec && rec.skipped === true && rec.version === version);
}

/**
 * Modal data for the analyzer's recommendation, or null. `d` = update/check
 * payload, which also carries the recommended_* fields (the route merges
 * GET /api/gowa/version into it).
 */
export function compatPrompt(d) {
  if (!d || !d.update_available || !d.latest_supported) return null;
  if (d.whatsapp_update_recommended !== true) return null;
  if (d.latest_version && d.latest_version === d.skipped_version) return null;
  // Same version the person just postponed: do not ask twice.
  if (postponedRecommendation(d.latest_version,
      { version: d.recommended_version, skipped: d.recommended_skipped })) return null;
  return {
    mode: MODE_COMPAT,
    latest_version: d.latest_version,
    installed_version: d.installed_version,
    installed_known: d.installed_version_known !== false,
    supported: d.latest_supported,
    release_url: d.release_url,
    update_risk_level: d.update_risk_level,
    update_reason: d.update_reason,
    update_evidence: d.update_evidence,
    assessed_versions: d.assessed_versions,
  };
}

/**
 * State to keep once `incoming` arrives while `current` may already be open.
 * Nothing open: take it. Same kind: refresh the data (as before this existed).
 * A different kind: keep what is open.
 */
export function nextPrompt(current, incoming) {
  if (!incoming) return current;
  if (!current) return incoming;
  if (current.mode !== incoming.mode) return current;
  return incoming;
}
