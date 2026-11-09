/* Contact list reacting to server events. Run: node --test tests/contactListEvents.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '../web/static/js/services/contactListEvents.js')).href);

const list = () => [
    {phone: 'A', is_pinned: 0, last_message_ts: 30},
    {phone: 'B', is_pinned: 0, last_message_ts: 20},
    {phone: 'C', is_pinned: 0, last_message_ts: 10},
];
const view = (o = {}) => ({showArchived: false, selectedPhone: null, ...o});

test('deleted: leaves the list; closes it only if it was the open one', async () => {
    const {applyContactListEvent: ap} = await load();
    let r = ap(list(), {type: 'deleted', phone: 'B'}, view());
    assert.deepEqual(r.contacts.map(c => c.phone), ['A', 'C']);
    assert.equal(r.deselect, false);
    r = ap(list(), {type: 'deleted', phone: 'B'}, view({selectedPhone: 'B'}));
    assert.equal(r.deselect, true);
    r = ap(list(), {type: 'deleted', phone: 'B'}, view({selectedPhone: 'A'}));
    assert.equal(r.deselect, false);
});

test('deleted echo of our own delete is a no-op (already gone)', async () => {
    const {applyContactListEvent: ap} = await load();
    const gone = list().filter(c => c.phone !== 'B');
    const r = ap(gone, {type: 'deleted', phone: 'B'}, view());
    assert.deepEqual(r.contacts.map(c => c.phone), ['A', 'C']);
    assert.equal(r.deselect, false);
    assert.equal(r.refetch, false);
});

test('archived while viewing the inbox: it leaves the list', async () => {
    const {applyContactListEvent: ap} = await load();
    const r = ap(list(), {type: 'archived', phone: 'C', archived: true}, view());
    assert.deepEqual(r.contacts.map(c => c.phone), ['A', 'B']);
    assert.equal(r.refetch, false);
});

test('unarchived while viewing the archived list: it leaves that list', async () => {
    const {applyContactListEvent: ap} = await load();
    const r = ap(list(), {type: 'archived', phone: 'C', archived: false}, view({showArchived: true}));
    assert.deepEqual(r.contacts.map(c => c.phone), ['A', 'B']);
});

test('archived into the view being shown: ask the server to place it', async () => {
    const {applyContactListEvent: ap} = await load();
    const r = ap(list(), {type: 'archived', phone: 'Z', archived: true}, view({showArchived: true}));
    assert.equal(r.refetch, true);
    assert.deepEqual(r.contacts.map(c => c.phone), ['A', 'B', 'C']);
});

test('archiving the open conversation closes it', async () => {
    const {applyContactListEvent: ap} = await load();
    const r = ap(list(), {type: 'archived', phone: 'B', archived: true}, view({selectedPhone: 'B'}));
    assert.equal(r.deselect, true);
});

test('pinned moves the contact to the top; unpinned puts it back by recency', async () => {
    const {applyContactListEvent: ap} = await load();
    let r = ap(list(), {type: 'pinned', phone: 'C', pinned: true}, view());
    assert.deepEqual(r.contacts.map(c => c.phone), ['C', 'A', 'B']);
    r = ap(r.contacts, {type: 'pinned', phone: 'C', pinned: false}, view());
    assert.deepEqual(r.contacts.map(c => c.phone), ['A', 'B', 'C']);
});

test('pinned echo with nothing to change returns the SAME array (no re-render)', async () => {
    const {applyContactListEvent: ap} = await load();
    const l = list();
    const r = ap(l, {type: 'pinned', phone: 'A', pinned: false}, view());
    assert.equal(r.contacts, l);
});

test('unknown event or missing phone changes nothing', async () => {
    const {applyContactListEvent: ap} = await load();
    const l = list();
    assert.equal(ap(l, {type: 'wat', phone: 'A'}, view()).contacts, l);
    assert.equal(ap(l, {type: 'deleted'}, view()).contacts, l);
    assert.equal(ap(l, null, view()).contacts, l);
});
