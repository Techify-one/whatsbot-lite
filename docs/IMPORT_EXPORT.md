# Importar / Exportar (migrar entre instalações do WhatsBot-Lite)

Leva as configurações, as tags, os contatos e as conversas de uma instalação para outra: um pacote `.zip` que a
tela **Painel → Sistema → Importar / Exportar** gera e a mesma tela lê. A tela, a ajuda
([agent/SYSTEM_HELP.md](../agent/SYSTEM_HELP.md)) e este guia falam de coisas diferentes: aqui está o **porquê** das
decisões. Código em [portability/](../portability), rotas em
[server/routes/portability.py](../server/routes/portability.py), tela em
[web/static/js/components/portability/](../web/static/js/components/portability).

## O que é diferente do WhatsBot Pro

O Pro tem um sistema parecido, e este foi desenhado a partir dele, mas **não é compatível**: o pacote do Lite tem
formato próprio (`product: "whatsbot-lite"` no manifest) e um pacote do Pro, ou sem esse campo, é recusado com
mensagem clara. O Lite é menor, então o import/export também é:

| Pro | Lite |
|---|---|
| usuários, grupos, times, caixas | **não existem**: uma só senha de painel |
| vários canais (Telegram, Meta…) | **não existem**: um só GOWA |
| conversa separada do contato | **não existe**: a conversa é implícita (`messages.contact_id`) |
| protocolos, etiquetas, campos personalizados, respostas rápidas | **não existem**: só tags e observações |
| agentes de IA, prompts, variáveis, ferramentas | **viajam** (escopo "Agentes de IA"; `ai_credentials` não existe no Lite) |
| plugins | **viajam** (escopo "Plugins"; código desligado, config e dados em 2 etapas) |
| Postgres apenas | **SQLite ou Postgres**: o motor de jobs é SQL portável |
| reinício automático no meio do import | **não há**; só "Continuar" depois de uma interrupção |

## O que viaja (escopos)

| Escopo | Entidades | Observação |
|---|---|---|
| Configurações do sistema | `config` | por **allowlist**, nos dois sentidos |
| Tags | `tags` | nome único + cor |
| Contatos e conversas | `contacts`, `messages` | + observações, tags de cada contato, mídia e fotos (opcionais) |
| Agentes de IA | `ai_variables`, `ai_prompts`, `ai_tools`, `ai_agents`, `tool_overrides` | ⚠️ `ai_tools` leva **código Python**: ver "Motor de IA" |
| Plugins | `plugins` (código), `plugin_config`, `plugin_data` | ⚠️ código que roda no servidor, dados em 2 etapas: ver "Plugins" |

### Configuração: allowlist, nunca denylist

Só viaja a chave que está classificada em [portability/registry.py](../portability/registry.py)
(`EXPORTABLE_CONFIG`, `SECRET_CONFIG`, `CONFIG_NEVER`). Chave nova do `DEFAULT_CONFIG` sem classificação **não
viaja** e quebra o teste de cobertura (`tests/test_portability_registry.py`): quem cria a chave decide, de propósito.
A allowlist vale também na **volta**: um pacote adulterado não consegue gravar `web_password_hash`,
`installation_id` nem `ai_engine_enabled`, mesmo que consiga forjar o arquivo.

Nunca viajam: senha do painel (o token de sessão é determinístico a partir do par hash+sal, então levar o par
entregaria uma sessão válida), `installation_id`, `access_token`/`account_url`, `bot_phone`, estado do WhatsApp,
proxy do GOWA (é rede da origem), portas, limite de disco, `ai_engine_enabled` (um pacote não liga o motor de IA
sozinho) e `plugin.*`.

A única chave **secreta** é `openrouter_api_key` (nome legado da chave do proxy LLM). Ela só viaja cifrada em
`secrets.enc`. Depois de importar config, o processo aplica ao `AgentHandler` o mesmo repasse que o `PUT /api/config`
faz; sem isso o prompt, o modelo e a chave antigos ficariam em memória até o próximo reinício. ⚠️ `Settings.get` dá
prioridade a variáveis de ambiente (`OPENROUTER_API_KEY`, `WHATSBOT_MODEL`, `WHATSBOT_SYSTEM_PROMPT`): se uma delas
está definida, o valor importado fica mascarado, e a verificação avisa (`env_override_active`).

## Formato

`.zip` com NDJSON (uma linha por registro):

```
manifest.json            # por ÚLTIMO; checksums SHA-256 de tudo; product, app_version, alembic_head, scopes, counts
data/config.ndjson
data/tags.ndjson
data/contacts.ndjson
data/messages.ndjson
media/<sha1[:20]><ext>   # opcional
avatars/<sha1[:20]>.jpg  # opcional
secrets.enc              # opcional: scrypt + AES-256-GCM
```

- **Zip-slip e zip-bomb** são recusados antes de ler qualquer conteúdo (`inspect_zip`): caminho absoluto, `..`,
  barra invertida, entradas demais, razão de compressão suspeita, linha NDJSON gigante. O `file_size` do diretório
  central é só a primeira barreira (o próprio arquivo o declara); a barreira real conta os bytes descomprimidos.
- **Senha do pacote ≠ senha do painel.** Mínimo de 12 caracteres, definida na hora de exportar. Ela deriva a chave
  por scrypt; o manifest entra como dado autenticado (AAD), então **trocar o manifest é adulteração** e falha como
  senha errada. Sem a senha não há recuperação.
- `cryptography` é importada **sob demanda**. O self-update do Lite sobrescreve o código mas **não roda `pip`** (só os
  launchers e o Dockerfile instalam o `requirements.txt`), então uma instalação recém-atualizada pode não a ter.
  Sem ela, exportar e importar **sem segredos** funciona; com segredos a rota responde 503 `crypto_unavailable` com a
  instrução ("reinicie pelo iniciador").

## Chave natural, nunca id

O id de uma instalação não vale na outra. Cada entidade casa por uma chave que sobrevive à viagem, e é isso que
torna reimportar **idempotente** (nada duplica, e é por isso que retomar um import interrompido é seguro):

| Entidade | Chave |
|---|---|
| config | a chave |
| tag | o nome |
| contato | o telefone, **com as variantes brasileiras** (12 e 13 dígitos: com e sem o 9) |
| observação | (texto, `created_at`) dentro do contato |
| mensagem | `msg_id` do WhatsApp, ou (`ts`, `role`, md5 do texto) dentro do contato, como **multiconjunto** |

A mensagem do pacote aponta para o id **de origem** do contato; o import mantém um mapa `id_origem → id_destino` em
memória (contatos e mensagens sempre viajam juntos), por isso não há tabela de mapeamento.

⚠️ O telefone de destino vem do contato **já resolvido no destino**, nunca da linha do pacote, inclusive para dar
nome ao arquivo da foto de perfil.

## Motor de IA

Escrita pelos repositórios (`agent_repo`, `prompt_repo`, `variable_repo`, `tool_repo`, `tool_override_repo`), então cada
mudança sobe a `version` e gera histórico. Agente, prompt e variável valem na **próxima mensagem** (a fábrica lê o banco a
cada pedido); só o ajuste de ferramentas recarrega a lista do `AgentHandler`. Nada emite evento.

### ⚠️ `ai_tools.code` é código que roda no boot

`agent/ai_tool_installer.py` importa toda ferramenta com `enabled=1` **sem nenhum interruptor** e instala `dependencies`
com `pip` no mesmo passo. Um pacote é dado não confiável, então:

- a ferramenta **sempre entra desligada** (`enabled=0`, `install_status='pending'`); o `enabled` do pacote é ignorado.
  Quem quiser usá-la a liga depois de ler o código;
- só é gravada com **`accept_code`**. A rota de confirmação exige `accept_code: true` sempre que a verificação achou
  código (`code_tools_total > 0`), **pelo que o pacote carrega e não pela estratégia escolhida**: com `skip` nada seria
  gravado, mas trocar para `overwrite` sem recalcular não pode esconder código. O motor recusa de novo (`env.accept_code`,
  aviso `tool_code_not_accepted`): é defesa em profundidade, não a única barreira;
- o popup lista cada ferramenta (nova, igual ou diferente da existente; com ou sem dependências) e tem uma caixa própria,
  separada do "Revisei e quero iniciar";
- a dependência passa por **regex fechada** (`entities_ai.is_pip_spec`): nome + extras + comparadores de versão. Começar
  com `-` (`--index-url`, `-r`), ter `@`, `://`, `;` ou espaço é recusado, porque a lista vai direto para o argv do `pip`;
- o nome da ferramenta segue a **mesma regra do instalador** (`NAME_RE`);
- código **não é mesclado**: `ai_tools` só aceita `skip` e `overwrite`; idêntica à que já existe fica como está (não a
  desliga); substituída fica **desligada** (`tool_replaced_disabled`);
- `install_status`, `install_error` e `installed_deps` **não viajam** (são desta máquina).

### Demais entidades

- **`ai_engine_enabled` nunca viaja**: importar um agente não liga o motor.
- **Variáveis**: nome no padrão do `{placeholder}`. Nome que indica segredo (`registry.is_secret_name`) só vai em
  `secrets.enc`; **o nome decide, não a marca `secret` do pacote**, então um pacote forjado não coloca valor em claro.
- **Prompts e agentes**: `merge` não pisa no que foi **editado neste servidor** (`version > 1`); a semente do boot
  (`version == 1`) é substituída, senão importar numa instalação nova não traria o prompt. `model_config` guarda só os quatro
  campos que o motor lê (`model`, `temperature`, `top_p`, `max_tokens`); `tool_names` é `null` (todas) ou lista de nomes.
  Referência solta (prompt ou ferramenta que não existe aqui nem veio no pacote) não impede o import: avisa.
- **`tool_overrides`** (ferramenta desligada, renomeada ou redescrita) **nunca cria linha**: ela nasce quando a ferramenta
  é registrada no boot, e o boot apaga as órfãs. Ferramenta que não existe aqui (plugin ausente) é pulada com aviso.
- Ordem de import: variáveis → prompts → ferramentas → agentes → ajustes (o agente confere a ferramenta que o pacote cria).

## Plugins

Três coisas, três riscos. O código está em [portability/plugins_io.py](../portability/plugins_io.py) e as barreiras de zip em
[plugins/package.py](../plugins/package.py), que **o "Importar (.zip)" e o "Atualizar" da tela de plugins também usam**.

### ⚠️ Código: roda no servidor

Mesma regra das ferramentas de IA, com o que muda por ser uma pasta inteira de código:

- o plugin **entra sempre desligado**, e o que já estava instalado e é **substituído também fica desligado** (trocar o
  código de algo que roda não pode ser silencioso). Ativar é uma decisão manual, na aba Plugins;
- só é gravado com **`accept_code`**: a rota exige quando `code_tools_total + code_plugins_total > 0` (pelo que o pacote
  **carrega**, não pela estratégia escolhida) e o motor recusa de novo (aviso `plugin_code_not_accepted`);
- `skip` é o padrão e **nunca** toca num plugin instalado. `overwrite` só atualiza para uma versão **maior** (nunca
  rebaixa: `plugin_downgrade_skipped`) e uma pasta sem registro no banco é deixada como está (`plugin_orphan_dir`);
- o manifest do zip é validado **antes** de tocar o disco: id do manifest = id do pacote, `version` semver e
  `whatsbot_api_version` compatível com esta instalação;
- **zip-slip e link simbólico** são recusados antes de extrair qualquer byte. **Zip-bomb**: o `file_size` do diretório
  central é declarado pelo próprio arquivo, então a barreira de verdade **conta os bytes efetivamente descomprimidos**
  (`safe_extract`: 128 MB por arquivo, 256 MB no total). A **prévia faz o ensaio** da extração (`safe_extract(zf, None)`
  percorre e conta sem gravar), então recusa a mesma bomba que o import recusaria;
- a extração acontece numa pasta de rascunho com `.` na frente (que o loader ignora) e só então vai ao lugar: falha no
  meio nunca deixa um plugin pela metade.

> **Correção que veio junto.** O "Importar (.zip)" da tela de plugins extraía com `extractall` e **sem teto**: um `.zip` de
> ~300 KB virava 300 MB em disco. O "Atualizar" só conferia o tamanho *declarado*. Agora os dois usam o mesmo
> `plugins/package.py`, e `tests/test_plugin_import_route.py` manda a bomba pela rota HTTP de verdade.

O export zipa a pasta do plugin sem `__pycache__`, sem banco local (`.db`) e **sem seguir link simbólico**.

### Configuração (`plugin.<id>.*`)

Só de plugin que existe aqui (ou que o próprio pacote instala). Campo cujo **nome** indica credencial
(`registry.is_secret_field`, a regra estreita: `token`, `senha`, `api_key`…) só viaja cifrado; **o nome decide, nunca a marca
`secret` do pacote**. Valor que aponta para a própria máquina (`127.0.0.1`, `localhost`) não vai
(`environment_value_dropped`). `skip` é o padrão: o valor que o operador já tem vale.

### Dados (`plugin_<id>_*`) em duas etapas

A tabela de um plugin só existe depois de ele ser **ativado** e o servidor **reiniciar** (as migrations rodam no boot), e
nunca criamos tabela de plugin por conta própria: o schema é do plugin. Então:

1. **1ª etapa**: entra tudo o que o destino já consegue receber (código, config, dados de plugin que já migrou). A
   tabela que ainda não existe fica **pendente** (`plugin_data_pending`) e o job pausa em `awaiting_restart` com
   `resume_stage='plugins'`. Não é falha nem interrupção.
2. O operador **ativa o plugin**, espera o servidor reiniciar e clica em **Continuar importação**. A 2ª etapa só mexe em
   `plugin_data` e preserva os números da 1ª. Se ainda sobrar tabela sem plugin ativo, o job volta a esperar.

O Lite **não** reinicia sozinho no meio do import (o `plugins/restart.py` não tem `is_supervised()` nem `boot_id`): a retomada é
**sempre manual**.

Regras de cada tabela:

- **Nome de tabela e de coluna vêm do catálogo do banco destino** (`inspect`), nunca do pacote; o pacote só fornece
  **valores**, parametrizados. Coluna do pacote que o destino não tem é descartada, não executada;
- **tabela com dados no destino é pulada inteira** (`plugin_table_not_empty`): sem a chave natural da tabela, os ids de
  origem colidiriam com os do destino. `merge` é opt-in e só deixa entrar linha de **id livre**, nunca troca a do destino;
- **uma transação por tabela**: falha do banco no meio desfaz a tabela inteira (`plugin_table_failed`) e o job segue;
- **`contact_id` viaja pelo TELEFONE** (em `__refs`), a chave natural, e é resolvido no destino mesmo depois do
  reinício, sem estado salvo. Contato que não existe aqui vira `NULL` (se a coluna aceita) ou a linha é ignorada: o id cru
  **nunca** é copiado (apontaria para outra pessoa);
- coluna de **nome secreto** vai em `secrets.enc` indexada pela chave primária; sem a senha entra **vazia** (nunca em
  claro, e nunca `NULL` em coluna `NOT NULL`, que derrubaria a tabela);
- `media_path` **nunca** vem do pacote (arquivo de plugin não viaja); coluna binária vira vazia;
- tabelas de nome `…session`, `…token`, `…cache`, `…queue`, `…lock` **nunca saem**;
- no Postgres as sequences são reancoradas ao fim (`repair_postgres_sequences`); no SQLite o autoincremento acompanha.

Diferenças para o Pro, por desenho: o Lite não tem o contrato `export_data`/`import_data` por serviço (só o caminho
genérico) nem tabela de mapa de ids, e não há usuários nem caixas para religar.

## Estratégias (se já existir no servidor)

`skip` (só adicionar o que falta), `merge` (completar o que existe) e `overwrite` (substituir pelo do pacote), **por
entidade**. Mensagens só aceitam `skip`: nunca se altera nem se duplica uma mensagem que já existe. O padrão
(`default_strategy`) é `merge` onde existe e `skip` onde não. A tela mostra o texto em português, os valores internos
não mudam.

A verificação (dry-run) e o import chamam **a mesma** `apply(row, strategy, env, dry_run)`, então as contagens
mostradas na prévia são exatamente as do import. Um valor igual ao do destino nunca conta como atualização.

## "Importar não é atividade"

Um histórico que entra não pode parecer movimento novo no painel:

- as **não lidas** entram zeradas (contador e `has_unread_mention`), senão 50 mil mensagens inundariam o painel;
- o `updated_at` do contato **não** vira "agora" (a lista ordena por `COALESCE(última mensagem, updated_at)`);
- **nenhum evento** é emitido: nem de plugin (`message.*`, `contact.*`, `tag.*`, `config.changed`) nem `new_message`
  no WebSocket, e a IA não responde a nada importado. O único broadcast é o de conclusão (`config_saved`,
  `tags_changed`) e a invalidação do cache da tela Arquivos.

`ai_enabled` e `ai_disabled_at` do contato viajam como na origem.

## Mídia

Opcional, com filtro por período (só afeta a mídia: as mensagens saem todas). Só os dois diretórios que a tela
Arquivos conhece (`statics/media` e `statics/operator`, **a mesma fonte**: `file_manager.ROOTS`) e só arquivo
**regular direto** neles. O resto do disco (banco, sessão do WhatsApp, código, `.env`) é inalcançável por construção.

- **Nenhum caminho do pacote vira caminho de disco.** No pacote o arquivo se chama pelo hash do caminho de origem; no
  destino ganha um nome **novo** (uuid).
- ⚠️ **A extensão passa por allowlist** (`SAFE_EXT`; `ACTIVE_EXT` nunca entra): `/statics` é um `StaticFiles` puro,
  então um `.html`/`.svg`/`.xml` importado seria servido **na mesma origem do painel**, com script rodando com a
  sessão do operador. Fora da lista vira `.bin`.
- A origem é **validada de novo na hora de copiar** (TOCTOU): link simbólico, diretório, arquivo que sumiu ou saiu
  do diretório fica de fora, sem derrubar o job.
- Tetos: 512 MB por arquivo e 4 GB por pacote (mídia não comprime). Passou do teto ⇒ a mensagem vai **sem** o arquivo,
  com aviso: o texto nunca se perde. **Disco cheio** no destino para de gravar mídia e mantém as mensagens.
- `media_state` `discarded`/`removed` viaja sem arquivo; mídia sem arquivo aparece como "Arquivo removido".

**Fotos de perfil** são **desligadas por padrão** (diferente do Pro): o Lite refaz o cache sozinho (busca no GOWA ao
abrir a conversa e a cada 30 minutos), então levar a foto só incha o pacote. Mesmas regras: só JPEG (cabeçalho
`FF D8 FF`), 1 MB por foto, 256 MB no total, gravação atômica; foto ausente no destino é preenchida em qualquer
estratégia e só `overwrite` troca uma existente.

## Jobs

Estado em tabela (`portability_jobs`, `portability_job_errors`; migration `0015`), SQL **portável SQLite/Postgres**:

- JSON como `Text`; o que se consulta vira coluna real (`package_sha256`, `source_job`, `affinity`, `resume_stage`);
- *claim* por `UPDATE ... WHERE state='queued'` decidido por `rowcount` (sem `SKIP LOCKED`, que o SQLite não tem);
- estados: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `awaiting_confirmation`, `awaiting_restart`;
- um **laço de fundo** (`portability_jobs_loop`, em `server/background.py`) executa um job por vez, com heartbeat;
- **órfãos** (processo morreu no meio) voltam à fila até 3 vezes. Com senha de pacote em jogo (que só existia na memória
  e morreu junto) o import vira **Interrompido** e espera o operador reenviar a senha e clicar em **Continuar**;
  export/verificação interrompidos falham e se refazem do zero;
- pacote vence em 24 h, o registro do job em 7 dias; pasta órfã some em 1 h;
- erros por linha têm teto (1.000 por entidade, 5.000 por job) e **nunca** levam conteúdo: telefone mascarado
  (`***1234`), sem nome de cliente nem texto de mensagem. O que passou do teto é contado mas não guardado
  ("mostrando N de M");
- a **senha do pacote só existe em memória** (`stash_password`/`take_password`), é guardada **antes** de o job existir
  na fila (o laço reivindica `queued` a qualquer instante) e descartada em qualquer saída: sucesso, falha, cancelamento
  ou pausa. Nunca vai à tabela, ao log nem à resposta da API;
- `storages/portability/` fica em `storages/` (persiste em Docker e sobrevive ao self-update) e **fora de qualquer
  mount servido ao navegador** (só `web/static`, `statics` e a estática dos plugins são servidos). Há teste que tenta
  alcançá-la por URL.

A migração SQLite → Postgres **não copia** as tabelas de job (`EPHEMERAL_TABLES` em `db/migration_postgres.py`):
apontam para pacotes e processos da instalação antiga.

## Rotas (`/api/portability/*`)

| Método | Rota | O que faz |
|---|---|---|
| POST | `/exports` | cria o job de export (senha só em memória) |
| POST | `/jobs/{id}/download` | baixa o pacote. **POST de propósito** (não entra em cache, prefetch nem histórico) e `no-store` |
| POST | `/imports` | upload (multipart) em blocos → job de **verificação** |
| POST | `/imports/{id}/confirm` | cria o import sobre o **mesmo** pacote; exige `acknowledged: true` |
| POST | `/imports/{id}/recalculate` | refaz a verificação com outras estratégias (mesmo job) |
| POST | `/jobs/{id}/resume` | continua um import interrompido (pede a senha de novo) |
| GET | `/jobs`, `/jobs/{id}`, `/jobs/{id}/errors` | consulta (polling) |
| POST | `/jobs/{id}/cancel` | cancela |

Regras de toda rota:

- **Exige senha de painel** (`password_required`, 403). O Lite só tem uma senha e, sem ela, `/api/*` fica aberto a quem
  alcançar a porta; exportar levaria a instalação inteira e importar gravaria nela. A tela, sem senha, nem oferece os
  assistentes e leva ao card de senha. Com senha e sem token, quem responde é o middleware (401).
- **Um job por vez** (409 `job_busy`), serializado por lock: o import grava no mesmo banco do webhook, e dois pacotes
  em paralelo só disputariam o lock do SQLite.
- **Teto de upload** (5 GB) conferido **duas vezes**: pelo `Content-Length` declarado (antes do corpo) e pela contagem
  de bytes reais (o cabeçalho é do cliente). Estouro ⇒ 413 e o parcial é apagado. Zip inválido não cria job nem pasta.
- `_public()` é **lista branca**: nunca devolve `params`, `package_path` nem a senha.
- **Polling, não WebSocket**: o `/ws` é broadcast para todo painel aberto, e o progresso de um pacote não pode vazar.
- O import só nasce de uma verificação **concluída**, e uma verificação vira **no máximo um** import. Se o import
  falhar ou for cancelado a verificação continua válida (reimportar é idempotente).

## Tela

Card **Importar / Exportar** na aba Sistema (`/painel?aba=sistema#import-export`), sem rota SPA nova. Sem build de JS;
o CSS é gerado (`cd tools/css && npm run build`, conferido por `npm run check`).

- A **lógica** está em [portabilityLogic.js](../web/static/js/utils/portabilityLogic.js) (puro, testado com
  `node --test tests/portability-logic.test.cjs`); os componentes só desenham.
- O servidor manda **código** de aviso, nunca texto com dado; a frase vive só no JS. A lista oficial é
  [portability/warnings.py](../portability/warnings.py), e `tests/test_portability_warnings.py` quebra se o motor
  emitir um código fora dela, se a lista ficar com código morto, ou se faltar/sobrar frase na tela. **Aviso novo = uma
  linha em `warnings.py` + a frase no JS, no mesmo commit.**
- O popup de confirmação lista o que será **substituído** e exige "Revisei e quero iniciar"; o servidor exige o mesmo
  (`acknowledged`), então a rota não é contornável por quem chama a API.
- A senha do pacote digitada no envio fica **só na memória do navegador** (nunca em storage nem URL), é reenviada
  no corpo de recalcular/confirmar/retomar (o servidor a consome e não a guarda) e some ao concluir ou descartar.
- Depois de um F5 a tela se reconstrói a partir dos jobs do servidor (`findResumable`).
- Modo escuro: só `wa-*`, `.wa-field` e as tintas amber/red/blue que o `custom.css` já cobre.

## O que ainda não viaja

- Arquivos que o plugin guarda em disco (`storages/plugin_data/<id>/`) e `media_path` de tabela de plugin **não viajam**.
- Reinício automático entre as duas etapas dos plugins (o Lite só tem a retomada manual).
- Telefone de grupo (JID `…@g.us`) e foto de grupo seguem as mesmas regras do contato.

## Limites conhecidos

- O pacote do Lite **não** é lido pelo Pro (e vice-versa). `schema_version` e `product` deixam a porta aberta para um
  conversor.
- Sem `audit_log` no Lite, quem importou o quê fica no log do servidor (só contagens) e na tabela de jobs.
- Importar um histórico grande no SQLite compete com o webhook pelo lock de escrita: por isso as mensagens entram em
  lotes curtos, com repetição curta em "database is locked". O lote que falha é regravado linha a linha e a contagem
  do relatório é corrigida.
- Reiniciar no meio de um import com segredos exige reenviar a senha (ela só existia na memória). Reimportar é
  idempotente, então nada duplica.
- O tempo de expiração da mídia no WhatsApp não é oficial: pacote exportado com mídia que o GOWA já não tem mais traz
  só o texto, com o aviso `media_missing`.

## Testes

```bash
python -m pytest tests/test_portability_*.py      # formato, cripto, registro, jobs, round-trip, contatos, mídia, rotas, avisos
node --test tests/portability-logic.test.cjs      # lógica da tela
(cd tools/css && npm run check)                   # tailwind.css atualizado
```

Rodam em SQLite e, com `WHATSBOT_TEST_DB_URL=postgresql+psycopg://…`, em Postgres. Os que cifram precisam de
`cryptography` e **pulam** sem ela. As garantias de segurança foram conferidas por **mutação** (remover a proteção e
ver o teste falhar): allowlist de config nos dois sentidos, extensão ativa → `.bin`, TOCTOU, link simbólico, teto de
upload, gate de senha, senha em memória, um job por vez, confirmação, ferramenta sempre desligada, `accept_code` na rota
e no motor, regex de dependência, merge de código proibido, plugin sempre desligado, zip-slip/bomba/link simbólico no
plugin, segredo de plugin por nome, `contact_id` pelo telefone.
