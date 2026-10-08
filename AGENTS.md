# WhatsBot-Lite

Bot de WhatsApp com IA para usuários finais, distribuído como EXE Windows.

## Stack

- **Python 3.11+** — linguagem principal
- **SQLAlchemy 2.0 Core + Alembic** — camada de dados portável (Core, sem ORM declarativo)
- **SQLite** — banco default (WAL mode, driver `sqlite3` da stdlib)
- **PostgreSQL** — backend opcional via `psycopg[binary]`, configurável pela tela Settings → Banco
- **GOWA** (go-whatsapp-web-multidevice) — bridge WhatsApp via REST, roda como subprocess. A versão
  que acompanha o app fica no arquivo `GOWA_VERSION` na raiz (fonte única lida pelo Dockerfile, pelos
  launchers e por `gowa/binary.py`). O usuário pode atualizar o binário pelo painel; ver
  [Atualização do GOWA](#atualização-do-gowa)
- **Proxy LLM da Techify** (`https://llm.techify.one/api/v1`) — provider de LLM, API **compatível com OpenRouter/OpenAI**. Substituiu o OpenRouter direto: a chave é provisionada pelo wizard de 1ª execução e o crédito/recarga é gerido pela Techify. O base URL é configurável via env `LLM_API_BASE_URL`. A chave continua sendo persistida na config key `openrouter_api_key` (nome legado mantido por compatibilidade)
- **AGNO** (`agno` 2.x) — framework de agentes usado como **motor de LLM** do agente. O loop de raciocínio + tool calling roda via `agno.agent.Agent`, apontado ao proxy Techify pelo model `OpenAILike`. Encapsulado em [agent/agno_engine.py](agent/agno_engine.py); o `AgentHandler` delega a ele preservando todos os hooks de plugin (filters/events), usage e execution tracking. Transcrição de áudio/descrição de imagem continuam em chamadas diretas ao cliente OpenAI (não são agênticas)
- **FastAPI + uvicorn** — backend web (REST API + WebSocket)
- **Preact + HTM + Tailwind CSS**: frontend web (sem build step para o JS; o CSS utilitário é pré-gerado e versionado em `web/static/css/tailwind.css`, ver [CSS do Tailwind](#css-do-tailwind))
- **PyInstaller** — empacotamento como EXE

## Arquitetura

```
main.py              → entry point, inicia uvicorn + abre browser
server/app.py        → FastAPI app (endpoints REST, WebSocket, webhook, background tasks)
gowa/manager.py      → lifecycle do subprocess GOWA (start/stop/watchdog)
gowa/client.py       → HTTP client para REST API do GOWA (localhost:3000)
agent/handler.py     → orquestra o processamento de mensagens (system prompt, filters/events, usage, save); delega o loop de LLM ao motor AGNO
agent/agno_engine.py → motor AGNO: monta OpenAILike + Agent único, envolve cada tool em agno Function (filters/events preservados), extrai reply/usage
agent/memory.py      → ContactMemory e TagRegistry (leitura/escrita no SQLite via repos)
agent/group_mentions.py → resolução de @menções em grupos (número ↔ nome, lista de membros, @todos)
agent/tools/         → tools core do LLM (uma tool por arquivo, agregadas em CORE_TOOLS)
config/settings.py   → load/save config + constantes do provider/Techify (LLM_API_BASE_URL, TECHIFY_*)
server/avatars.py    → cache de fotos de perfil em disco (statics/avatars/<phone>.jpg) + broadcast avatar_updated
server/balance_monitor.py → consulta saldo de crédito do proxy (/credits) e emite low_balance via WS
db/                  → módulo de banco de dados (SQLAlchemy 2.0 Core)
  engine.py          → factory do Engine, URL resolution (env > arquivo > sqlite default), PRAGMAs SQLite
  tables.py          → MetaData + 11 Table objects (Core, sem mapper/Session)
  upsert.py          → helper dialect-agnóstico (INSERT ... ON CONFLICT)
  connection.py      → init_db(): cria engine + roda Alembic upgrade
  migration_postgres.py → migra dados SQLite → Postgres (usado pelo endpoint admin)
  migrate_json.py    → migração one-time de JSON legado → banco
  alembic/           → migrations Alembic (env.py + versions/)
  repositories/      → data access layer (um arquivo por domínio)
    config_repo.py   → get_all(), get(), set(), set_many(), delete_prefix()
    contact_repo.py  → get_or_create(), update(), list_contacts(), get_full_contact()
    message_repo.py  → add(), get_all(), get_context(), get_last(), delete_all()
    usage_repo.py    → add(), global_summary(), by_contact(), detail()
    tag_repo.py      → get_all(), create(), update(), delete(), set_contact_tags()
    plugin_repo.py   → list_all(), upsert(), set_enabled(), applied_migrations()
plugins/             → sistema de plugins (core, não confundir com storages/plugins)
  loader.py          → PluginRegistry, descoberta + importlib + bootstrap
  manifest.py        → parser plugin.yaml + validação semver
  migrator.py        → runner SQL com prefixo plugin_<id>_ obrigatório
  context.py         → ToolContext, PromptContext (passados aos plugins)
  restart.py         → schedule_restart() — touch sentinela + os._exit
assets/              → recursos não-código (templates copiados em runtime)
  plugin_examples/   → plugins de referência (copiados pra storages/plugins/ no 1º boot)
storages/plugins/    → user-writable, ignorado por .gitignore (preservado em updates)
web/index.html       → entry point do frontend (HTML + import map)
web/static/js/       → componentes Preact + HTM (sem build step)
web/static/vendor/   → libs JS vendorizadas (preact, htm, tailwind)
web/static/css/tailwind.css → CSS utilitário pré-gerado do core (não editar à mão; gerado por tools/css)
web/static/tailwind-theme.json → tema do Tailwind (fonte única para o build e para o runtime dos plugins)
tools/css/           → build do CSS (Node só em dev; usuários finais não precisam)
GOWA_VERSION         → versão do GOWA que acompanha esta release (fonte única)
gowa/binary.py       → resolve qual binário rodar (bin/ bundled vs storages/bin/ gerenciado) + metadados
gowa/updater.py      → download/verificação/swap/rollback do binário GOWA
gowa/proxy.py        → proxy de saída da conexão do WhatsApp (validação, URL, teste de conexão)
server/routes/gowa_update.py → endpoints /api/gowa/* (versão, check, update, rollback, skip, proxy)
bin/gowa.exe         → binário GOWA pré-compilado do Windows (não editar; atualizações vão pra storages/bin/)
storages/bin/        → binário GOWA atualizado pelo painel (writable, persiste em Docker/Coolify)
WHATSBOT_VERSION     → versão + histórico de changelog do WhatsBot-Lite que acompanha esta release (fonte única, bumpado pelo /release-up)
server/routes/update.py → detecção, preferências e instalação de updates do WhatsBot-Lite
```

## Base obrigatória do Chat de ajuda

O arquivo [agent/SYSTEM_HELP.md](agent/SYSTEM_HELP.md) é a fonte oficial, em linguagem de usuário final,
para o projeto **WhatsBot-Lite — Ajuda do sistema**. Ele é lido novamente ao construir o agente de cada
mensagem de ajuda, recebe o domínio atual no lugar de `{{base_url}}` e fica no contexto antes de qualquer
consulta por ferramenta. A criação de plugins usa outro prompt e não recebe essa base.

O arquivo [agent/PLUGIN_CREATOR.md](agent/PLUGIN_CREATOR.md) é a referência oficial completa do
Criador de Plugins. Seu conteúdo inteiro é injetado no prompt de cada execução de implementação. O
criador recebe o ID e o inventário concreto do projeto, ferramentas do workspace, terminal e validação.
Também pode consultar exemplos oficiais, plugins instalados e o core em modo somente leitura quando o guia
não cobrir uma dúvida concreta; isso é complementar, não uma fase obrigatória. Toda mudança na API pública,
estrutura, manifesto, telas, events, filters, migrations, testes ou convenções de plugins deve atualizar
essa referência no mesmo trabalho. Ela precisa bastar para a criação comum sem vasculhar o repositório.

**Regra de entrega:** toda criação, alteração, remoção ou mudança de lugar de uma funcionalidade visível
para o usuário deve atualizar `agent/SYSTEM_HELP.md` no mesmo trabalho. Registre o que a funcionalidade
faz, onde encontrá-la, os passos comuns, condições relevantes e o link direto correto. Se uma opção mudar
de nome, tela, aba, âncora ou comportamento, corrija a entrada existente; se for removida, remova também a
orientação obsoleta. Escreva para uma pessoa sem conhecimento técnico e confirme no frontend que a rota e
a âncora documentadas existem.

Quando a base não cobre a pergunta, o agente de ajuda recebe ferramentas somente de leitura para pesquisar
o código e a documentação do core, o código-fonte dos plugins instalados em `storages/plugins/`, configurações
operacionais permitidas, plugins registrados e a estrutura real do banco. Mensagens, contatos, arquivos de
dados dos plugins e credenciais não fazem parte dessa consulta. Depois da investigação, a resposta continua
curta, não técnica e com o link mais específico.

## Comandos

Escolha o launcher pelo ambiente onde está rodando:

| Ambiente | Comando | Modo | Hot-reload | Quando usar |
|---|---|---|---|---|
| Linux dev nativo | `./linux_start.sh` | Python local + uvicorn `--reload` | Sim (core + plugins) | Dia-a-dia de desenvolvimento — edita `.py` e o worker reinicia sozinho |
| macOS dev nativo | `macos_start.command` | Python local + uvicorn `--reload` | Sim (core + plugins) | Dia-a-dia em macOS; baixa Python e o binário GOWA automaticamente na 1ª execução |
| Windows dev nativo | `windows_start.bat` | Python local + uvicorn `--reload` | Sim (core + plugins) | Dia-a-dia em Windows; baixa Python automaticamente na 1ª execução |
| Linux/macOS prod-like | `./docker_start.sh` | `docker compose up --build -d` | Não | Validar o build Docker localmente antes de push pro Coolify |
| Coolify / servidor remoto | `git push` → deploy automático | Container do [Dockerfile](Dockerfile), `CMD python main.py` | Não | Produção — Coolify clona o repo e roda o Dockerfile |

Parar o servidor:
- Linux dev: `Ctrl+C` no terminal do `linux_start.sh` (ou `pkill -f "uvicorn server.dev"` se desanexado)
- macOS dev: `Ctrl+C` na janela do `macos_start.command` (ou rode `macos_stop.command`)
- Windows dev: `windows_stop.bat`
- Docker local: `docker compose down`

Setup inicial (1ª vez no Linux):

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./linux_start.sh
```

O `windows_start.bat` e o `macos_start.command` fazem o setup sozinhos (baixam Python 3.12, criam a venv, instalam as deps; o de macOS também baixa o binário GOWA).

## Banco de dados

A camada de dados usa **SQLAlchemy 2.0 Core** (sem ORM declarativo). Cada tabela é um `Table` em [db/tables.py](db/tables.py) e os repositórios constroem statements via `select()/insert()/update()/delete()`. Repos rodam síncronos e são chamados das rotas via `asyncio.to_thread`.

### Escolha do backend

A URL é resolvida na ordem:

1. Variável de ambiente `DATABASE_URL` (cobre Docker/Coolify — `.env`).
2. Arquivo local `storages/database.json` (Windows / EXE — gerenciado pela UI).
3. Fallback: `sqlite:///storages/whatsbot.db`.

Para trocar para Postgres no Windows: Settings → Banco → cola a URL `postgresql+psycopg://user:senha@host:5432/whatsbot` → "Migrar agora". O endpoint `POST /api/admin/migrate-to-postgres` recebe a URL, valida que o destino está vazio, aplica Alembic, copia tabela a tabela (incluindo `plugin_*`), grava em `database.json` e dispara restart. SQLite original fica preservado para rollback (basta apagar/editar `database.json` e reiniciar).

Para Docker: setar `DATABASE_URL` no `.env` antes de subir o container — o arquivo `database.json` é ignorado quando a env está presente.

**Docker Swarm com múltiplas réplicas (ou rolling update entre tasks): `DATABASE_URL` apontando para Postgres compartilhado é obrigatório.** Volumes nomeados em Swarm são locais por nó, não compartilhados entre réplicas — SQLite local resulta em DBs divergentes (escritas vão pra uma réplica, leituras vêm de outra). Coolify e single-container não sofrem disso.

### Tabelas

| Tabela | Descrição |
|--------|-----------|
| `config` | Configurações do app (key-value, valores JSON-encoded). Configs de plugin usam prefixo `plugin.<id>.` |
| `contacts` | Contatos/grupos (phone, name, email, profissão, empresa, flags). Inclui `is_pinned` (fixar conversa no topo) e `has_unread_mention` (@menção não lida em grupo) |
| `observations` | Notas/observações por contato (texto livre) |
| `messages` | Histórico completo de mensagens (role, content, ts, media). Inclui `revoked` (apagada pra todos), `reactions` (JSON `{emoji: [reactor,...]}`) e `reply_to_msg_id` (msg_id GOWA da mensagem citada) |
| `usage` | Registros de uso da API (tokens, custo, modelo) |
| `tags` | Tags globais (name, color) |
| `contact_tags` | Relação N:N contato ↔ tag |
| `unread_msg_ids` | IDs de mensagens não lidas por contato |
| `executions` | Tracking de execuções (webhook → resposta) |
| `execution_steps` | Passos de cada execução (tool calls, llm_request, etc.) |
| `plugins` | Plugins descobertos no filesystem (id, version, enabled, load_error) |
| `plugin_migrations` | Versões de SQL migrations já aplicadas, por plugin |
| `plugin_<id>_*` | Tabelas criadas por plugins via suas migrations (prefixo obrigatório) |
| `tool_overrides` | Override por-tool (enabled, description, display_label). Row criada automaticamente para cada tool registrada (core + plugin) |

### Configuração SQLite

Quando o engine é SQLite (default), as PRAGMAs são aplicadas via `event.listens_for("connect")` em [db/engine.py](db/engine.py):

- `PRAGMA journal_mode=WAL` — permite leituras concorrentes
- `PRAGMA foreign_keys=ON` — integridade referencial
- `PRAGMA busy_timeout=5000` — espera até 5s em lock contention
- `connect_args={"check_same_thread": False}` — reuso entre threads compatível com `asyncio.to_thread`

Em Postgres essas pragmas não se aplicam (são SQLite-only); o engine usa `pool_pre_ping=True` para sobreviver a quedas idle de conexão.

### Padrão de acesso

Repos usam o padrão dialect-agnóstico baseado em `Table` objects:

```python
from sqlalchemy import select
from db.engine import get_engine
from db.tables import contacts

def get_by_phone(phone: str) -> dict | None:
    with get_engine().connect() as conn:
        row = conn.execute(
            select(contacts).where(contacts.c.phone == phone)
        ).mappings().first()
    return dict(row) if row else None
```

Regras:

- Leitura: `with get_engine().connect() as conn:` (sem transação implícita).
- Escrita: `with get_engine().begin() as conn:` (auto-commit no exit, rollback em exceção).
- UPSERT: usar `db.upsert.upsert()` / `db.upsert.upsert_ignore()` — escolhe `sqlite.insert()` ou `postgresql.insert()` automaticamente.
- Nunca usar `?` ou `%s` direto — bind params nomeados (`:phone`) via `sqlalchemy.text()` ou expressões Core.
- Migrations: Alembic ([db/alembic/versions](db/alembic/versions)). Para um schema change, rode `alembic revision --autogenerate -m "msg"` e revise. `init_db()` aplica `alembic upgrade head` no boot; DBs legados sem `alembic_version` são automaticamente stampados em `0001_baseline` antes do upgrade.

`db.connection.get_db()` ainda existe como shim deprecated retornando `engine.raw_connection()`, mas é apenas para plugins de terceiros não migrados. Código novo (core ou plugin oficial) usa `get_engine()`.

## Fluxo de mensagens (webhook)

Mensagens recebidas no WhatsApp são entregues em tempo real via webhook do GOWA:

1. GOWA inicia com `--webhook http://127.0.0.1:{web_port}/api/webhook`
2. Mensagem chega → GOWA faz POST em `/api/webhook` com payload contendo `body`, `from`, `id`, `is_from_me`
3. Webhook acumula mensagens do mesmo contato por `message_batch_delay` segundos (padrão: 3s) — se o contato enviar várias mensagens em sequência, são juntadas em uma só
4. Após o delay, `_process_batch()` junta os textos com `\n` e chama `agent_handler.process_message()`
5. O AgentHandler faz a chamada ao LLM com tool calling — se o LLM detectar dados pessoais (nome, email, profissão, empresa), chama `save_contact_info` automaticamente
6. Resposta é enviada via `gowa_client.send_message()`

**NÃO usa polling** — o auto-reply por polling foi removido. Toda recepção de mensagens é via webhook.

**Histórico paginado**: `GET /api/contacts/{phone}?limit=N` devolve só a janela mais recente (sem `limit` mantém o
formato antigo, com tudo). A janela é ordenada por `(ts, id)`, e o `_id` da mensagem é o cursor (o `ts` sozinho não
serve: mensagens com o mesmo timestamp seriam puladas). `GET /api/contacts/{phone}/messages` aceita **um** entre
`before`, `after` e `around` (id de mensagem) e devolve `has_more_before`/`has_more_after` e `quoted` (originais de
respostas que ficaram fora da janela). É só leitura: não marca como lida nem cria contato. No frontend
([Contacts.js](web/static/js/components/contacts/Contacts.js)) `contactData.messages` é uma **janela**, não a
conversa: a busca do servidor continua cobrindo o histórico inteiro, e um resultado, uma citação antiga ou o envio de
mensagem chamam `jumpToMessage`/`jumpToLatest`. Enquanto a janela não está no fim (`has_more_after`), mensagens ao vivo
do WebSocket ficam em buffer e entram quando a janela alcança o fim. Respostas de contato antigas são descartadas por
`detailReq` (e o `fetch` anterior é abortado), para uma resposta lenta nunca cair na conversa errada.

## Memória por contato

Cada contato é armazenado na tabela `contacts` com campos normalizados:

- **Info** (name, email, profession, company, address) — colunas diretas na tabela `contacts`
- **Observações** — tabela `observations` (uma linha por observação)
- **Mensagens** — tabela `messages` com colunas `role`, `content`, `ts`, `media_type`, `media_path`, `status`, `msg_id`
- **Usage** — tabela `usage` com tokens, custo e modelo por chamada
- **Tags** — relação N:N via `contact_tags`

`ContactMemory` em `agent/memory.py` é o wrapper que encapsula o acesso via repos. Mensagens são lazy-loaded do DB (não mantidas em memória). Apenas as últimas N (configurável) são enviadas ao LLM.

Info é salva automaticamente via tool calling do LLM e injetada no system prompt. Histórico persiste entre reinícios do app.

## Provider de LLM e onboarding (Techify)

O WhatsBot-Lite usa o **proxy LLM da Techify** (`https://llm.techify.one/api/v1`) como provider — API compatível com OpenRouter/OpenAI, então o cliente OpenAI (`base_url=LLM_API_BASE_URL`) e os endpoints `/models` e `/credits` funcionam sem mudança. As constantes vivem em [config/settings.py](config/settings.py) (`LLM_API_BASE_URL`, `TECHIFY_SERVICE_NUMBER_URL`, `TECHIFY_PROVISION_NUMBER`, `TECHIFY_REQUEST_APIKEY_URL`, `TECHIFY_PROVISION_MESSAGE`), todas com override por env.

**Destino do provisionamento = número + frase, resolvidos CAMPO A CAMPO** ([server/routes/setup.py](server/routes/setup.py) `fetch_provision_target`): `/service_number` (fonte da verdade — rotacionar qualquer um dos dois é editar a resposta dele, servida pela Cloudflare, sem release nem env em cliente nenhum) → env (`TECHIFY_PROVISION_NUMBER` / `TECHIFY_PROVISION_MESSAGE`) → **literal em [config/settings.py](config/settings.py)**, que é a última rede para o endpoint fora do ar → os seams `filter.provisioning.number` / `.message`, que têm a última palavra. ⚠️ Os dois campos são independentes: uma resposta que ainda não traga `message` continua ditando o `phone`, e a frase cai no fallback. ⚠️ A frase é o **gatilho** que o destino reconhece, então trocar o número sem trocar a frase junto entrega um texto que o outro lado ignora **em silêncio** — é por isso que os dois seams existem em par. `None`/`""` em qualquer um dos dois **aborta**: o wizard responde com erro acionável (HTTP 400) e **nada** é enviado — sem materializar o contato e sem armar o polling da chave.

**Wizard de 1ª execução** ([web/static/js/components/SetupWizard.js](web/static/js/components/SetupWizard.js), rota `/wizard`): em 3 passos —
1. **Conectar WhatsApp** (QR; auto-avança ao conectar).
2. **Provisionar chave de API**: o WhatsBot-Lite consulta `/service_number` da Techify — que devolve `{ok, phone, message}` e é a **fonte da verdade do par destino+frase** —, manda essa mensagem ao número retornado pedindo a conta+chave (`POST /api/setup/request-key`), faz polling até a chave chegar (com TTL) e já credita ~US$1. O contato do número de provisionamento tem a IA desativada automaticamente.
3. **Prompt do agente**: o usuário escreve a personalidade da IA. Pode pular o wizard e ir direto pro chat.

O wizard só aparece em instalações ainda não configuradas. A chave é persistida em `config["openrouter_api_key"]` (nome legado).

**Monitor de saldo** ([server/balance_monitor.py](server/balance_monitor.py)): consulta `/credits` do proxy, cacheia o resultado e, após chamadas ao LLM, emite o evento WS `low_balance` quando `remaining < low_balance_threshold` (default US$0,50, configurável; `low_balance_enabled` liga/desliga). O frontend (`LowBalanceModal.js`) abre um modal de recarga apontando para `account_url` (URL da conta Techify, salva junto com `access_token` na config). `GET /api/balance` retorna o snapshot inicial no boot.

## Motor de agente (AGNO)

O loop de raciocínio + tool calling roda no **AGNO** ([agent/agno_engine.py](agent/agno_engine.py)). O motor roda **sempre um `Agent` único** por mensagem. O `AgentHandler` continua dono de TUDO em volta (system prompt + `filter.system_prompt`, montagem do histórico + `filter.llm.messages`, lista de tools + `filter.llm.tools`, eventos `llm.before`/`llm.after`, usage, `track_step`, save da resposta, `split_messages`) e só delega o miolo a `agno_engine.run_async` / `run_sync`.

Pontos-chave da integração:

- **Stateless por requisição**: um `Agent` novo é montado por mensagem, para os closures de tool capturarem o coletor `executed` daquela request sem cross-talk entre contatos concorrentes.
- **WhatsBot-Lite é dono do contexto**: o engine NÃO recebe `db` nem deixa o AGNO montar contexto próprio (`build_context=False`, `add_history_to_context=False`, etc.). O system prompt (já filtrado) vira `system_message`; o histórico (já filtrado) é convertido em `agno.models.message.Message` e passado como `input`.
- **Tools**: cada schema OpenAI registrado é embrulhado num `agno.tools.function.Function` (`skip_entrypoint_processing=True`) cujo entrypoint reaplica `filter.tool.args`/`filter.tool.result` e emite `tool.before`/`tool.after` — mesma semântica do dispatch antigo. Async path usa entrypoint assíncrono; sync path usa síncrono.
- **Usage**: lido de `run_output.metrics` (`RunMetrics.input_tokens/output_tokens`) e gravado via `AgentHandler._record_usage_tokens` (em vez de `response.usage`).
- **Reply**: `_extract_reply` pega a ÚLTIMA mensagem `assistant` sem tool calls de `run_output.messages` (fallback: `run_output.content`). Isso evita que o AGNO concatene um "chatter" pré-tool com a resposta final — crítico com `split_messages` (saída JSON array) ligado.
- **Transcrição/descrição de mídia** continuam em chamadas diretas ao cliente OpenAI no handler (não são agênticas) — o cliente OpenAI segue vivo só para isso e para `test_api_key`.

O motor roda **sempre um `Agent` único**. A base extensível para configurar agentes via banco (prompt/modelo/tools lidos do DB) é a infra `ai_agents` + [agent/agent_factory.py](agent/agent_factory.py), ligada por `ai_engine_enabled` — também single-agent.

## Atualização do WhatsBot-Lite (self-update)

Diferente da atualização do GOWA (binário, com verificação de SHA-256 e rollback automático — ver
próxima seção), o self-update do WhatsBot-Lite em si ([server/routes/update.py](server/routes/update.py))
é mais simples: sobrescreve os arquivos de código com o conteúdo da última release do GitHub.

- **Versão local**: lida de `WHATSBOT_VERSION` na raiz (JSON `{"version", "changelog": [{"version",
  "description"}, ...]}`), nunca de `git describe`. Isso importa porque instalações via "Download ZIP"
  do GitHub (o fluxo mais comum pra quem não usa git) não têm pasta `.git` — `git describe` falharia
  sempre e travaria a versão exibida em `"0.0.0"`. Ler de um arquivo tracked funciona igual em git
  clone, ZIP ou Docker.
- **Download é sempre da tag da release**, nunca do `main` — `POST /api/update` busca a última release,
  pega o `tag_name` e baixa `archive/refs/tags/{tag}.zip`. A branch `main`
  pode estar à frente da última release; o self-update só deve entregar o que já foi publicado como
  estável.
- **`changelog` é uma lista acumulada, mais recente primeiro** (`changelog[0]` é sempre a entrada da
  `version` instalada) — o `/release-up` **adiciona** uma entrada no início a cada release, nunca
  substitui as anteriores. Isso deixa o histórico completo disponível localmente, sem precisar de
  rede, mesmo pra quem pulou várias releases de uma vez. `latest_description` em
  `GET /api/update/check` vem do corpo da última release; se a API do GitHub atingir o limite, o servidor
  resolve a tag pela página pública e lê o changelog do `WHATSBOT_VERSION` dessa tag. O resultado fica
  em cache por cinco minutos para que atualizações de página não consumam o limite da API.
- **Aviso de nova versão**: [app.js](web/static/js/app.js) chama `GET /api/update/check` ao abrir a página
  e ao reconectar o WebSocket. Quando existe uma release estável mais nova, `WhatsNewModal` mostra o
  changelog e oferece três escolhas: **Atualizar agora**, **Agora não** e **Nunca avisar**. “Agora não”
  grava a versão em `whatsbot_skipped_version`; uma release com outro número volta a aparecer. “Nunca
  avisar” desliga `whatsbot_update_notifications_enabled`, que pode ser reativado em Painel → Sistema →
  Atualizar WhatsBot-Lite (`/painel?aba=sistema#update`). Essas preferências ficam no banco da instalação,
  não no navegador.
- **Compatibilidade com versões antigas**: `GET /api/update/local-version` e
  `POST /api/update/popup-seen` continuam disponíveis para frontend em cache. O marcador legado
  `storages/update_popup.json` é apenas removido e não controla mais os avisos.
- **Preserva `storages/statics/logs/venv/.git/bin` e `.env`** — mesma lista de diretórios/arquivos
  protegidos contra sobrescrita usada pelo update do GOWA para `bin/`.
- **Reinício automático**: depois de trocar os arquivos, `POST /api/update` chama
  `plugins.restart.schedule_restart()`. Em Docker/Coolify o processo encerra após a resposta HTTP e
  a política de restart do container o inicia novamente; em desenvolvimento o trigger do Uvicorn
  recarrega o worker. Isso evita servir frontend novo com rotas Python antigas ainda em memória.
- **`/release-up` é responsável por bumpar `WHATSBOT_VERSION`** antes de criar a tag — ver
  [.claude/commands/release-up.md](.claude/commands/release-up.md). Esquecer esse passo deixa o
  self-update reportando uma versão desatualizada mesmo depois de uma release nova.

## Atualização do GOWA

O binário GOWA pode ser atualizado pelo painel em qualquer ambiente (Linux, macOS, Windows, Docker,
Coolify), sem rebuild nem redeploy. A tela fica em Painel → Sistema → card **GOWA (motor do WhatsApp)**
(`/painel?aba=sistema#gowa`); a versão instalada também aparece como chip ao lado do status de conexão
no topo do `/painel`.

### Onde o binário mora (regra fundamental)

**`bin/` é read-only em runtime. `storages/bin/` é o único diretório gravável.**

| Diretório | Papel | Por quê |
|---|---|---|
| `bin/<nome>` | **bundled**: o que a imagem, o launcher ou o repo entregou | No Docker é symlink pra camada da imagem (some no redeploy); no Windows `bin/gowa.exe` é tracked no git (escrever ali sujaria a árvore e conflitaria no `git pull`) |
| `storages/bin/<nome>` | **managed**: instalado pelo painel | `storages/` é gitignored, é volume nomeado no Docker e é preservado pelo self-update do WhatsBot-Lite |

`gowa/binary.py:resolve_binary()` escolhe entre os dois **comparando versões**, não preferindo cegamente
o gerenciado: uma imagem reconstruída com GOWA mais novo ganha de um override velho em `storages/bin`.
A ordem é `WHATSBOT_GOWA_BINARY` (env, escape hatch de dev) → managed (se `>=` bundled) → bundled.

### Fonte única da versão

O arquivo `GOWA_VERSION` na raiz (uma linha, ex. `8.8.0`) é lido pelo `Dockerfile`, pelo
`linux_start.sh`, pelo `macos_start.command` e por `gowa/binary.py`. Os launchers e o Dockerfile gravam
`bin/.gowa_stamp` (gitignored) com a versão efetivamente baixada, e o stamp tem precedência sobre o
`GOWA_VERSION` na hora de dizer o que está em `bin/`. Como a guarda de download compara stamp vs.
`GOWA_VERSION`, bumpar o arquivo faz as máquinas de dev re-baixarem no próximo start.

**Regra de release**: ao bumpar `GOWA_VERSION`, commitar o `bin/gowa.exe` correspondente **no mesmo
commit**. O Windows depende do exe tracked, e a lógica "bundled mais novo ganha" depende dos dois em
sincronia.

### Faixa homologada

`GOWA_SUPPORTED_RANGE` em [gowa/updater.py](gowa/updater.py) (hoje `>=8.8.0,<10.0.0`) define o que o
WhatsBot-Lite foi testado para falar. Versão fora da faixa continua aparecendo, mas marcada como **não
homologada**: exige confirmação em dois cliques e **nunca** dispara o modal proativo na tela principal.
Ao homologar uma versão maior, bumpe a constante junto com o `GOWA_VERSION`.

### Como funciona

- **Verificação**: `gowa_update_check_loop` ([server/background.py](server/background.py)) roda 1×/dia
  (`GOWA_UPDATE_CHECK_INTERVAL`), 120s após o boot. Só verifica; nunca instala sozinho. Achou versão
  nova, homologada e diferente de `gowa_skipped_version` → emite `gowa_update_available` e o
  `GowaUpdateModal` abre em qualquer aba. O `app.js` também faz um seed no boot, pra um painel aberto
  entre duas checagens não perder um release já publicado.
- **Instalação** (`gowa/updater.py:install`): valida a versão contra `^\d+\.\d+\.\d+$` (a URL é
  sempre montada no servidor; aceitar URL do cliente viraria SSRF), baixa o zip para
  `storages/bin/.tmp/`, confere o SHA-256 contra o `checksums.txt` publicado (`checksums-macos.txt` no
  macOS), extrai só o membro `{os}-{arch}`, roda `chmod +x`, remove a quarentena do Gatekeeper no macOS,
  faz um smoke test (`--help`) para pegar arquitetura errada, para o GOWA, troca o binário com
  `os.replace` atômico, religa e espera o health check. Em salto de major, tira antes um snapshot de
  `storages/whatsapp.db*` e `storages/chatstorage.db*` (a sessão do WhatsApp).
- **Rollback**: se o health check falhar em 45s, reverte sozinho e emite `rolled_back`. O botão
  "Reverter para vX" fica disponível enquanto houver binário gerenciado.
- **Concorrência**: `_UPDATE_LOCK` não-bloqueante. Um segundo update simultâneo recebe HTTP 409.
- **Rate limit**: a API do GitHub permite 60 req/h por IP sem token. O `fetch_latest_release` cacheia
  por 1h, usa `If-None-Match` e, ao bater no limite, devolve o cache com `rate_limited: true` em vez de
  erro. `GITHUB_TOKEN` na env é usado se existir.

### Config

| Chave | Default | Descrição |
|---|---|---|
| `gowa_auto_check_enabled` | `True` | Liga a verificação diária. Override por env `WHATSBOT_GOWA_AUTO_CHECK` |
| `gowa_skipped_version` | `""` | Versão que o usuário mandou pular (escrita por `/api/gowa/skip-version`) |
| `gowa_latest_version` | `""` | Última versão vista (escrita pelo servidor) |
| `gowa_last_check_at` | `0.0` | Timestamp da última checagem (escrita pelo servidor) |

Só `gowa_auto_check_enabled` está no `allowed_keys` do `PUT /api/config`; as outras são server-only.

### Proxy de saída (conexão do WhatsApp)

O WebSocket do WhatsApp é discado pelo `whatsmeow` direto, então `HTTP_PROXY`/`HTTPS_PROXY` **não**
valem para ele. O GOWA tem um knob dedicado, disponível a partir da **8.11.0**. A tela fica no mesmo
card **GOWA (motor do WhatsApp)**, em `/painel?aba=sistema#gowa-proxy`.

Dois jeitos de preencher, porque cada serviço entrega de um jeito:

- **IP e porta**: tipo (SOCKS5/HTTP/HTTPS), IP ou host, porta e, quando o serviço exigir, usuário e
  senha. Colar `1.2.3.4:1080` no campo de host separa host e porta sozinho; porta vazia cai no default
  do tipo (1080/8080/443).
- **URL única**: `socks5://usuario:senha@host:1080`. Usuário e senha são opcionais.

Detalhes de implementação:

- **Vai por env var, não por flag**: o [gowa/manager.py](gowa/manager.py) injeta `WHATSAPP_PROXY` no
  `env` do `Popen`. Um GOWA antigo ignora env desconhecida, mas **aborta** com flag desconhecida
  (`--whatsapp-proxy`) e nunca subiria; além disso argv vaza a senha no `ps`. Com GOWA < 8.11.0 o
  painel mostra o aviso e o start loga um warning.
- **Aplicar exige restart**: o proxy só entra no dial da conexão. `PUT /api/gowa/proxy` reinicia o GOWA
  automaticamente, e só quando a URL efetiva realmente mudou.
- **Senha nunca volta em claro**: a API devolve `***`; reenviar `***` (no campo ou dentro da URL)
  significa "mantém a senha salva".
- **Teste de conexão**: `POST /api/gowa/proxy/test` abre um túnel de verdade até `web.whatsapp.com:443`
  (handshake SOCKS5 com auth RFC1929, ou `CONNECT` com `Proxy-Authorization`), tudo em stdlib. Testa o
  que está na tela, então dá pra validar as credenciais antes de salvar.
- **Escape hatch**: `WHATSBOT_GOWA_PROXY` na env tem prioridade sobre o painel (útil em
  Docker/Coolify); nesse caso a UI aparece travada com o aviso.

| Chave | Default | Descrição |
|---|---|---|
| `gowa_proxy_enabled` | `False` | Liga o proxy |
| `gowa_proxy_mode` | `"fields"` | `fields` (IP/porta/usuário/senha) ou `url` (URL única) |
| `gowa_proxy_scheme` | `"socks5"` | `socks5`, `http` ou `https` |
| `gowa_proxy_host` / `gowa_proxy_port` | `""` / `0` | Endereço do proxy no modo `fields` |
| `gowa_proxy_username` / `gowa_proxy_password` | `""` | Credenciais, quando o serviço exigir |
| `gowa_proxy_url` | `""` | URL completa no modo `url` |

Nenhuma dessas chaves está no `allowed_keys` do `PUT /api/config`: a escrita passa só por
`/api/gowa/proxy`, que valida antes de salvar.

**Atenção em Docker Swarm com múltiplas réplicas**: `storages` é volume local por nó, então cada réplica
atualizaria o próprio binário. O update loga um warning nesse caso.

## CSS do Tailwind

O core **não carrega mais o compilador do Tailwind no navegador**. O runtime (`vendor/tailwind.js`) observa o DOM e
reprocessa todas as classes a cada mudança, o que travava a abertura de conversas longas (medido: era o maior
consumo de CPU do JS). No lugar, o `index.html` carrega [tailwind.css](web/static/css/tailwind.css), gerado
antes do commit e **versionado**, então quem instala o WhatsBot-Lite não precisa de Node.

- **Regerar**: `cd tools/css && npm install && npm run build`. O tema vive em
  [tailwind-theme.json](web/static/tailwind-theme.json), lido pelo build e pelo runtime dos plugins.
- **Conferir**: `npm run check` falha se o `tailwind.css` versionado estiver desatualizado. Rode antes de commitar
  qualquer mudança de classe em `web/` ou em `assets/plugin_examples/`. Classes montadas por concatenação
  (`` `bg-${cor}-100` ``) **não são detectadas**: escreva a classe completa, ou ela some do CSS gerado.
- **Ordem no `<head>`**: `custom.css` vem ANTES de `tailwind.css`. O runtime antigo anexava o estilo por último e
  ganhava os empates; manter essa ordem preserva o resultado visual.
- **Plugins**: o build só varre o core e `assets/plugin_examples/`. Plugins instalados depois (Loja, Chat, zip)
  usam classes arbitrárias, então [PluginScreen.js](web/static/js/components/PluginScreen.js) carrega o runtime sob
  demanda (`utils/tailwindRuntime.js`) na primeira tela de plugin aberta. O runtime fica FORA do caminho de
  conversas e do restante do core.

## Navegação do painel

As configurações do core em [ConfigPanel.js](web/static/js/components/ConfigPanel.js) usam três abas:
**Agente** (`?aba=agente`), **Modelos e mídia** (`?aba=modelos-midia`) e **Sistema**
(`?aba=sistema`). Cada configuração visível mantém um `id` próprio para links diretos, combinando a
aba e a âncora, como `/painel?aba=agente#prompt`. `PANEL_TARGET_TABS` também precisa mapear cada nova
âncora para que links antigos no formato `/painel#prompt` continuem abrindo a aba correta.

Ao criar, mover, renomear ou remover uma configuração do core, atualize no mesmo trabalho a lista de
abas, o mapa de âncoras, os links de fallback em `server/routes/chat.py` e a base
`agent/SYSTEM_HELP.md`. Configuração de plugin continua pertencendo à tela do próprio plugin.

O Chat em [Chat.js](web/static/js/components/Chat.js) usa a barra lateral fixa no desktop e um drawer
sobreposto abaixo de 768 px. Nesse modo, a conversa ocupa toda a largura, os arquivos do projeto abrem
sobre o Chat e o compositor distribui opções e ações em duas linhas. Funcionalidades baseadas apenas
em arrastar precisam de uma alternativa por toque; a ordem dos projetos usa setas no mobile. Preserve
essas duas formas de navegação ao alterar projetos, arquivos ou o compositor.

O criador de plugins não usa limite numérico de tool calls do AGNO nem orçamento de consultas: plugins
complexos precisam alternar criação de vários arquivos, validação e correções até terminar. Para evitar
pesquisas repetitivas, o contrato completo de [agent/PLUGIN_CREATOR.md](agent/PLUGIN_CREATOR.md) entra no
system prompt. Referências e shell continuam disponíveis para lacunas e testes concretos. A rota faz até
três rodadas automáticas quando a validação externa reprova, mantém limite total de 30 minutos e envia
eventos `heartbeat` durante pausas do modelo. Não volte a limitar ferramentas por uma contagem genérica.
As operações do `agno.tools.workspace.Workspace` são expostas por `workspace_functions()` em
[agent/plugin_chat.py](agent/plugin_chat.py), com schemas JSON explícitos. Não volte a registrar o toolkit
diretamente: no Agno 2.9 isso pode enviar parâmetros vazios ao modelo, fazendo modelos menores pararem ou
adivinharem argumentos. O wrapper continua usando as proteções de caminho e escritas atômicas do Workspace.
Com nível de raciocínio vazio, o criador usa `none` para `deepseek/deepseek-v4.1-flash` porque esse modelo
vem em `high` e pode gastar toda a saída antes da primeira tool; modelos com raciocínio obrigatório usam
`low`. Uma escolha explícita da pessoa continua prevalecendo. `run_command` aceita somente caminhos relativos
ao workspace: diagnóstico não pode vasculhar executáveis, ambientes virtuais ou arquivos do host.
O runner simples de `test_*.py` também roda em processo e banco temporários; nunca execute testes gerados
contra o banco real, pois repetições de validação deixariam dados e causariam falhas falsas.
Depois de uma validação bem-sucedida, a rota persiste uma mensagem `kind=install_offer`; não deixe a oferta
somente no stream, porque `openConversation()` recarrega o histórico ao terminar a execução. O frontend
reconstrói o card **Sim, instalar** a partir da oferta pendente. O clique no card e frases explícitas como
“instale por favor” chamam o instalador determinístico do backend, sem delegar a instalação ao LLM. Ao
concluir, a oferta é marcada como `installed`/`updated`, a mensagem de sucesso é persistida e o reinício é
agendado. Não oriente a pessoa a procurar um plugin ainda não instalado na página Plugins.
O streaming HTTP é apenas uma assinatura visual. `POST .../messages` cria uma task em
`_background_chat_tasks`, e essa task consome o agente e persiste ações/resposta mesmo se o navegador
desconectar. Nunca volte a cancelar o agente por `request.is_disconnected()`. A execução ativa fica em
`_active_conversation_runs`; `GET .../activity` permite que uma conversa reaberta recupere mensagens e
estado por polling. Fechar/trocar a tela deve abortar somente o `fetch` do frontend. Cancelamento explícito
pelo botão continua usando `POST /api/chat/runs/{run_id}/cancel`. Esse estado ativo é por processo: um
reinício do WhatsBot-Lite encerra tarefas em curso, mas fechar o navegador não.

## Fotos de perfil (avatars)

[server/avatars.py](server/avatars.py) cacheia as fotos de perfil em disco em `statics/avatars/<phone>.jpg` (servidas pelo mount estático). Como o WhatsApp não emite evento de "foto mudou", a atualização é por re-fetch do GOWA (ao abrir a conversa e numa varredura periódica de fundo — `AVATAR_REFRESH_INTERVAL = 1800s` em [server/background.py](server/background.py)), sobrescrevendo o arquivo só quando os bytes diferem. O frontend faz cache-bust pelo mtime (`avatar_v`); uma mudança dispara o WS `avatar_updated` `{phone, v}` pra atualizar ao vivo sem reload.

## @menções em grupos

[agent/group_mentions.py](agent/group_mentions.py) é o serviço central que conhece os participantes de um grupo e converte menções entre o formato de fio do WhatsApp (`@<número>`) e nomes humanos:

- **Entrada**: `resolve_incoming()` troca `@<dígitos>` numa mensagem recebida por `@<Nome>` (o painel/LLM veem nomes, não números).
- **Saída**: `resolve_outgoing()` transforma `@Nome` / `@todos` (escritos pelo operador ou pela IA) em menção real — `@<número>` inline no texto + a lista `mentions` que o `/send/message` do GOWA aceita. `@todos`/`@geral`/`@all`/etc. viram `@everyone`.

Nomes não vêm do GOWA (`DisplayName` volta vazio): são resolvidos de contatos salvos → pushName capturado de mensagens recebidas → catálogo do device (`/user/my/contacts`) → `/user/info` (cap de 20 lookups por chamada). Participantes são indexados por dígitos do phone **e** do `lid`. Cache de membros por grupo (TTL 300s), invalidado em mudança de roster (join/leave/promote/demote, via webhook `group.participants_changed`). O serviço é inicializado em `create_app` (`group_mentions.init(gowa_client)`) e a identidade do bot é registrada via `set_bot_identity`. A config `group_reply_mode` (default `mention_only`) controla quando a IA responde em grupos.

**Remetente clicável**: no `ContactDetail`, o nome do remetente numa mensagem de grupo vira `<button>` quando casa por nome exato com um membro do roster (`members`) cujo `phone` começa com `55` — o fluxo de iniciar conversa prefixa `55` a qualquer número, então DDI estrangeiro seria corrompido; esses ficam texto. O clique chama `onOpenParticipant` → `Contacts.handleOpenParticipant`: `GET /api/contacts/lookup`; contato existente abre direto, inexistente (ou erro no lookup) passa pelo `StartChatWarningModal` (o aviso de banimento não pode ser contornado). Sem `onOpenParticipant` (sandbox) o rótulo segue texto.

## API REST do WhatsBot-Lite (backend FastAPI)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| GET | `/` | Serve o frontend (web/index.html) |
| GET | `/wizard` | Serve o frontend forçando o wizard de 1ª execução (onboarding) |
| GET | `/api/config` | Retorna config (API key mascarada) + `account_url` |
| PUT | `/api/config` | Salva config + atualiza AgentHandler |
| POST | `/api/config/test-key` | Testa API key no proxy Techify (compatível OpenRouter); auto-salva se válida |
| POST | `/api/config/request-apikey` | Provisiona uma chave via Techify (manda msg ao número de provisionamento; usado pelo wizard) |
| GET | `/api/models` | Lista de modelos do proxy (cache 10 min). Busca `/models/user` (já sem os modelos bloqueados nos guardrails da OpenRouter, então editar o bloqueio lá basta) e cai em `/models` se o proxy não expuser essa rota |
| GET | `/api/balance` | Saldo de crédito atual + threshold + `account_url` (recarga). Updates live via WS `low_balance` |
| GET | `/api/status` | Status de conexão + contagem de msgs |
| GET | `/api/qr` | QR code como PNG (204 se indisponível) |
| POST | `/api/whatsapp/reconnect` | Reconectar GOWA |
| POST | `/api/whatsapp/logout` | Logout GOWA |
| POST | `/api/webhook` | Recebe mensagens do GOWA (webhook) |
| GET | `/api/contacts?archived=true` | Lista apenas contatos/grupos arquivados |
| GET | `/api/contacts/unread-count` | Total de mensagens não lidas (badge global) |
| GET | `/api/contacts/lookup?phone=` | Diz se o número já é contato (`{exists, contact}`) **sem criar nada** (ao contrário de `check-phone` e `GET /{phone}`). Normaliza igual ao `check-phone` (`_normalize_br_phone`) e aceita as variantes BR de 12/13 dígitos. Declarado antes de `/{phone}` |
| POST | `/api/contacts/send-self-link` | Envia o link `wa.me` do número pesquisado para o próprio WhatsApp conectado (alternativa segura a iniciar conversa pela API não oficial) |
| POST | `/api/contacts/{phone}/pin` | Fixa/desafixa a conversa (`{pinned}`). Fixadas vão pro topo da lista. WS `contact_pinned` |
| POST | `/api/contacts/{phone}/unread` | Marca a conversa como não lida (manual) |
| POST | `/api/contacts/mark-all-read` | Zera não lidas de todas as conversas |
| POST | `/api/contacts/mark-all-unread` | Marca todas as conversas como não lidas |
| POST | `/api/contacts/{phone}/messages/react` | Reage a uma mensagem com emoji (string vazia remove). WS `message_reaction` |
| POST | `/api/contacts/{phone}/messages/delete` | Apaga mensagem (revoke pra todos). WS `message_revoked`/`message_deleted` |
| GET | `/api/contacts/{phone}/members` | Lista participantes do grupo com nomes resolvidos (autocomplete de @menção) |
| GET | `/api/webhook-payloads?limit=N` | Últimos N payloads raw do webhook (debug, max 50) |
| GET | `/api/gowa-logs?limit=N` | Tail do `logs/gowa.log` (stdout/stderr do subprocess GOWA, só populado com `WHATSBOT_GOWA_DEBUG=1`) |
| GET | `/api/tools` | Lista todas as tools registradas (core + plugin) com estado de override |
| PUT | `/api/tools/{name}` | Atualiza override `{enabled?, description?, display_label?}`; `description=null` reseta |
| GET | `/api/plugins` | Lista todos os plugins descobertos com status (ativo/inativo/erro) |
| GET | `/api/plugins/manifest` | Manifest público dos plugins ativos (pro frontend dinâmico) |
| POST | `/api/plugins/{id}/enable` | Ativa o plugin e dispara restart |
| POST | `/api/plugins/{id}/disable` | Desativa o plugin e dispara restart |
| GET/PUT | `/api/plugins/{id}/settings` | Schema Pydantic + values do plugin (settings declarativas) |
| GET | `/api/plugins/{id}/export` | Baixa o plugin como `.zip` |
| POST | `/api/plugins/import` | Importa um plugin via upload de `.zip` |
| DELETE | `/api/plugins/{id}` | Remove a pasta + tabelas `plugin_<id>_*` + settings namespaceadas |
| POST | `/api/plugins/restart` | Restart manual do servidor |
| GET | `/api/update/check` | Compara a versão local com a última release e retorna `update_available`, `should_notify`, changelog e preferências; `?force=true` ignora o cache |
| GET | `/api/update/local-version` | Lê a versão e o histórico local; mantido para compatibilidade com frontends antigos |
| POST | `/api/update/popup-seen` | Limpa o marcador legado do popup pós-update; mantido para compatibilidade |
| POST | `/api/update/skip-version` | `{version}` — oculta o aviso somente para essa release; a próxima volta a aparecer |
| POST | `/api/update` | Baixa o `.zip` da **tag** da última release (não o `main`), sobrescreve os arquivos preservando `storages/statics/logs/venv/.git/bin` e `.env`, e reinicia o processo automaticamente |
| GET | `/api/gowa/version` | Versão do GOWA em uso, origem (`bundled`/`managed`/`env`), se há backup pra reverter |
| GET | `/api/gowa/update/check?force=1` | Consulta a última release no GitHub (cache 1h) + se está na faixa homologada |
| POST | `/api/gowa/update` | Baixa, verifica o SHA-256, troca o binário e reinicia. Body `{version?, force_unsupported?}`. 409 se já houver update rodando |
| POST | `/api/gowa/rollback` | Restaura a versão anterior do GOWA |
| POST | `/api/gowa/skip-version` | `{version}` — não avisa mais nessa versão específica |
| GET | `/api/gowa/proxy` | Config do proxy de saída (senha mascarada) + se o GOWA instalado suporta |
| PUT | `/api/gowa/proxy` | Salva o proxy e reinicia o GOWA quando a URL efetiva muda |
| POST | `/api/gowa/proxy/test` | Testa o proxy de verdade (handshake SOCKS5 ou HTTP CONNECT até `web.whatsapp.com:443`) |
| `*` | `/api/plugins/{id}/*` | Endpoints REST mountados pelo plugin (router próprio) |
| GET | `/api/admin/database` | Info do backend atual (dialect, URL redacted, caminho do config) |
| POST | `/api/admin/migrate-to-postgres` | Inicia migração SQLite → Postgres. Body: `{postgres_url}`. Status via WS `db_migration_progress` |
| GET | `/api/admin/migrate-to-postgres/status` | Snapshot polling do estado da migração |
| WS | `/ws` | WebSocket para eventos real-time |

Formato de resposta REST: `{"ok": bool, "data": ..., "error": ...}`

Eventos WebSocket (frontend): `{"event": "...", "data": {...}}` — inclui `status`, `qr_update`, `gowa_status`, `config_saved`, `new_message`, `message_reaction`, `message_revoked`, `message_deleted`, `contact_pinned`, `group_participants_changed`, `avatar_updated` (`{phone, v}` — `v` = mtime do arquivo, usado pra cache-bust da foto), `low_balance` (saldo abaixo do threshold → abre o modal de recarga),
`gowa_update_available` (nova versão do GOWA → abre o modal de atualização), `gowa_update_progress`
(`{phase, progress, version}`, `phase ∈ {downloading, verifying, installing, restarting, health_check, rollback}`),
`gowa_update_done` (`{ok, version, previous_version, rolled_back, message}`).

## GOWA REST API (endpoints reais — v8.8.0 multi-device)

IMPORTANTE: O GOWA v8.8.0 é multi-device. Antes de usar qualquer endpoint, é necessário criar um device via `POST /devices`. Após criação, todas as requests (exceto `/devices`) exigem header `X-Device-Id`.

| Operação | Método | Endpoint | Notas |
|---|---|---|---|
| Listar devices | GET | `/devices` | Sem header obrigatório |
| Criar device | POST | `/devices` body: `{device_id?}` | Sem header, retorna device_id |
| Login/QR | GET | `/app/login` | Retorna JSON com `results.qr_link` (URL do PNG) |
| Status | GET | `/app/status` | Retorna `results.is_connected`, `results.is_logged_in` |
| Logout | GET | `/app/logout` | |
| Reconectar | GET | `/app/reconnect` | |
| Enviar msg | POST | `/send/message` body: `{phone, message, mentions?, reply_message_id?}` | `mentions`: lista de números (ou `@everyone`); `reply_message_id`: citar/responder |
| Revogar msg | POST | `/message/{id}/revoke` | Apagar mensagem pra todos |
| Reagir | POST | `/message/{id}/reaction` body: `{phone, emoji}` | Emoji vazio remove a reação |
| Listar chats | GET | `/chats?limit=N` | Resposta aninhada: `results.data[]` |
| Msgs do chat | GET | `/chat/{jid}/messages?limit=N` | Resposta aninhada: `results.data[]` |
| Info de grupo | GET | `/group/info?group_id={jid}` | Participantes (phone/lid/admin) — usado por `group_mentions` |
| Info de usuário | GET | `/user/info?phone={jid}` | pushName ("default name") — só business retorna |
| Contatos do device | GET | `/user/my/contacts` | Catálogo do celular (digits → nome salvo) |
| Foto de perfil | GET | `/user/avatar` | Bytes do avatar (cacheado em `statics/avatars/`) |

Binário iniciado com: `gowa.exe rest --port 3000 --webhook http://127.0.0.1:{web_port}/api/webhook`

Campos do payload do webhook GOWA: `body`, `from`, `sender_jid`, `chat_id`, `id`, `is_from_me`, `timestamp`, `from_name`

## Convenções de código

- Python com type hints nas assinaturas de função
- Logging via `logging` stdlib (nunca print)
- Operações bloqueantes (GOWA, LLM/proxy Techify, SQLite) usam `asyncio.to_thread()` no backend FastAPI
- Nomes de variáveis e comentários em inglês; textos exibidos ao usuário em português BR
- Tratar respostas da API GOWA com fallback para nomes de campo alternativos (a API não é 100% consistente nos nomes)
- Frontend: ES modules, componentes Preact em PascalCase, services/hooks em camelCase
- **Tools do LLM (core)**: criar em `agent/tools/<name>.py` com (a) o schema dict (`<NAME>_TOOL = {"type": "function", ...}`) e (b) função `execute(ctx, args) -> str | None`. Adicionar a tupla `(SCHEMA, execute)` em `CORE_TOOLS` em `agent/tools/__init__.py`. O dispatch é genérico via registry em `AgentHandler` — nunca adicionar `if/elif` por nome de tool
- **Tools de plugin**: viver em `storages/plugins/<id>/tools.py` no formato `CORE_TOOLS = [(schema, executor), ...]` e ser declaradas no manifest. NÃO mexer em `agent/tools/` ou no handler
- **Contrato de tool (core OU plugin)**: toda tool registrada vira row em `tool_overrides` automaticamente (via `tool_override_repo.ensure` no `_register_tool`). O usuário pode customizar `description` e `display_label` na tela `/tools`. O `name` da tool é IDENTIDADE e NÃO deve ser renomeado depois de release — quebra histórico de `usage` (`call_type=<name>`) e overrides do usuário. Description em código é o **default**: escreva como instrução clara pro LLM, deve funcionar sem customização. O schema também aceita `"display_label": "..."` no dict raiz (fora de `function`) — o handler retira antes de mandar pro LLM, e o valor vira o default mostrado na UI
- **Acesso a dados**: sempre via SQLAlchemy Core. Repos em `db/repositories/` usam `with get_engine().begin() as conn:` + statements de `db/tables`. Nunca usar `sqlite3` diretamente. Plugins acessam o banco via `from plugins.context import make_plugin_db` + `from sqlalchemy import text`

## Tema e modo escuro (legibilidade)

O painel suporta **modo claro e escuro**. O tema é a classe `.dark` no `<html>` (toggle no menu da engrenagem → "Modo escuro", persistido em `localStorage["whatsbot_theme"]`; um script inline no `<head>` do `web/index.html` aplica antes do 1º paint pra não piscar). As cores são dirigidas por **variáveis CSS (canais RGB)** em [web/static/css/custom.css](web/static/css/custom.css): a paleta `wa-*` do Tailwind (`bg-wa-panel`, `text-wa-text`, `border-wa-border`, …) resolve para `rgb(var(--wa-*) / <alpha-value>)` (config em `web/index.html`), então alternar a classe re-tematiza o app inteiro e os modificadores de opacidade (`bg-wa-teal/10`) continuam funcionando.

**REGRA — ao adicionar QUALQUER área nova (tela core, card, modal, tela de plugin), garanta que as cores sejam legíveis no modo escuro.** Na prática:

- **Prefira as classes semânticas `wa-*`** para superfícies/textos/bordas (`bg-wa-bg`, `bg-wa-panel`, `text-wa-text`, `text-wa-secondary`, `border-wa-border`, `bg-wa-hover`, `bg-wa-teal`). Elas trocam de cor sozinhas nos dois temas — é o caminho recomendado e à prova de futuro.
- **Não dependa de cores cruas do Tailwind** (`bg-white`, `text-gray-*`, `bg-green-50`…) nem do fundo padrão do navegador em inputs. Como rede de segurança, `custom.css` tem overrides `html.dark` que re-tematizam as cruas mais comuns (brancos, cinzas `50–300`, e as tintas de acento green/red/amber/yellow/blue/orange/purple/pink em `-50/100/200` + textos `-600/700/800`). Isso é **fallback**, não substitui usar `wa-*` — cores fora dessa lista (ex.: um hex inline, um `bg-*-300` de fundo, uma cor nova) NÃO são cobertas e ficarão ilegíveis.
- **Campos de formulário**: use a classe `.wa-field` (fundo cinza + texto preto, legível nos dois temas) em `<input>`/`<textarea>`/`<select>`. Deixar sem cor de fundo cai no branco padrão do navegador + texto claro do tema = ilegível.
- **Controles nativos** (date/time/range/checkbox/scrollbar) seguem o tema via `color-scheme` (já setado em `:root`/`html.dark`).
- **Acentos** (`text-white` em botão colorido, vermelho de "excluir") podem ficar como estão.
- **Sempre teste**: abra a tela, ligue o modo escuro e confira o contraste. Se uma cor crua não estiver coberta, ou troque por `wa-*`/`.wa-field`, ou adicione o override `html.dark` correspondente em `custom.css`.

Telas de plugin (`storages/plugins/<id>/static/*.js`) seguem as MESMAS regras — usam o mesmo runtime do Tailwind e o mesmo `custom.css`.

## Dados do projeto

Tudo salvo na pasta raiz do projeto (dev) ou junto ao EXE (PyInstaller):
- `storages/whatsbot.db` — banco SQLite (default; configs, contatos, mensagens, usage, tags)
- `storages/database.json` — override do backend (`{"url": "postgresql+psycopg://..."}`); ausente = SQLite
- `storages/` — dados do GOWA (sessão WhatsApp) + banco de dados da aplicação
- `logs/` — logs com rotação
- `statics/senditems/` — mídia enviada pelo operador
- **Webhook payloads (debug)**: últimos 50 payloads raw do GOWA em memória, acessíveis via `GET /api/webhook-payloads`
- **Contatos arquivados**: ao receber mensagem de um contato, o webhook consulta `gowa_client.is_chat_archived(jid)` e persiste `is_archived` na tabela `contacts`. A sidebar filtra por `?archived=true/false`. O status de archive é atualizado on-demand (não por polling)

## Sistema de plugins

Plugins são extensões opcionais isoladas em `storages/plugins/<id>/` (volume Docker / pasta separada no Windows, ignorada por updates). Um plugin pode agregar:

- **Tools** para o agente LLM (registradas no mesmo registry das tools core)
- **Prompt fragments** injetados dinamicamente no system prompt
- **Endpoints REST** sob `/api/plugins/<id>/...`
- **Tela Preact** carregada via `import()` ES dinâmico
- **Migrations SQL** com prefixo `plugin_<id>_` obrigatório
- **Settings declarativas** via Pydantic (form auto-gerado pela UI)
- **Broadcast WebSocket** via `from plugins.context import broadcast; broadcast("evento", {...})` — thread-safe, fire-and-forget, ws_manager + loop são injetados no startup do server. Use pra empurrar atualizações em tempo real à tela do plugin (a tela escuta `/ws` e filtra pelo nome do evento).

### Layout de um plugin

```
storages/plugins/<id>/
├── plugin.yaml              # manifest (id, name, version, whatsbot_api_version, entry, screens, frontend_extends?)
├── __init__.py
├── tools.py                 # CORE_TOOLS = [(schema, executor), ...]   (opcional)
├── prompts.py               # PROMPT_FRAGMENTS = [callable, ...]        (opcional)
├── routes.py                # router = APIRouter()                       (opcional)
├── settings.py              # class Settings(BaseModel) — Pydantic       (opcional)
│                            #   (config do plugin: settings OU screen config:true)
├── migrations/
│   └── 001_initial.sql      # tabelas com prefixo plugin_<id>_
└── static/
    └── <id>.js              # default-export componente Preact
```

### Lifecycle

1. **Bootstrap**: na 1ª execução, `plugins.loader.bootstrap_initial_plugins()` copia `assets/plugin_examples/*` para `storages/plugins/` se a pasta estiver vazia (Windows e Docker).
2. **Discovery**: `discover_and_load(plugins_dir)` escaneia o filesystem, parseia cada manifest, faz `upsert` na tabela `plugins`.
3. **Migrations**: para plugins com `enabled=1`, `run_pending_migrations` aplica SQL files em ordem numérica. Naming `NNN_descricao.sql`. O migrator valida regex que toda `CREATE TABLE`/`ALTER TABLE`/`CREATE INDEX` use prefixo `plugin_<id>_`. Antes de executar, o migrator traduz o idiom SQLite de chave auto-incremento (`id INTEGER PRIMARY KEY AUTOINCREMENT`) para o equivalente do dialeto: em Postgres vira `INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` (`_adapt_sql` em [plugins/migrator.py](plugins/migrator.py)); em SQLite passa intacto. Assim plugins escritos no estilo SQLite (o exemplo `lembretes` + os da Loja) rodam também em Postgres sem reescrever o `.sql`.
4. **Dependências pip**: antes do import, `_ensure_plugin_deps` ([plugins/loader.py](plugins/loader.py)) instala as `dependencies` declaradas no manifest via `pkg_deps.ensure_pip_deps` (cache em `plugins.installed_deps` — pip só roda quando o conjunto muda). Em seguida, [plugins/dep_check.py](plugins/dep_check.py) escaneia os `import` do plugin (AST) e **auto-instala dependências de terceiros usadas mas não declaradas** (resolve nome-do-import → pacote-pip por mapa curado, ex.: `google`→`google-auth`; senão assume `import foo`→`pip install foo`). Libs do host (fecho transitivo do `requirements.txt`, ex.: `pydantic`, `fastapi`, `sqlalchemy`, `httpx`) nunca são flagadas — o plugin as usa sem declarar. Se ainda faltar um módulo no import, o `load_error` vira mensagem acionável nomeando o pacote provável (nunca mais o `ModuleNotFoundError` cru).
5. **Import**: `importlib.spec_from_file_location` registra o pacote como `whatsbot_plugins.<id>`. Submódulos declarados no `entry:` são importados sob demanda.
6. **Wiring**: `agent_handler.register_plugin_tools/prompts` adicionam ao registry. `app.include_router` monta o router em `/api/plugins/<id>`. `app.mount` serve `static/` em `/plugins/<id>/static`. `screens[].path` é registrado como rota SPA dinâmica.
7. **Toggle**: enable/disable atualiza a tabela `plugins` e dispara `schedule_restart` (`os._exit(0)` após delay; supervisor relança — Coolify/Docker `restart: unless-stopped` ou launcher do EXE).

### Settings declarativas (Pydantic Valves)

Plugin declara `class Settings(BaseModel)` em `settings.py`. O endpoint `GET /api/plugins/<id>/settings` retorna `model_json_schema()` + valores atuais; `PUT` valida via Pydantic e persiste em `config_repo` com prefixo `plugin.<id>.<field>`. Frontend (`PluginSettingsForm.js`) renderiza form genérico para string/int/float/bool/enum.

### Onde fica a configuração de um plugin (REGRA)

**Toda configuração de um plugin vive na aba de configuração DO PRÓPRIO plugin** — o botão **Configurar** no card em *Gerenciar Plugins* (`/plugins`). **Nunca** adicione uma seção/aba nova ao painel de Configurações padrão do WhatsBot-Lite ([web/static/js/components/ConfigPanel.js](web/static/js/components/ConfigPanel.js)) para algo que pertence a um plugin. O core não deve crescer com opções de plugin.

Há dois jeitos (escolha um, ou combine) de preencher o modal "Configurar":

1. **Settings declarativas** (`settings.py` → `class Settings(BaseModel)`): form auto-gerado pelo `PluginSettingsForm`. Use quando as opções são campos simples (string/int/float/bool/enum) persistidos no servidor (`plugin.<id>.<field>`).
2. **Tela de configuração custom** (`screen` com `config: true`): um componente Preact próprio, renderizado dentro do mesmo modal "Configurar" via `PluginScreen`. Use quando precisa de UI rica (toggles que aplicam na hora, preferências em `localStorage` per-device, upload, preview de som, etc.). Quando o plugin tem uma screen `config: true`, o modal renderiza ela **no lugar** do form declarativo.

Referências (na Loja de Plugins, ver "Plugins de exemplo"): `auto_signature` (settings declarativas), `custom_sounds` e `notifications` (screen `config: true`).

### Frontend dinâmico

`/api/plugins/manifest` retorna apenas plugins carregados com seus `screens[]`. `app.js` faz fetch no boot e separa as screens por flag `config`:

- **`config: false`** (default) — screen "de funcionalidade": aparece como página no `GearMenu` (menu da engrenagem) e é renderizada full-page via `PluginScreen`. Ex: uma tela de listagem/operação do plugin.
- **`config: true`** — screen "de configuração": **filtrada fora do GearMenu** (`app.js` faz `.filter(s => !s.config)`) e renderizada dentro do modal **Configurar** do card em `/plugins` (`PluginsManager.js`). É a aba de configuração do próprio plugin.

`PluginScreen` faz `import(screen.component)` dinâmico e passa `apiBase = "/api/plugins/<id>"` como prop. Importmap em `web/index.html` cobre `preact`, `preact/hooks`, `htm` — plugin usa os mesmos sem bundle. Screen custom pode importar utilitários do core por URL absoluta (ex: `import { playNotificationSound } from '/static/js/utils/notifications.js'`).

**Extensão de UI do core por slots (API 1.3)**: além de screens próprias, um plugin pode preencher pontos
nomeados da interface do core. O manifest declara `frontend_extends: /plugins/<id>/static/<arquivo>.js` (o
caminho PRECISA começar com `/plugins/<id>/static/`, mesma regra do `component` das screens; o parser
recusa o resto). `/api/plugins/manifest` devolve o campo e o `app.js` chama `loadPluginExtensions()`
([web/static/js/plugins/slots.js](web/static/js/plugins/slots.js)), que faz `import()` do módulo uma vez por
carga de página e chama o `default export` com `{pluginId, apiBase, addSlot}`. O plugin registra um
componente Preact com `addSlot(nome, Componente)`. Slot disponível hoje: `chat.header.actions` — barra de
cabeçalho da conversa em `ContactDetail`, fora do sandbox; o componente recebe
`{phone, contact, info, isGroup, rawName}` como props. Slot vazio não renderiza nada (instalação sem plugins
fica idêntica); módulo que falha ao carregar é descartado com `console.warn`, e cada componente tem seu
próprio error boundary, então um plugin quebrado nunca derruba a conversa. Para criar um slot novo no core,
renderize `<Slot name="..." ctx=${{...}} />` no ponto desejado e documente o nome e o `ctx` aqui e em
[agent/PLUGIN_CREATOR.md](agent/PLUGIN_CREATOR.md).

Telas de plugin recebem toda a largura útil do painel. O componente decide apenas a organização interna;
não volte a limitar o wrapper compartilhado com `max-w-5xl`. A referência [agent/PLUGIN_CREATOR.md](agent/PLUGIN_CREATOR.md)
contém o contrato visual genérico usado por modelos baratos e o validador do Chat confere largura,
responsividade, tema, campos, foco, estados de consulta, contraste de cores hex mensurável e formulários
repetidos compactos (por exemplo, nota editável só sob demanda). Esse contrato define qualidade sem copiar
a tela de um plugin específico.

### Convenções obrigatórias

- **`id`**: snake_case, regex `^[a-z][a-z0-9_]{0,31}$`. Vira o prefixo de tabela e o nome do pacote Python.
- **Tabelas**: SEMPRE `plugin_<id>_<nome>`. O migrator rejeita o contrário com erro claro.
- **`whatsbot_api_version`**: range semver no manifest (ex: `">=1.0,<2.0"`). Versão atual em `plugins/manifest.WHATSBOT_API_VERSION` — hoje **`1.3.0`**. A 1.3 adiciona `frontend_extends` no manifest (slots de UI do core, ver "Frontend dinâmico"); plugin que use declara `">=1.3,<2.0"`. A 1.2 adiciona `plugins.context.send_whatsapp_message()` e `get_plugin_setting()`; plugin que use esses helpers declara `">=1.2,<2.0"`. Os filters de provisioning continuam disponíveis desde 1.1.
- **Permissions**: declaradas no manifest mas **não enforced no MVP** — informativo apenas.
- **Configuração no próprio plugin**: opções de um plugin vão SEMPRE na aba de configuração dele (settings declarativas e/ou screen `config: true`), NUNCA numa aba nova do painel de Configurações do core. Ver "Onde fica a configuração de um plugin".
- **Settings**: chaves persistem com prefixo `plugin.<id>.`. Plugin nunca grava direto na tabela `config` sem esse prefixo.
- **Cores / modo escuro**: a tela do plugin (`static/<id>.js`) DEVE ser legível no tema escuro. Use as classes semânticas `wa-*` (`bg-wa-bg`, `bg-wa-panel`, `text-wa-text`, `border-wa-border`, …) e `.wa-field` em inputs. Cores cruas (`bg-white`, `bg-green-50`, …) têm fallback no `custom.css`, mas hex inline e cores fora da lista coberta NÃO — teste com o modo escuro ligado. Ver "Tema e modo escuro (legibilidade)".

### Events e Filters (bus do plugin)

Plugins podem reagir a tudo que acontece no WhatsBot-Lite e modificar dados em trânsito sem editar o core. Dois mecanismos complementares (padrão WordPress: actions + filters; referências validadas em Baileys / WAHA / Home Assistant):

- **Events** — broadcast fire-and-forget, paralelo. Plugin exporta `EVENT_HANDLERS` em `<plugin>/events.py` e declara `entry.events: events` no manifest. Não bloqueia o pipeline principal; exceção em um handler nunca afeta outros.
- **Filters** — interceptive, síncrono no pipeline. Plugin exporta `FILTERS` em `<plugin>/filters.py` e declara `entry.filters: filters` no manifest. Recebe `(ctx, value)` e retorna valor modificado ou `None` pra abortar a ação envolvida. Exceção em um filter é isolada (loga + valor passa intacto ao próximo).

Toggle do plugin = tudo-ou-nada: enable liga handlers e filters; disable derruba ambos no próximo restart.

**Eventos GOWA / mensagem** (cobre tudo que o webhook GOWA emite):

| Evento | Quando dispara | Payload chave |
|--------|---------------|---------------|
| `message.received` | Inbound user msg (inclui group sem @mention). **Emitido ANTES do save** — listener que precisa ler do DB deve usar `message.saved` | `phone, name, text, raw_text, msg_id, media_type, media_path, media_extras, is_group, group_jid, individual_phone, raw` |
| `message.saved` | **NOVO** — emitido DEPOIS do INSERT no DB, em todos os 3 sites de save inbound (text batch, media batch, group_no_mention) | `phone, text, msg_id, media_type, media_path, media_extras, is_group, group_jid, source` — `source ∈ {batch_text, batch_media, group_no_mention}` |
| `message.sent` | Resposta IA, operator send, image/audio panel, retry, private @ia, echo do próprio celular | `phone, text, msg_id, media_type, media_path, media_extras, source, status` — `source ∈ {ai, operator, private_ai, retry, echo}` |
| `message.any` *(alias)* | Re-dispatch de `received` + `sent` com `direction: "in"\|"out"` | igual ao original + `direction` |
| `message.reaction` | Reação emoji em mensagem | `id, phone, reaction, reacted_message_id, is_from_me` |
| `message.edited` | Mensagem editada | `id, phone, original_message_id, body` |
| `message.revoked` | Mensagem apagada pra todos | `id, phone, revoked_message_id, revoked_from_me, revoked_chat` |
| `message.deleted` | Mensagem deletada do histórico | `deleted_message_id, original_content, original_sender, was_from_me` |
| `presence.changed` | Digitando / gravando | `phone, state` (`composing`/`paused`), `media` (`text`/`audio`) |
| `receipt.changed` | Ack delivered/read | `phone, msg_ids, status` |
| `group.participants_changed` | Join/leave/promote/demote | `chat_id, phone, type, jids` |
| `group.joined` | Bot adicionado ao grupo | `chat_id, phone` |
| `call.received` | Chamada recebida (offer) | `call_id, phone, auto_rejected` |
| `newsletter.event` | Eventos de newsletter | `subtype, raw` |
| `chat.archived` | Arquivamento detectado no GOWA | `phone, archived` |
| `connection.changed` | GOWA connect/disconnect/QR | `is_connected, is_logged_in, qr_required` |

**Eventos internos**:

| Evento | Source |
|--------|--------|
| `llm.before` / `llm.after` | `aprocess_message`/`process_message` antes/depois de `chat.completions.create`. `after`: `reply, tool_calls, usage, latency_ms` |
| `tool.before` / `tool.after` | `_dispatch_tool`. `after`: `result, error, latency_ms` |
| `contact.updated` | PUT `/api/contacts/{phone}/info` |
| `contact.ai_toggled` | POST `/api/contacts/{phone}/toggle-ai` |
| `contact.tagged` | PUT `/api/contacts/{phone}/tags` (snapshot completo da lista de tags) |
| `contact.untagged` | **NOVO** — um emit POR tag removida em PUT `/api/contacts/{phone}/tags` (`{phone, tag, ts}`) |
| `tag.created` / `tag.updated` / `tag.deleted` | tag endpoints |
| `execution.started` / `execution.ended` | **NOVO** — wrappers async `astart_execution`/`aend_execution`. `ended` carrega `error: str\|None` e `duration_ms` |
| `config.changed` | PUT `/api/config` (com `keys_changed`) |
| `tool_override.changed` | PUT `/api/tools/{name}` |
| `plugin.loaded` / `plugin.enabled` / `plugin.disabled` / `plugin.settings.changed` | lifecycle do plugin |
| `app.startup` / `app.shutdown` | lifespan do server |

Chave especial `*` — subscrever via `EVENT_HANDLERS = {"*": fn}` recebe todo evento emitido (após os subscribers específicos). `ctx.event_name` traz o nome real.

**Filters disponíveis** (ponto de modificação/cancelamento):

| Filter | Local | Tipo do valor | `None` faz | `ctx.extras` |
|--------|-------|---------------|------------|--------------|
| `filter.webhook.payload` | `/api/webhook` antes de qualquer parse | `dict` (body raw GOWA) | Webhook responde 200 sem processar | — |
| `filter.message.before_save` | inbound depois do parse | `dict` (mensagem tipada com `raw`, inclui `media_extras`) | Mensagem ignorada (nem salva nem responde) | `phone` |
| `filter.message.outgoing` | **NOVO** — antes de salvar/emitir um `message.sent` de echo (mensagem enviada do celular fora do app) | `dict` (mensagem tipada, `is_from_me=True`, `source="echo"`) | Echo ignorado (não salva nem emite) | `phone` |
| `filter.transcription.should_run` | **NOVO** — wrapper `_maybe_transcribe`, ANTES de chamar `transcribe_audio`/`describe_image` (cobre os 4 call sites) | `bool` (default `True`) | Pula a transcrição (mesmo efeito de `False`) | `phone, media_kind ∈ {audio,image}, media_path, is_group, group_jid, source ∈ {batch,echo,group_no_mention}` |
| `filter.transcription.result` | **NOVO** — depois da chamada de transcribe/describe, antes de a string ser usada | `str` | Trata como se a transcrição fosse vazia | igual ao `should_run` + `model` |
| `filter.media.unknown` | **NOVO** — último recurso antes da regra "ignored", quando nenhum media type built-in casou | `None` (default) ou `dict` `{media_type, media_path, text, media_extras}` | Cai no "ignored" original | `phone, raw` |
| `filter.contact.tags` | **NOVO** — `PUT /api/contacts/{phone}/tags` antes de `contact.set_tags` | `list[str]` (tags pretendidas) | Mantém tags atuais | `phone, previous_tags` |
| `filter.event.before_emit` | **NOVO** — wrap interno do `emit_with_filter`. Recebe o payload de QUALQUER evento prestes a sair (exceto lifecycle) | `dict` (payload) | Cancela o emit | `event_name` |
| `filter.system_prompt` | antes do LLM | `str` | System prompt vira vazio | `phone` |
| `filter.llm.messages` | antes do LLM | `list[dict]` (formato OpenAI) | LLM não é chamado | `phone, model` |
| `filter.llm.tools` | antes do LLM | `list[dict]` (schemas) | LLM chamado sem tools | `phone, model` |
| `filter.tool.args` | `_dispatch_tool` antes do executor | `{tool_name, args}` | Tool pulada | `phone` |
| `filter.tool.result` | `_dispatch_tool` depois do executor | `str` (feedback pro LLM) | LLM recebe string vazia | `phone, tool_name` |
| `filter.reply.raw` | `_send_reply` antes do split | `str` | Nada é enviado | `phone` |
| `filter.reply.parts` | depois do split | `list[str]` | Nada é enviado | `phone` |
| `filter.reply.part` | cada parte antes do GOWA (vale pra send manual também) | `str` | Aquela parte é pulada | `phone` |
| `filter.provisioning.number` | **NOVO (API 1.1.0)** — `setup.fetch_provision_target`, DEPOIS de o core resolver o destino (`/service_number` → env → literal do código) | `str` | **Aborta o envio**: sem destino o wizard recusa com erro acionável em vez de mandar a frase para um número que ninguém escolheu. O core não valida formato — normalizar é de quem responde | `source ∈ {service_number, fallback}, message` (a frase JÁ resolvida) |
| `filter.provisioning.message` | **NOVO (API 1.1.0)** — mesmo produtor, logo DEPOIS do de número, e **só se houver destino** | `str` | **Aborta o envio**: mensagem vazia queima a única abertura de conversa com um contato novo | `source, number` (o destino já decidido) |

**Lifecycle events bypassam `filter.event.before_emit`** — `plugin.loaded/enabled/disabled/settings.changed` e `app.startup/shutdown` chamam `emit()` direto. Plugin não pode bloquear seu próprio carregamento.

**Assinaturas**:

```python
# events.py
def on_event(ctx: EventContext, payload: dict) -> None: ...
async def on_event_async(ctx: EventContext, payload: dict) -> None: ...

EVENT_HANDLERS = {"message.received": on_event, "llm.after": on_event_async}

# filters.py
def fn(ctx: FilterContext, value: T) -> T | None: ...
async def fn_async(ctx: FilterContext, value: T) -> T | None: ...

FILTERS = {
    "filter.reply.part": fn,                    # priority default 100
    "filter.message.before_save": (fn, 50),     # priority 50 — roda antes
}
```

`ctx` expõe `handler` (AgentHandler), `plugin_id`, `plugin_db`, `event_name`/`filter_name`, `emitted_at`. Sync vai pra `asyncio.to_thread`; async é `await`-ado direto. Filter pode ser sync ou async — em paths sync (process_message) o WhatsBot-Lite usa `apply_filter_sync` que delega ao loop com `run_coroutine_threadsafe`.

**Padrões de uso comuns**:

- **Observador / auditor / analytics** — `EVENT_HANDLERS = {"*": log_handler}` ou eventos específicos.
- **Anonimizar / traduzir / sanitizar inbound** — `FILTERS = {"filter.message.before_save": fn}` modifica o dict.
- **Adicionar assinatura / formatar / mascarar PII na saída** — `FILTERS = {"filter.reply.part": fn}` modifica cada parte.
- **Bloquear contato / palavra-chave / horário** — qualquer filter retornando `None`. Veja o plugin `blacklist` na Loja de Plugins.
- **Injetar contexto extra no LLM** — `FILTERS = {"filter.system_prompt": fn}` ou `filter.llm.messages` pra reescrever o histórico antes do call.
- **Reagir a tool call específica** — `EVENT_HANDLERS = {"tool.after": fn}` com `if payload["tool_name"] == "x"`.
- **Push em tempo real pra tela do plugin** — `plugins.context.broadcast("evento", {...})` do dentro do handler.

**Boas práticas**:

- Filter síncrono trava o pipeline — mantenha rápido. Persistência pesada/network vai num event handler.
- **Para reagir a mensagem JÁ salva**: assine `message.saved`, não `message.received` — o último é emitido ANTES do INSERT no DB e listener que leia do DB pode race.
- **Pra controlar transcrição** (decisão "transcrever ou não, e como"): use `filter.transcription.should_run` + `filter.transcription.result`, nunca remova o campo `audio`/`image` no `filter.webhook.payload` — fazer isso quebra o player no histórico.
- NÃO chamar `gowa_client.send_message` dentro de handler de `message.sent` → loop infinito (a send produz outro `message.sent`).
- Filtre por `media_type` / `source` / `is_group` no INÍCIO do handler. O bus entrega tudo.
- Persista estado entre eventos em tabelas `plugin_<id>_*` (via `ctx.plugin_db` + `from sqlalchemy import text`), nunca em globals — não sobrevivem ao restart.
- `payload["raw"]` carrega o payload bruto do GOWA (potencialmente grande, com base64 de áudio). Plugins que logam tudo devem cortar `raw` antes de serializar.
- Restart obrigatório no toggle do plugin: `plugin.enabled`/`plugin.disabled` emitem ANTES do `os._exit`; o novo processo emite `plugin.loaded` no boot.

Plugin de exemplo bundled em `assets/plugin_examples/` (copiado na 1ª execução): apenas `lembretes` (tools + routes + migrations + screen — anota lembretes pedidos pelo contato e mostra na tela com update via WebSocket).

Os demais plugins de exemplo foram movidos para a **Loja de Plugins** (repositório [Techify-one/whatsbot-plugins](https://github.com/Techify-one/whatsbot-plugins), publicado em https://techify.one/whatsbot/plugins) e são instalados via `Importar (.zip)` na tela Gerenciar Plugins: `event_logger` (assina `*`), `auto_signature` (`filter.reply.part`), `blacklist` (`filter.message.before_save` → `None`), `transcricao_grupos` (`filter.transcription.should_run` — controle de transcrição por grupo via UI + DB), `horario_funcionamento` (settings declarativas + `filter.system_prompt`/`filter.llm.tools`/`filter.llm.messages` + migrations — horário de funcionamento por dia da semana, com mensagem de ausência fora do expediente e cooldown por contato), `custom_sounds` (screen `config: true` + routes/migrations — biblioteca de sons), `notifications` (screen `config: true` somente-UI — preferências de notificação per-device em `localStorage`).

### Media types suportados

O webhook detecta os tipos abaixo e os converte em `parsed_msg` (`media_type` + `media_path` + `media_extras`):

| `media_type` | Campo no payload GOWA | `media_path` | `media_extras` típico |
|---|---|---|---|
| `image` | `image` (str ou `{path, caption, mimetype}`) | path do arquivo | `{caption, mimetype}` |
| `audio` | `audio` ou `video_note` (str ou `{path, duration, mimetype}`) | path do arquivo | `{duration_ms, mimetype, is_voice_note}` |
| `video` | `video` (str ou `{path, caption, duration, mimetype}`) | path do arquivo | `{caption, duration_ms, mimetype}` |
| `sticker` | `sticker` (str ou `{path, is_animated, mimetype}`) | path do arquivo | `{is_animated, mimetype}` |
| `document` | `document` (str ou `{path, file_name, mimetype, caption}`) | path do arquivo | `{file_name, mimetype}` |
| `location` | `location` (`{latitude, longitude, name, address}`) | `geo:lat,lng` | `{lat, lng, name, address}` |
| `live_location` | `live_location` | `geo:lat,lng` | `{lat, lng}` |
| `poll` | `poll` (`{name, options[]}`) | `None` | `{name, options}` |
| `interactive` | `buttons_response` / `list_response` | `None` | `{button_id, title}` ou `{row_id, title}` |
| `order` | `order` | `None` | `{item_count, total, currency}` |
| `product` | `product` | `None` | `{product_id, title}` |
| `contact` | `contact` (single vCard) | `None` | `{contacts: [{name, phone}]}` |
| `contacts` | `contacts_array` (lista) | `None` | `{contacts: [...]}` |

Plugins podem registrar tipos completamente customizados via `filter.media.unknown` — devolva `{media_type, media_path, text, media_extras}` e a mensagem vira aquele tipo.

### Criar um plugin novo

Use o slash command `/new-plugin` no Claude Code. O comando lê os arquivos de referência, pergunta requisitos (id, telas, tools, tabelas, settings) e gera a estrutura completa em `storages/plugins/<id>/` sem tocar no core. Veja `.claude/commands/new-plugin.md`.

### Importar/exportar

- Export: `GET /api/plugins/<id>/export` retorna um `.zip` da pasta (excluindo `__pycache__/` e arquivos `.db`), nomeado `<id>-<versao>-plugin.zip` — cai em `<id>-plugin.zip` se o manifesto não for legível. O nome do arquivo NÃO participa do import, que lê o `id` do manifesto de dentro do zip.
- Import: `POST /api/plugins/import` (multipart) valida o `plugin.yaml` na raiz, checa colisão de `id` e path traversal, extrai em `storages/plugins/<id>/`. Plugin importado fica `enabled=0` — usuário ativa pela UI.

## Migração de dados legados

Para instalações que usavam a versão anterior (armazenamento em JSON), o sistema detecta automaticamente na inicialização se o banco está vazio e existem arquivos JSON legados (`contacts/*.json`, `config.json`). Nesse caso, executa a migração via `db/migrate_json.py`. Os arquivos JSON originais não são deletados.

## Testes automatizados

Testes de endpoint em `tests/test_endpoints.py` — cobrem todos os endpoints da API usando FastAPI TestClient com banco SQLite temporário. GOWA e o LLM (proxy Techify) são mockados.

```bash
# Rodar testes (não precisa de servidor rodando)
source venv/Scripts/activate
python tests/test_endpoints.py
python tests/test_gowa_update.py   # updater do GOWA (offline, release falsa via file://)
python tests/test_gowa_proxy.py    # proxy do GOWA (offline, proxies SOCKS5/HTTP falsos em socket)
python tests/test_provisioning_target.py  # par destino+frase do provisionamento + os dois seams (offline)
python tests/test_llm_costing.py  # cache do LLM: session id, header x-session-id, estimate_cost com desconto, usage_repo (offline)
```

Os testes criam um banco temporário (SQLite por default; setar `WHATSBOT_TEST_DB_URL=postgresql+psycopg://...` para rodar contra Postgres), inserem dados de teste (contatos, mensagens, tags, usage), e validam ~250 checagens (helper `check(...)`) cobrindo:
- Health, Auth (com e sem senha), Config (GET/PUT/test-key, `group_reply_mode`), Status, Balance
- Contacts (list, detail, search, archived, send, retry, image, audio, presence, read, toggle-ai, update info, **pin/unpin**, **unread/mark-all-read/mark-all-unread**, **unread-count**, **@menção em grupo / has_unread_mention**, **react/delete de mensagem**, **members** de grupo, **check-phone / lookup** sem efeito colateral)
- Tags (CRUD + contact tags)
- Usage (summary, by-contact, detail)
- Logs, Webhook payloads, Webhook (presence, echo, ack, reaction, reply/quoted, revoke)
- GOWA (versão, check/update/rollback/skip-version e **proxy**: validação, máscara de senha, restart e teste de conexão)
- Setup wizard (`request-key` com o `/service_number` fixado — o par destino+frase vai como veio do endpoint — e os dois abortos dos seams; `key-status` pendente/pronta)
- WhatsApp/QR (get, refresh, reconnect, logout)
- Sandbox (send, clear)
- Frontend SPA routes (inclui `/wizard`)
- Auth middleware (proteção de endpoints, exemptions)

## Teste opcional com Evolution API

Se você tiver acesso a uma instância da Evolution API, pode testar o fluxo de mensagens de ponta a ponta. Isso é opcional, mas recomendado ao alterar webhook, agent, handler ou batching.

Variáveis de teste devem ser configuradas no arquivo `.env`:
- `EVOLUTION_API_URL` — URL base da Evolution API
- `EVOLUTION_API_KEY` — API key de autenticação
- `EVOLUTION_INSTANCE_ID` — ID da instância Evolution
- `EVOLUTION_TEST_NUMBER` — número WhatsApp para receber a mensagem de teste

### Como testar

1. Garanta que o servidor está rodando e conectado (`curl /api/status` → `connected: true`)
2. Envie mensagem de teste via Evolution API:
```bash
source .env
curl -X POST "${EVOLUTION_API_URL}/message/sendText/${EVOLUTION_INSTANCE_ID}" \
  -H "Content-Type: application/json" \
  -H "apikey: ${EVOLUTION_API_KEY}" \
  -d "{\"number\": \"${EVOLUTION_TEST_NUMBER}\", \"text\": \"mensagem de teste\"}"
```
3. Aguarde ~10 segundos e verifique os logs:
```bash
curl -s http://127.0.0.1:{web_port}/api/logs?limit=10
```
4. Confirme nos logs que aparece:
   - `[Webhook] Message from ...` — mensagem recebida
   - `[Batch] Processing N messages ...` — batch processado
   - `[Batch] Replied to ...` — resposta enviada

### Processo de teste para kill/restart

```bash
# Matar processos anteriores
taskkill //F //IM gowa.exe 2>&1; taskkill //F //IM python.exe 2>&1

# Iniciar servidor
source venv/Scripts/activate
python -c "import uvicorn; from server.dev import app; uvicorn.run(app, host='127.0.0.1', port=8080, log_level='info')"
```

## Gotchas

- O GOWA demora ~5s para iniciar e aceitar conexões — o polling de QR/status deve tolerar falhas silenciosamente
- **Device obrigatório**: `POST /devices` deve ser chamado antes de qualquer outro endpoint; sem device registrado, tudo retorna 404 `DEVICE_NOT_FOUND`
- **Login quando já conectado**: `GET /app/login` retorna erro `ALREADY_LOGGED_IN` se o device já está autenticado — verificar `is_connected()` antes de pedir QR
- **Respostas aninhadas**: listas de chats/mensagens vêm em `results.data[]`, não direto em `results`
- JIDs do WhatsApp seguem formato `5511999999999@s.whatsapp.net` — extrair phone com `.split("@")[0]`
- O PyInstaller foi removido do projeto: não há mais tratamento de `sys._MEIPASS` em `gowa/manager.py`
  nem em `server/helpers.py`. A distribuição no Windows é o repositório + `windows_start.bat`
- `subprocess.CREATE_NO_WINDOW` é necessário no Windows para não abrir janela de console do GOWA
- GOWA usa `stdout=subprocess.DEVNULL` — NUNCA usar `subprocess.PIPE` sem consumir, causa deadlock no Windows
- Config auto-salva no shutdown do server (lifespan) e na primeira execução (`Settings.load`)
- Frontend vendorizado: libs JS em `web/static/vendor/` — sem dependência de CDN em runtime
- **Permissão de envio em grupo**: a abertura de um grupo consulta o GOWA (`can_bot_send_in_group`) com cache de 60 s e timeout de 4 s, em paralelo com o banco; se o GOWA demorar ou falhar, vale o valor já salvo. O webhook continua atualizando o valor a cada mensagem do grupo
- **Sockets fantasma no Windows**: ao reiniciar frequentemente, portas podem ficar presas em LISTENING com PIDs inexistentes. Use porta alternativa ou reinicie o PC
- **`windows_start.bat` mata processos**: o bat já executa `taskkill` para gowa.exe e uvicorn.exe antes de iniciar. No Linux, o `linux_start.sh` faz `pkill -f bin/gowa` no fim de cada iteração do loop pra liberar a porta antes de relançar; pra parar manualmente, `pkill -f "uvicorn server.dev"` + `pkill -f bin/gowa`
- **GOWA `/chats` limit máximo**: `GET /chats?limit=N` retorna HTTP 400 para valores acima de ~200. Usar `limit=100` como máximo seguro
- **Archive status é chat-level**: o webhook do GOWA **não** inclui campo de archive no payload. Para saber se um chat é arquivado, consultar `GET /chats` e verificar o campo `archived` no item com o `jid` correspondente
- **Debug do subprocess GOWA**: por padrão o stdout/stderr do GOWA vão para `DEVNULL` (sem custo). Para diagnosticar mensagens descartadas (payloads vazios, tipos não decodificados, templates HSM da Cloud API, etc.), setar a env `WHATSBOT_GOWA_DEBUG=1` (no Coolify ou outro ambiente) e reiniciar o container. Com a flag ativa, o GOWA é iniciado com `--debug=true` e os logs são gravados em `logs/gowa.log` (truncado quando passa de ~10 MB). Acessível via `GET /api/gowa-logs?limit=N` (default 500, max 5000). A resposta inclui `debug_enabled`, `log_path`, `size` e `lines[]`. Desligar setando `WHATSBOT_GOWA_DEBUG=0` ou removendo a variável + reiniciando
- **Mensagens HSM via Cloud API (linked device limitation)**: contas Business via WhatsApp Cloud API enviam mensagens template (`<hsm tag="..."/>`, ex: Mercado Livre, OTP, notificações). Por design do WhatsApp, esses templates **não são entregues com conteúdo para linked devices** — só para o device primário. O GOWA recebe um `placeholderMessage` com `type: MASK_LINKED_DEVICES` (sem body/media), e o webhook chega só com metadata (`chat_id`, `from`, `id`, `timestamp`). Não é bug — é limitação estrutural. Para confirmar, ativar `WHATSBOT_GOWA_DEBUG=1` e procurar `placeholderMessage` ou `<hsm tag=` em `/api/gowa-logs`
- **SQLite WAL files**: `whatsbot.db-wal` e `whatsbot.db-shm` são criados automaticamente pelo SQLite no modo WAL. Não deletar enquanto o servidor estiver rodando. São limpos automaticamente quando todas as conexões fecham
- **Auto-criação do banco**: na inicialização, `init_db()` resolve a URL (env > `storages/database.json` > sqlite default), cria o engine e roda `alembic upgrade head`. SQLite vazio é criado do zero; DBs SQLite legados (sem `alembic_version`) são automaticamente stampados no baseline antes do upgrade — não há recriação destrutiva
- **Bootstrap de plugins**: os plugins de referência vivem em `assets/plugin_examples/<id>/` (trackeados no git) e são copiados para `storages/plugins/<id>/` apenas na 1ª execução, quando `storages/plugins/` está vazio. Atualizar o core nunca sobrescreve plugins do usuário. Se o usuário deletar um plugin de referência pela UI, ele NÃO volta no próximo boot — a flag de "primeira execução" é "tem alguma subpasta?". Bundled hoje: apenas `lembretes`. Os outros plugins de exemplo vivem na Loja de Plugins (repositório `whatsbot-plugins`) e são instalados via `Importar (.zip)`. Instalações que já tinham `storages/plugins/` populado NÃO recebem plugins novos automaticamente — importar via `POST /api/plugins/import` (zip gerado por `GET /api/plugins/<id>/export`) ou esvaziar a pasta antes do próximo boot.
- **Restart de plugin requer supervisor**: `enable`/`disable` chama `os._exit(0)` após um delay curto. Em Docker, `restart: unless-stopped` (compose) faz o container relançar; em dev, `restart.py` toca `server/_reload_trigger.py` (`.py` dentro de um `--reload-dir`, casa com o include default `*.py` do uvicorn) — o watchfiles reinicia o worker antes do `os._exit` rodar. O arquivo é regenerado em runtime e está no `.gitignore`. Em EXE Windows, o `update.py` relança. Sem supervisor, o servidor cai e não volta sozinho.
- **Prefixo de tabela enforced**: o migrator usa regex em `CREATE TABLE`/`ALTER TABLE`/`CREATE INDEX`/`DROP TABLE`/`DROP INDEX` e RECUSA migration que tente criar objeto fora do prefixo `plugin_<id>_`. Erro mostra qual nome violou. Usar comentários SQL `--` ou `/* */` é OK; o migrator os strip-a antes da validação.
- **Auto-incremento em migrations de plugin (SQLite vs Postgres)**: o idiom SQLite `id INTEGER PRIMARY KEY AUTOINCREMENT` é inválido em Postgres (erro de sintaxe `near "AUTOINCREMENT"`). O migrator resolve isso automaticamente: `_adapt_sql` ([plugins/migrator.py](plugins/migrator.py)) reescreve esse padrão para `INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` quando o backend é Postgres (em SQLite passa intacto). Pode escrever a migration no estilo SQLite que ela roda nos dois. A tradução cobre só esse idiom de PK — outros SQLite-ismos (`strftime`, `INSERT OR REPLACE`, etc.) continuam não-portáveis e devem ser evitados. Insira linhas com `INSERT ... RETURNING id` (suportado por SQLite 3.35+ e Postgres) em vez de depender de `lastrowid`.
- **Tool name é global**: se um plugin registra uma tool com nome já existente (core ou outro plugin), o registry loga warning e ignora a duplicata. Convenção: nomes específicos como `<id>_<verbo>` (ex: `orders_create`).
- **Dependências de plugin (auto-cura + validador)**: o ideal é declarar os pacotes pip de terceiros no `dependencies` do `plugin.yaml`. Mas se o autor esquecer, o loader **não quebra mais**: [plugins/dep_check.py](plugins/dep_check.py) detecta os `import` de terceiros não satisfeitos e instala sozinho (resolvendo import→pacote por mapa curado, ex.: `google`→`google-auth`, `PIL`→`pillow`, `bs4`→`beautifulsoup4`; senão assume `import foo`→`pip install foo`). Não declare libs do host (`pydantic`, `fastapi`, `sqlalchemy`, `httpx`, `pyyaml`, …) — são o fecho transitivo do `requirements.txt` e estão sempre presentes. **Antes de publicar**, rode o validador: `python -m plugins.dep_check storages/plugins/<id>` (sai com código ≠0 e lista import→pacote a declarar; serve pro `/new-plugin` e CI da Loja). **Atenção:** em build empacotado (EXE/frozen) não há pip — a auto-cura não roda, então toda dependência de terceiros PRECISA estar declarada e vir no bundle. A heurística de adivinhação só cobre o caso comum; nomes import≠pacote fora do mapa curado falham com `load_error` acionável.
- **Import dinâmico de plugin JS**: o componente é carregado via `import(screen.component)` ES nativo. O path no manifest precisa começar com `/plugins/<id>/static/...` (servido pelo mount estático). CSP em `server/app.py` permite `'self'`, então funciona sem mudança.
- **Plugin com erro de carga**: se importação falha, o erro vai pra coluna `load_error` na tabela `plugins`, aparece no card da UI, e o plugin é pulado — o app sobe normalmente. Não há crash em cascata.
