/* Dedupe of chat messages. Run: node --test tests/messages.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

const modulePath = pathToFileURL(
    path.join(__dirname, '../web/static/js/services/messages.js')).href;
const load = () => import(modulePath);

const img = (msg_id, ts, extra = {}) => ({
    role: 'user', content: '', ts, msg_id, media_type: 'image', ...extra,
});

test('two captionless images with different msg_id are two messages', async () => {
    const {sameMessage, findDuplicateIndex} = await load();
    const a = img('A', 1000.1);
    const b = img('B', 1000.9);
    assert.equal(sameMessage(a, b), false);
    assert.equal(findDuplicateIndex(b, [a]), -1);
});

test('the same msg_id is a duplicate even when ts and content differ', async () => {
    const {sameMessage} = await load();
    assert.equal(sameMessage(img('A', 1000), img('A', 5000, {content: 'x'})), true);
});

test('optimistic bubble without msg_id still collapses into its echo', async () => {
    const {findDuplicateIndex} = await load();
    const optimistic = {role: 'assistant', content: 'oi', ts: 100};
    const echo = {role: 'assistant', content: 'oi', ts: 103, msg_id: 'X'};
    assert.equal(findDuplicateIndex(echo, [optimistic]), 0);
});

test('different roles never match', async () => {
    const {sameMessage} = await load();
    assert.equal(sameMessage(
        {role: 'user', content: 'a', ts: 1, msg_id: 'A'},
        {role: 'transcription', content: 'a', ts: 1, msg_id: 'A'}), false);
});

test('db row id decides when msg_id is missing on one side', async () => {
    const {sameMessage} = await load();
    assert.equal(sameMessage(
        {role: 'user', content: '', ts: 1, _id: 7},
        {role: 'user', content: '', ts: 2, _id: 8}), false);
    assert.equal(sameMessage(
        {role: 'user', content: '', ts: 1, _id: 7},
        {role: 'user', content: '', ts: 2, _id: 7}), true);
});

test('legacy fallback: same content inside 30s is a dup, outside is not', async () => {
    const {sameMessage} = await load();
    const base = {role: 'user', content: 'ok'};
    assert.equal(sameMessage({...base, ts: 100}, {...base, ts: 120}), true);
    assert.equal(sameMessage({...base, ts: 100}, {...base, ts: 200}), false);
});
