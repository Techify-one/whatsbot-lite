/* Decisions of the automatic GOWA modals. Run: node --test tests/gowa-prompt.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

const modulePath = pathToFileURL(
    path.join(__dirname, '../web/static/js/utils/gowaPrompt.js')).href;
const load = () => import(modulePath);

// Payload of GET /api/gowa/version on a machine that just self-updated.
const versionPayload = (extra = {}) => ({
    installed_version: '8.8.0',
    installed_version_known: true,
    recommended_version: '9.6.0',
    recommended_update_available: true,
    recommended_supported: true,
    recommended_skipped: false,
    recommended_release_url: 'https://example.invalid/tag/v9.6.0',
    platform_supported: true,
    busy: false,
    ...extra,
});

test('recommended newer than installed opens the modal with the recommended version', async () => {
    const {recommendedPrompt, MODE_RECOMMENDED} = await load();
    const prompt = recommendedPrompt(versionPayload());
    assert.equal(prompt.mode, MODE_RECOMMENDED);
    assert.equal(prompt.latest_version, '9.6.0'); // never GitHub's latest
    assert.equal(prompt.installed_version, '8.8.0');
    assert.equal(prompt.installed_known, true);
});

test('unknown installed version still opens it, flagged as unknown', async () => {
    const {recommendedPrompt} = await load();
    const prompt = recommendedPrompt(versionPayload({
        installed_version: '', installed_version_known: false,
    }));
    assert.ok(prompt);
    assert.equal(prompt.installed_known, false);
    assert.equal(prompt.installed_version, '');
});

test('nothing to ask in each of the quiet cases', async () => {
    const {recommendedPrompt} = await load();
    const quiet = {
        'already postponed': {recommended_skipped: true},
        'not newer': {recommended_update_available: false},
        'out of the approved range': {recommended_supported: false},
        'no binary for this platform': {platform_supported: false},
        'an update is already running': {busy: true},
        'no recommended version': {recommended_version: ''},
    };
    for (const [label, extra] of Object.entries(quiet)) {
        assert.equal(recommendedPrompt(versionPayload(extra)), null, label);
    }
    assert.equal(recommendedPrompt(null), null);
    assert.equal(recommendedPrompt(undefined), null);
});

test('analyzer prompt keeps its old gating and tags its mode', async () => {
    const {compatPrompt, MODE_COMPAT} = await load();
    const base = {
        update_available: true, latest_supported: true, whatsapp_update_recommended: true,
        latest_version: '9.8.0', skipped_version: '', installed_version: '8.8.0',
        release_url: 'u', update_reason: 'r', update_evidence: [], assessed_versions: [],
    };
    const prompt = compatPrompt(base);
    assert.equal(prompt.mode, MODE_COMPAT);
    assert.equal(prompt.latest_version, '9.8.0');
    assert.equal(compatPrompt({...base, update_available: false}), null);
    assert.equal(compatPrompt({...base, latest_supported: false}), null);
    // Does NOT depend on the new fields, and the old gate on the analyzer stays.
    assert.equal(compatPrompt({...base, whatsapp_update_recommended: false}), null);
    assert.equal(compatPrompt({...base, skipped_version: '9.8.0'}), null);
});

test('analyzer stays quiet for the very version the person just postponed', async () => {
    const {compatPrompt, postponedRecommendation} = await load();
    const payload = {
        update_available: true, latest_supported: true, whatsapp_update_recommended: true,
        latest_version: '9.6.0', skipped_version: '', installed_version: '8.8.0',
        recommended_version: '9.6.0', recommended_skipped: true,
    };
    assert.equal(compatPrompt(payload), null);
    // A different upstream version is still worth asking about.
    assert.ok(compatPrompt({...payload, latest_version: '9.8.0'}));
    assert.equal(postponedRecommendation('9.6.0', {version: '9.6.0', skipped: true}), true);
    assert.equal(postponedRecommendation('9.6.0', {version: '9.7.0', skipped: true}), false);
    assert.equal(postponedRecommendation('9.6.0', {version: '9.6.0', skipped: false}), false);
    assert.equal(postponedRecommendation('', {version: '', skipped: true}), false);
    assert.equal(postponedRecommendation('9.6.0', null), false);
});

test('one modal at a time: a different kind never replaces the open one', async () => {
    const {nextPrompt, MODE_RECOMMENDED, MODE_COMPAT} = await load();
    const rec = {mode: MODE_RECOMMENDED, latest_version: '9.6.0'};
    const compat = {mode: MODE_COMPAT, latest_version: '9.8.0'};
    assert.equal(nextPrompt(null, rec), rec, 'empty takes the first one');
    assert.equal(nextPrompt(null, compat), compat);
    assert.equal(nextPrompt(rec, compat), rec, 'analyzer cannot cover the recommended modal');
    assert.equal(nextPrompt(compat, rec), compat, 'recommended cannot cover the analyzer modal');
    assert.equal(nextPrompt(rec, null), rec, 'no incoming keeps the current one');
    assert.equal(nextPrompt(null, null), null);
    const refreshed = {mode: MODE_RECOMMENDED, latest_version: '9.7.0'};
    assert.equal(nextPrompt(rec, refreshed), refreshed, 'same kind refreshes its data');
});
