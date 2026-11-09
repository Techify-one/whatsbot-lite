/* Collapsed system cards + media caption. Run: node --test tests/messageView.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

const modulePath = pathToFileURL(
    path.join(__dirname, '../web/static/js/services/messageView.js')).href;
const load = () => import(modulePath);

test('only transcription and tool_call are collapsible', async () => {
    const {isCollapsibleRole} = await load();
    assert.equal(isCollapsibleRole('transcription'), true);
    assert.equal(isCollapsibleRole('tool_call'), true);
    assert.equal(isCollapsibleRole('private_note'), false);
    assert.equal(isCollapsibleRole('user'), false);
});

test('collapsedPreview collapses whitespace and cuts at a word boundary', async () => {
    const {collapsedPreview} = await load();
    assert.equal(collapsedPreview('transcription', '  oi   tudo\n bem '), 'oi tudo bem');
    const long = 'palavra '.repeat(30);
    const out = collapsedPreview('transcription', long, {maxLen: 20});
    assert.ok(out.endsWith('…'));
    assert.ok(out.length <= 21);
    assert.ok(!out.slice(0, -1).endsWith(' '));
    assert.equal(collapsedPreview('transcription', null), '');
});

test('tool_call preview is the first non-empty line', async () => {
    const {collapsedPreview} = await load();
    assert.equal(collapsedPreview('tool_call', '\n\n🔧 save_contact_info\n{"a":1}'), '🔧 save_contact_info');
});

test('cardStateKey follows the message, not the index', async () => {
    const {cardStateKey} = await load();
    assert.equal(cardStateKey({_id: 5, msg_id: 'A', role: 'transcription', ts: 1}, 0), 'id:5');
    assert.equal(cardStateKey({msg_id: 'A', role: 'transcription', ts: 1}, 0), 'mid:A');
    assert.equal(cardStateKey({role: 'transcription', ts: 12.5}, 3), 'rt:transcription:12.5');
    assert.equal(cardStateKey({role: 'transcription'}, 3), 'ix:3');
});

test('mediaCaptionOf never returns text the AI wrote', async () => {
    const {mediaCaptionOf} = await load();
    assert.equal(mediaCaptionOf('[Descrição da imagem]: Aqui está uma descrição\nlonga'), '');
    assert.equal(mediaCaptionOf('[Transcrição do áudio]: olá, tudo bem?'), '');
    assert.equal(mediaCaptionOf('[Conteúdo do documento]: texto do pdf'), '');
});

test('mediaCaptionOf keeps the customer caption and drops placeholders', async () => {
    const {mediaCaptionOf} = await load();
    assert.equal(mediaCaptionOf('[Imagem recebida]\nolha isso'), 'olha isso');
    assert.equal(mediaCaptionOf('[Imagem enviada pelo contato]'), '');
    assert.equal(mediaCaptionOf('[Áudio recebido]'), '');
    assert.equal(mediaCaptionOf('[Áudio]'), '');
    assert.equal(mediaCaptionOf('minha legenda'), 'minha legenda');
    assert.equal(mediaCaptionOf('contrato\n[Conteúdo do documento]: clausula 1'), 'contrato');
    assert.equal(mediaCaptionOf(''), '');
    assert.equal(mediaCaptionOf(null), '');
});

test('isAiContentLabel recognises the AI labels only', async () => {
    const {isAiContentLabel} = await load();
    assert.equal(isAiContentLabel('Descrição da imagem'), true);
    assert.equal(isAiContentLabel('descrição da imagem'), true);
    assert.equal(isAiContentLabel('Maria Silva'), false);
});
