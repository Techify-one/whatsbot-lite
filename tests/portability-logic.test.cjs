/* Lógica pura do Importar/Exportar. Rodar: node --test tests/portability-logic.test.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');

const modulePath = pathToFileURL(
    path.join(__dirname, '../web/static/js/utils/portabilityLogic.js')).href;
const load = () => import(modulePath);

const report = (extra = {}) => ({
    entities: {
        config: {create: 0, update: 2, skip: 30, error: 0},
        tags: {create: 3, update: 0, skip: 1, error: 0},
        contacts: {create: 10, update: 4, skip: 0, error: 2},
        messages: {create: 100, update: 0, skip: 5, error: 0},
    },
    default_strategy: {config: 'merge', tags: 'merge', contacts: 'merge', messages: 'skip'},
    strategy: {config: 'merge', tags: 'merge', contacts: 'merge', messages: 'skip'},
    supported_strategies: {
        config: ['skip', 'merge', 'overwrite'], tags: ['skip', 'merge', 'overwrite'],
        contacts: ['skip', 'merge', 'overwrite'], messages: ['skip']},
    manifest: {secrets: 'absent'},
    ...extra,
});

// ── export ──────────────────────────────────────────────────────────────────

test('export: sem escopo é erro e nada é montado', async () => {
    const {buildExportRequest} = await load();
    assert.ok(buildExportRequest({scopes: []}).error);
    assert.ok(buildExportRequest({scopes: null}).error);
});

test('export: a senha só entra no corpo quando há segredos', async () => {
    const {buildExportRequest} = await load();
    const plain = buildExportRequest({scopes: ['settings'], includeSecrets: false,
                                      password: 'uma-senha-bem-longa', confirm: 'uma-senha-bem-longa'});
    assert.equal(plain.body.include_secrets, false);
    assert.equal('password' in plain.body, false);
    const sealed = buildExportRequest({scopes: ['settings'], includeSecrets: true,
                                       password: 'uma-senha-bem-longa', confirm: 'uma-senha-bem-longa'});
    assert.equal(sealed.body.password, 'uma-senha-bem-longa');
});

test('export: senha curta ou diferente da confirmação é recusada', async () => {
    const {buildExportRequest, MIN_PASSWORD_LEN} = await load();
    assert.equal(MIN_PASSWORD_LEN, 12);
    const short = buildExportRequest({scopes: ['settings'], includeSecrets: true,
                                      password: 'curta', confirm: 'curta'});
    assert.match(short.error, /12/);
    const exact = 'a'.repeat(12);
    assert.ok(buildExportRequest({scopes: ['settings'], includeSecrets: true,
                                  password: exact, confirm: exact}).body);
    assert.ok(buildExportRequest({scopes: ['settings'], includeSecrets: true,
                                  password: 'a'.repeat(11), confirm: 'a'.repeat(11)}).error);
    assert.match(buildExportRequest({scopes: ['settings'], includeSecrets: true,
                                     password: exact, confirm: exact + 'x'}).error, /conferem/);
});

test('export: mídia exige o escopo de conversas e valida o período', async () => {
    const {buildExportRequest} = await load();
    assert.match(buildExportRequest({scopes: ['settings'], includeMedia: true}).error, /conversas/);
    const ok = buildExportRequest({scopes: ['conversations'], includeMedia: true,
                                   mediaFrom: '2026-01-01', mediaTo: '2026-01-31'});
    assert.equal(ok.body.include_media, true);
    assert.ok(ok.body.media_from < ok.body.media_to);
    // o dia final vai até 23:59:59, não até a meia-noite do início
    assert.ok(ok.body.media_to - ok.body.media_from > 30 * 86400);
    assert.match(buildExportRequest({scopes: ['conversations'], includeMedia: true,
                                     mediaFrom: '2026-02-01', mediaTo: '2026-01-01'}).error, /invertido/);
    assert.match(buildExportRequest({scopes: ['conversations'], includeMedia: true,
                                     mediaFrom: '2026-13-45'}).error, /inválida/);
    const open = buildExportRequest({scopes: ['conversations'], includeMedia: true});
    assert.equal('media_from' in open.body, false);
    assert.equal('media_to' in open.body, false);
});

test('export: fotos de perfil vêm DESLIGADAS e só viajam quando ligadas com conversas', async () => {
    const {buildExportRequest} = await load();
    assert.equal('include_avatars' in buildExportRequest({scopes: ['conversations']}).body, false);
    assert.equal(buildExportRequest({scopes: ['conversations'], includeAvatars: true}).body.include_avatars, true);
    assert.equal('include_avatars' in buildExportRequest({scopes: ['settings'], includeAvatars: true}).body, false);
});

test('escopos oferecidos são exatamente os do servidor, sem os do Pro', async () => {
    const {SCOPES} = await load();
    assert.deepEqual(SCOPES.map((s) => s.id).sort(), ['agents', 'catalog', 'conversations', 'settings']);
});

// ── relatório ───────────────────────────────────────────────────────────────

test('relatório: linhas, total de alterações e erros', async () => {
    const {entityRows, totalChanges, entitiesWithErrors} = await load();
    const rows = entityRows(report());
    assert.equal(rows.length, 4);
    assert.equal(rows.find((r) => r.entity === 'contacts').changes, 14);
    assert.equal(totalChanges(report()), 2 + 3 + 14 + 100);
    assert.deepEqual(entitiesWithErrors(report()).map((e) => [e.entity, e.error]), [['contacts', 2]]);
    assert.equal(totalChanges({}), 0);
});

test('estratégia: mensagens são fixas; o resto é escolhível', async () => {
    const {isFixedStrategy, strategyChoices} = await load();
    assert.equal(isFixedStrategy(report(), 'messages'), true);
    assert.equal(isFixedStrategy(report(), 'tags'), false);
    assert.deepEqual(strategyChoices({}, 'tags'), ['skip']);
});

test('estratégia: em lote ignora o que a linha não aceita e conta', async () => {
    const {applyBulkStrategy, bulkResultText} = await load();
    const r = report();
    const res = applyBulkStrategy(r, r.strategy, ['tags', 'messages'], 'overwrite');
    assert.equal(res.strategy.tags, 'overwrite');
    assert.equal(res.strategy.messages, 'skip');
    assert.deepEqual([res.applied, res.ignored], [1, 1]);
    assert.match(bulkResultText(res), /1 linha alterada; 1 ignorada/);
    const back = applyBulkStrategy(r, res.strategy, ['tags'], 'default');
    assert.equal(back.strategy.tags, 'merge');
    assert.equal(r.strategy.tags, 'merge');                       // não muta a entrada
});

test('prévia desatualizada: só quando a escolha difere da que a prévia usou', async () => {
    const {isPreviewStale} = await load();
    const r = report();
    assert.equal(isPreviewStale(r, {...r.strategy}), false);
    assert.equal(isPreviewStale(r, {...r.strategy, tags: 'overwrite'}), true);
    assert.equal(isPreviewStale(r, {}), false);                   // ausente = a que a prévia usou
});

test('substituições: só as linhas em "overwrite" entram no popup', async () => {
    const {overwriteRows} = await load();
    const rows = overwriteRows(report(), {config: 'merge', tags: 'overwrite', contacts: 'overwrite'});
    assert.deepEqual(rows.map((r) => r.entity), ['tags', 'contacts']);
    assert.deepEqual(overwriteRows(report(), {}), []);
});

// ── confirmação ─────────────────────────────────────────────────────────────

test('confirmar: sempre manda o "revisei" e exige senha quando há segredos', async () => {
    const {buildConfirmRequest, confirmNeedsPassword} = await load();
    const plain = buildConfirmRequest({report: report(), strategy: {tags: 'skip'}});
    assert.equal(plain.body.acknowledged, true);
    assert.equal('password' in plain.body, false);
    for (const state of ['locked', 'ok']) {
        const r = report({manifest: {secrets: state}});
        assert.equal(confirmNeedsPassword(r), true);
        assert.match(buildConfirmRequest({report: r, strategy: {}}).error, /senha/);
        assert.equal(buildConfirmRequest({report: r, strategy: {}, password: 'x'}).body.password, 'x');
    }
    assert.equal(confirmNeedsPassword(report()), false);
    assert.equal(buildConfirmRequest({report: report(), strategy: {}, overwriteSecrets: true})
        .body.overwrite_secrets, true);
});

test('confirmar: não manda campos que o Lite não conhece', async () => {
    const {buildConfirmRequest} = await load();
    const body = buildConfirmRequest({report: report(), strategy: {}, password: 'x',
                                      overwriteSecrets: true, activatePlugins: true, enableChannels: true}).body;
    assert.deepEqual(Object.keys(body).sort(), ['acknowledged', 'overwrite_secrets', 'password', 'strategy']);
});

// ── avisos ──────────────────────────────────────────────────────────────────

test('avisos: código vira frase, desconhecido vira "Aviso: código"', async () => {
    const {warningLabel, warningRows, warningsTitle} = await load();
    assert.match(warningLabel('media_disk_full'), /espaço em disco/);
    assert.equal(warningLabel('codigo_que_nao_existe'), 'Aviso: codigo_que_nao_existe');
    const r = report({warnings: {messages: {media_missing: {count: 3, keys: ['*']}}},
                      notes: ['same_instance']});
    const rows = warningRows(r);
    assert.equal(rows[0].count, 3);
    assert.equal(rows[0].entityLabel, 'Mensagens');
    assert.equal(warningsTitle(r), '4 avisos');
    assert.equal(warningsTitle(report({warnings: {messages: {media_missing: {count: 1, keys: []}}}})), '1 aviso');
});

test('erros: truncamento só aparece quando o servidor cortou', async () => {
    const {truncationText} = await load();
    assert.equal(truncationText({contacts: {total: 5, stored: 5}}, 'contacts'), null);
    assert.match(truncationText({contacts: {total: 1500, stored: 1000}}, 'contacts'), /1\.000 de 1\.500/);
    assert.equal(truncationText({}, null), null);
});

test('erros: o JSON copiado traz só o que o servidor já mascarou', async () => {
    const {errorsAsJson} = await load();
    const out = JSON.parse(errorsAsJson([{entity: 'contacts', row_index: 7, code: 'invalid_row',
                                          detail: {key: '***1234'}, id: 99, created_at: 1}]));
    assert.deepEqual(out, [{entidade: 'contacts', linha: 7, codigo: 'invalid_row', detalhe: {key: '***1234'}}]);
});

// ── job ─────────────────────────────────────────────────────────────────────

test('job: para de consultar quando termina ou espera uma ação', async () => {
    const {shouldStopPolling} = await load();
    for (const state of ['succeeded', 'failed', 'cancelled', 'awaiting_confirmation', 'awaiting_restart']) {
        assert.equal(shouldStopPolling({state}), true, state);
    }
    for (const state of ['queued', 'running']) assert.equal(shouldStopPolling({state}), false, state);
    assert.equal(shouldStopPolling(null), false);
});

test('job: percentual limitado a 0..100 e 100 quando concluído', async () => {
    const {percentOf, nextPollDelay} = await load();
    assert.equal(percentOf({state: 'succeeded', progress: {percent: 3}}), 100);
    assert.equal(percentOf({state: 'running', progress: {percent: 250}}), 100);
    assert.equal(percentOf({state: 'running', progress: {percent: -5}}), 0);
    assert.equal(percentOf({state: 'running', progress: {}}), null);
    assert.equal(percentOf({state: 'running', progress: {percent: NaN}}), null);
    assert.deepEqual([0, 1, 2, 3, 9].map(nextPollDelay), [2000, 4000, 8000, 10000, 10000]);
});

test('job: texto de progresso', async () => {
    const {progressText} = await load();
    assert.equal(progressText({state: 'queued'}), 'Aguardando na fila…');
    assert.equal(progressText({state: 'running', progress: {entity: 'messages', done: 1200}}),
                 'Mensagens — 1.200 registros');
    assert.equal(progressText({state: 'running', progress: {entity: 'tags', done: 1}}), 'Tags — 1 registro');
    assert.equal(progressText({state: 'running', progress: {entity: 'finalize'}}), 'Finalizando o pacote…');
});

test('job: import interrompido não é falha', async () => {
    const {stateLabel, isInterrupted, interruptedText} = await load();
    const job = {state: 'awaiting_restart', interrupted: true, needs_password: true};
    assert.equal(isInterrupted(job), true);
    assert.equal(isInterrupted({state: 'awaiting_restart', interrupted: false}), false);
    assert.match(stateLabel('awaiting_restart', job), /pode continuar/);
    assert.match(interruptedText(job), /senha do pacote/);
    assert.doesNotMatch(interruptedText({}), /senha/);
});

// ── retomar depois de F5 ────────────────────────────────────────────────────

test('F5: import ativo vence qualquer verificação', async () => {
    const {findResumable} = await load();
    const jobs = [
        {id: 'i', kind: 'import', state: 'running', source_job: 'd'},
        {id: 'd', kind: 'import_dryrun', state: 'succeeded', has_package: true},
    ];
    assert.equal(findResumable(jobs).type, 'import');
});

test('F5: verificação que já virou import não volta; a que falhou volta', async () => {
    const {findResumable} = await load();
    const dry = {id: 'd', kind: 'import_dryrun', state: 'succeeded', has_package: true};
    assert.equal(findResumable([{id: 'i', kind: 'import', state: 'succeeded', source_job: 'd'}, dry]), null);
    assert.equal(findResumable([{id: 'i', kind: 'import', state: 'failed', source_job: 'd'}, dry]).type, 'dry');
    assert.equal(findResumable([{id: 'i', kind: 'import', state: 'cancelled', source_job: 'd'}, dry]).type, 'dry');
});

test('F5: verificação descartada, vencida ou sem pacote não volta', async () => {
    const {findResumable} = await load();
    const dry = {id: 'd', kind: 'import_dryrun', state: 'succeeded', has_package: true, expires_at: 1000};
    assert.equal(findResumable([dry], {dismissed: 'd', now: 10}), null);
    assert.equal(findResumable([dry], {now: 2000}), null);
    assert.equal(findResumable([{...dry, expires_at: null, has_package: false}]), null);
    assert.equal(findResumable([{...dry, expires_at: 5000}], {now: 10}).type, 'dry');
    assert.equal(findResumable([{...dry, state: 'running'}], {now: 2000}).type, 'dryrun');
    assert.equal(findResumable([]), null);
    assert.equal(findResumable(null), null);
});

// ── utilidades ──────────────────────────────────────────────────────────────

test('formatBytes e o teto de upload espelham o servidor', async () => {
    const {formatBytes, MAX_IMPORT_BYTES} = await load();
    assert.equal(MAX_IMPORT_BYTES, 5 * 1024 ** 3);
    assert.equal(formatBytes(0), '0 B');
    assert.equal(formatBytes(1023), '1023 B');
    assert.equal(formatBytes(1024), '1 KB');
    assert.match(formatBytes(5 * 1024 ** 3), /^5 GB$/);
    assert.equal(formatBytes(undefined), '0 B');
});

test('gate de senha: só o código password_required conta', async () => {
    const {isPasswordRequired} = await load();
    assert.equal(isPasswordRequired({ok: false, error_code: 'password_required', status: 403}), true);
    assert.equal(isPasswordRequired({ok: false, error_code: 'job_busy', status: 409}), false);
    assert.equal(isPasswordRequired({ok: true}), false);
    assert.equal(isPasswordRequired(null), false);
});

test('duração e tempo decorrido', async () => {
    const {elapsedText, jobDuration} = await load();
    assert.equal(elapsedText(42), '42 s');
    assert.equal(elapsedText(125), '2 min 05 s');
    assert.equal(elapsedText(-3), '0 s');
    assert.equal(jobDuration({started_at: 10, finished_at: 75}), '1 min 05 s');
    assert.equal(jobDuration({started_at: 10}), '');
});

test('dayStart rejeita data inexistente', async () => {
    const {dayStart, dayEnd} = await load();
    assert.equal(dayStart('2026-02-30'), null);
    assert.equal(dayStart(''), null);
    assert.equal(dayStart('01/02/2026'), null);
    assert.ok(dayEnd('2026-02-01') > dayStart('2026-02-01'));
});

// ── Fase 2: ferramentas com código ──────────────────────────────────────────

test('pacote com código de ferramenta: o confirm exige o "entendo" e manda accept_code', async () => {
    const {buildConfirmRequest, codeToolsTotal, codeToolRows} = await load();
    const r = {...report(), code_tools_total: 2,
        code_tools: [{name: 'a_tool', state: 'new', dependencies: true}, {name: 'b_tool', state: 'differs', dependencies: false}]};
    assert.equal(codeToolsTotal(r), 2);
    assert.match(buildConfirmRequest({report: r, strategy: {}}).error, /código/);
    assert.match(buildConfirmRequest({report: r, strategy: {}, acceptCode: false}).error, /código/);
    assert.equal(buildConfirmRequest({report: r, strategy: {}, acceptCode: true}).body.accept_code, true);
    const rows = codeToolRows(r);
    assert.equal(rows[0].name, 'a_tool');
    assert.match(rows[0].text, /pip/);
    assert.match(rows[1].text, /diferente/);
});

test('pacote sem código: o corpo do confirm não leva accept_code', async () => {
    const {buildConfirmRequest, codeToolsTotal} = await load();
    assert.equal(codeToolsTotal(report()), 0);
    assert.equal('accept_code' in buildConfirmRequest({report: report(), strategy: {}}).body, false);
});
