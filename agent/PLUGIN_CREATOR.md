# Referência completa para criar plugins do WhatsBot-Lite

Esta referência contém o contrato necessário para criar e atualizar plugins. Use-a primeiro. Consulte o
código do WhatsBot-Lite, `AGENTS.md`, exemplos oficiais ou outros plugins apenas quando uma informação concreta
não estiver aqui. Faça busca direcionada, leia somente o trecho necessário e volte à implementação.

## Fluxo obrigatório

1. Escolha o `id` estável já definido pelo projeto. Ele usa `snake_case`, começa com letra e tem até 32 caracteres.
2. Projetos novos recebem um esqueleto descartável marcado por `.whatsbot-scaffold`. Não leia nem liste esse
   esqueleto: o id obrigatório está no contexto, a versão inicial é `1.0.0` e `whatsbot_api_version` é
   `">=1.2,<2.0"`. A primeira ação deve sobrescrever `plugin.yaml` com `write_file`; depois implemente apenas
   as camadas necessárias. Apague `.whatsbot-scaffold` quando o pedido estiver completo. Nunca encerre a
   rodada apenas anunciando que vai começar.
3. Se o workspace já tiver arquivos, leia somente os arquivos que serão alterados. Preserve o `id` e migrations existentes.
4. Implemente o comportamento completo. Não pare depois de criar um esqueleto.
5. Se houver tela, aplique o contrato de qualidade visual desta referência e revise o componente como uma
   pessoa usando a tela, não apenas como código. A interface é parte do plugin, não acabamento opcional.
6. Crie testes simples para regras importantes quando fizer sentido.
7. Chame `validate_plugin_project`. Corrija todos os erros de backend e frontend e valide novamente até
   retornar `valid: true`.
8. Responda com um resumo curto do que foi criado e testado. Diga que o WhatsBot-Lite exibirá a opção
   **Sim, instalar**. O usuário também pode autorizar escrevendo “instale por favor” no próprio chat; o
   WhatsBot-Lite fará a instalação diretamente. Não mande o usuário procurar o plugin na página Plugins antes
   de ele ser instalado.

Num projeto novo, comece escrevendo os arquivos usando esta referência; não leia o esqueleto nem pesquise
exemplos antes da primeira escrita. Se uma ferramenta devolver erro de argumentos ou formatação, corrija a chamada e tente novamente.
Não procure outro arquivo para descobrir como chamar a ferramenta. A estrutura inicial não é um plugin
pronto: a validação reprova enquanto `.whatsbot-scaffold` existir.

Não narre planos entre as ações. Não diga “vou pesquisar”, “vou começar” ou “continuarei depois”. Use as
ferramentas do workspace. O terminal roda na raiz do plugin: use comandos Python portáteis para testes e
diagnósticos, sem instalar pacotes ou acessar a rede. Use somente caminhos relativos ao plugin. Não procure
`python`, `pytest`, ambientes virtuais, testes do validador ou arquivos internos do host: o resultado de
`validate_plugin_project` é a fonte de verdade e já identifica o teste que falhou. Exemplos oficiais ficam
em `assets/plugin_examples/`.

## Estrutura

Use somente os arquivos necessários:

```text
plugin.yaml
__init__.py
tools.py                    # ações que a IA do WhatsApp pode chamar
prompts.py                  # instruções adicionais para a IA saber quando usar as ações
routes.py                   # API da tela do plugin
events.py                   # reações a eventos do WhatsBot-Lite
filters.py                  # alteração de dados no fluxo
settings.py                 # opções simples exibidas em Plugins → Configurar
migrations/001_initial.sql  # tabelas persistentes
static/<id>.js              # tela Preact do plugin
test_plugin.py              # funções test_* sem parâmetros
```

## Manifesto

O diretório do projeto e o campo `id` precisam ter o mesmo nome. Declare em `entry` apenas módulos que
existirem. Use `whatsbot_api_version: ">=1.2,<2.0"` quando usar a API descrita aqui.
Módulos do mesmo plugin usam imports relativos, por exemplo `from .settings import delivery_message`;
eles são carregados no namespace `whatsbot_plugins.<id>` e não existem como módulos globais.

```yaml
id: meu_plugin
name: Meu Plugin
version: 1.0.0
whatsbot_api_version: ">=1.2,<2.0"
description: Descrição curta.
author: WhatsBot-Lite
entry:
  tools: tools
  prompts: prompts
  routes: routes
  events: events
  filters: filters
  settings: settings
migrations: migrations
screens:
  - id: meu-plugin-home
    title: Meu Plugin
    path: /meu-plugin
    icon: list
    component: /plugins/meu_plugin/static/meu_plugin.js
permissions:
  - llm.tool
  - db.write
dependencies: []
events:
  - message.saved
filters: []
```

Uma tela de uso normal aparece no menu da engrenagem. Uma tela com `config: true` aparece dentro do modal
**Configurar** do plugin e não no menu. Opções simples devem usar `settings.py` em vez de uma tela própria.

Para colocar um botão ou atalho dentro de uma tela do WhatsBot-Lite (hoje: o cabeçalho da conversa), declare
`frontend_extends: /plugins/meu_plugin/static/extends.js` no manifesto e use `whatsbot_api_version:
">=1.3,<2.0"`. O caminho precisa começar com `/plugins/<id>/static/`. O módulo exporta uma função padrão,
chamada uma vez ao abrir o painel, que registra um componente Preact no slot `chat.header.actions`; ele recebe
`phone`, `contact`, `info`, `isGroup` e `rawName` como props. Sem necessidade de botão no cabeçalho, não use
`frontend_extends`.

```js
import { h } from 'preact';
import htm from 'htm';
const html = htm.bind(h);

function Acao({ phone }) {
  return html`<button class="wa-field px-2 py-1 rounded text-sm"
    onClick=${() => fetch('/api/plugins/meu_plugin/ping', { method: 'POST' })}>Ping ${phone}</button>`;
}

export default function ({ addSlot }) {
  addSlot('chat.header.actions', Acao);
}
```
Dependências de terceiros importadas pelo código precisam constar em `dependencies`; `fastapi`, `pydantic`,
`sqlalchemy`, `httpx` e módulos do WhatsBot-Lite já pertencem ao host e não são declarados.

## Persistência e migrations

Use SQLAlchemy com parâmetros nomeados. Toda tabela e todo índice começam com `plugin_<id>_`. O SQL abaixo
é portável entre SQLite e PostgreSQL porque o migrador adapta a chave autoincrementável:

```sql
CREATE TABLE IF NOT EXISTS plugin_meu_plugin_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plugin_meu_plugin_items_status
    ON plugin_meu_plugin_items(status);
```

No Python:

```python
from sqlalchemy import text
from plugins.context import make_plugin_db

with make_plugin_db() as conn:
    row = conn.execute(
        text("INSERT INTO plugin_meu_plugin_items (phone, title, created_at) "
             "VALUES (:phone, :title, :created_at) RETURNING id"),
        {"phone": phone, "title": title, "created_at": created_at},
    ).scalar_one()
```

Use `with make_plugin_db()` para leitura e escrita; o bloco confirma a transação ao terminar. Evite SQL
específico de SQLite. Calcule timestamps com `int(time.time())`. Atualizações criam migrations novas e
aditivas (`002_...sql`, `003_...sql`); nunca altere uma migration já entregue e não use `DROP`, `TRUNCATE`
ou renomeações destrutivas. Arquivos enviados pelo usuário ficam em
`plugin_data_dir("meu_plugin")`, pois essa pasta sobrevive às atualizações do código.

## Ações da IA e instruções

Uma tool recebe `ctx` e `args`. `ctx.contact.phone` identifica a conversa; `ctx.contact.info` contém dados
conhecidos do contato; `ctx.plugin_db` equivale a `make_plugin_db`.

```python
import time
from sqlalchemy import text
from plugins.context import broadcast, make_plugin_db

CREATE_ITEM_TOOL = {
    "type": "function",
    "display_label": "Registrar item",
    "function": {
        "name": "meu_plugin_criar_item",
        "description": "Registra o item somente depois que o cliente confirmar.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Resumo completo do item confirmado"}
            },
            "required": ["title"],
        },
    },
}

def execute_create(ctx, args):
    title = str((args or {}).get("title") or "").strip()
    if not title:
        return "Não foi possível registrar: descrição vazia."
    with make_plugin_db() as conn:
        item_id = conn.execute(
            text("INSERT INTO plugin_meu_plugin_items "
                 "(phone, title, status, created_at) "
                 "VALUES (:phone, :title, 'pending', :created_at) RETURNING id"),
            {"phone": ctx.contact.phone, "title": title, "created_at": int(time.time())},
        ).scalar_one()
    broadcast("plugin_meu_plugin_changed", {"id": item_id})
    return "Item registrado com sucesso."

CORE_TOOLS = [(CREATE_ITEM_TOOL, execute_create)]
```

Use nomes de tool prefixados pelo id para evitar colisões. A descrição deve dizer claramente quando chamar
e quando não chamar. `prompts.py` complementa o prompt do atendimento:

```python
def meu_plugin_prompt(contact, ctx):
    return (
        "\n\n--- Meu Plugin ---\n"
        "Depois de o cliente confirmar todos os dados, chame meu_plugin_criar_item uma única vez. "
        "Não registre rascunhos nem pedidos ainda em negociação."
    )

PROMPT_FRAGMENTS = [meu_plugin_prompt]
```

## Rotas, tela e mensagens no WhatsApp

O router é montado automaticamente em `/api/plugins/<id>`. Não repita esse prefixo:

```python
import asyncio
from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from plugins.context import (
    broadcast, get_plugin_setting, make_plugin_db, send_whatsapp_message,
)

router = APIRouter()

@router.get("/items")
async def list_items():
    with make_plugin_db() as conn:
        rows = conn.execute(text(
            "SELECT id, phone, title, status, created_at "
            "FROM plugin_meu_plugin_items ORDER BY created_at DESC"
        )).mappings().all()
    return {"ok": True, "data": [dict(row) for row in rows]}

@router.post("/items/{item_id}/status")
async def change_status(item_id: int, body: dict):
    status = str((body or {}).get("status") or "")
    if status not in {"accepted", "cancelled", "delivering"}:
        raise HTTPException(400, "Situação inválida")
    with make_plugin_db() as conn:
        row = conn.execute(text(
            "SELECT id, phone, title, status, created_at "
            "FROM plugin_meu_plugin_items WHERE id=:id"
        ), {"id": item_id}).mappings().first()
    if not row:
        raise HTTPException(404, "Item não encontrado")
    item = dict(row)
    if status == "delivering" and item["status"] != "accepted":
        raise HTTPException(409, "Aceite o item antes de marcar a entrega")
    if status == "delivering":
        message = get_plugin_setting(
            "meu_plugin", "delivery_message", "Seu pedido saiu para entrega!"
        )
        await asyncio.to_thread(send_whatsapp_message, item["phone"], message)
    with make_plugin_db() as conn:
        conn.execute(text(
            "UPDATE plugin_meu_plugin_items SET status=:status WHERE id=:id"
        ), {"id": item_id, "status": status})
    item["status"] = status
    broadcast("plugin_meu_plugin_changed", item)
    return {"ok": True, "data": item}
```

`send_whatsapp_message(phone, text, mentions=None, reply_message_id=None)` é a API oficial de envio de
texto. Ela usa o fluxo do WhatsBot-Lite, salva a mensagem no histórico, atualiza a tela, emite `message.sent` e
respeita contatos da área de testes sem enviar para o GOWA. Ela levanta erro se o envio real falhar. Em rota
`async`, chame por `await asyncio.to_thread(...)`; em tool ou event handler síncrono, chame diretamente.
Nunca reaja a `message.sent` enviando outra mensagem, pois isso cria um ciclo. Para ações repetíveis, guarde
um estado de notificação e bloqueie chamadas duplicadas. Não marque como avisado antes de o helper retornar.
O mesmo vale para o status que afirma que a ação ocorreu: envie primeiro e só depois grave `delivering`,
`notified` ou qualquer estado equivalente. Se o helper levantar erro, o banco deve conservar o estado
anterior para a pessoa poder tentar novamente. Escreva um teste de comportamento que substitua o helper
por uma falha controlada e comprove que o estado não mudou; testar apenas uma função de normalização não
comprova esse fluxo.

A tela é Preact + HTM sem build e recebe `apiBase` automaticamente. Inclua o token nas chamadas e no
WebSocket. Para atualização ao vivo, abra `/ws?token=<token>`, trate mensagens JSON no formato
`{event, data}` e feche o socket no cleanup do `useEffect`.

### Contrato de qualidade visual

Este contrato vale para qualquer plugin com tela. Ele descreve a qualidade, não um desenho fixo. Não copie
um painel de pedidos para uma agenda, um formulário ou um relatório. Escolha uma composição coerente com o
assunto e mantenha os mesmos padrões de organização, contraste e acabamento.

Antes de escrever o componente, decida internamente:

1. **Pessoa e tarefa principal:** quem usa a tela e qual ação precisa concluir rapidamente.
2. **Prioridade da informação:** o que precisa ser reconhecido primeiro, comparado lado a lado e consultado
   só quando necessário.
3. **Estrutura apropriada:** coleções operacionais usam linhas ou grade densa no desktop e cards no celular;
   agendas usam tempo como eixo; formulários usam grupos curtos; indicadores existem apenas quando ajudam a
   decidir. Não use um hero alto, quatro cartões de números e gradientes como abertura automática.
4. **Direção visual:** escolha uma paleta pequena, uma escala tipográfica e um único elemento de identidade
   ligado ao assunto. O restante deve ser calmo e funcional.

Requisitos obrigatórios:

- **Use a largura disponível.** A raiz usa `width: 100%` e não recebe `max-w-5xl`, `max-w-3xl` ou outra
  limitação genérica. Um formulário de leitura curta pode ter uma coluna interna limitada, mas cabeçalho,
  contexto e fundo da página continuam ocupando a área da tela.
- **Mostre mais trabalho com menos rolagem.** Em coleções, alinhe os campos comparáveis em colunas no desktop.
  Evite um cartão alto por registro, caixas dentro de caixas e textareas sempre abertas. Detalhes secundários
  podem abrir sob demanda. Endereço, título, valores e observações importantes não podem ser cortados sem um
  meio claro de consultar o conteúdo completo.
- **Crie hierarquia real.** Título da página, contexto curto, controles, dados principais, dados secundários e
  ações precisam ter pesos distintos. Valor, horário, estado ou outro dado decisivo recebe destaque adequado
  ao assunto. Rótulos descrevem conteúdo; decoração não substitui estrutura.
- **Contraste explícito.** Prefira `wa-*` e `.wa-field`. CSS próprio usa variáveis na raiz e equivalentes em
  `html.dark`. Texto comum busca contraste mínimo de 4.5:1; texto grande e bordas de controles, 3:1. Inputs,
  selects e textareas sempre têm fundo, borda, texto e placeholder definidos nos dois temas. Nunca deixe
  campo branco com texto branco ou superfície cinza sem borda perceptível.
- **Interação nítida.** Botões descrevem a ação (`Salvar nota`, `Confirmar visita`), têm estados hover,
  `disabled` e `:focus-visible`. Ação destrutiva pede confirmação perto do item. Operações assíncronas mostram
  qual item está ocupado e não apagam dados em caso de erro.
- **Estados completos.** Implemente carregamento, vazio orientativo, erro com possibilidade de tentar de novo
  e sucesso quando a ação precisar de confirmação. Se houver busca ou filtro, o vazio diferencia “não há
  dados” de “nenhum resultado”.
- **Responsividade real.** Defina ao menos um breakpoint para reorganizar a estrutura e outro para celulares
  estreitos quando a tela for complexa. Teste mentalmente 1366×768, 390×844 e 360×800. A página não cria
  rolagem horizontal; controles de toque têm cerca de 44px; campos no celular usam fonte de 16px para evitar
  zoom. Tabelas largas viram cards ou expõem rolagem local identificável.
- **Acessibilidade.** Campos têm `<label>` ou `aria-label`; botões de ícone têm nome acessível; foco por teclado
  permanece visível. Use SVG simples ou texto para ícones funcionais, em vez de símbolos Unicode ambíguos.
- **Texto para pessoas.** Escreva em português claro, sem nomes de endpoints, tabelas ou detalhes internos.
  Espaço vazio, erro e confirmação explicam o que aconteceu e a próxima ação.

Use CSS com nomes prefixados pelo plugin para evitar colisões. Uma base pequena e adaptável é suficiente:

```javascript
import { h } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';
import htm from 'htm';
const html = htm.bind(h);

const CSS = `
.mp-page{--mp-ink:#172b3a;--mp-muted:#526579;--mp-surface:#fff;--mp-panel:#edf2f6;
  --mp-line:#6b7d90;--mp-accent:#12574e;width:100%;min-width:0;padding:20px;
  color:var(--mp-ink);background:var(--mp-panel);font-family:'Segoe UI',system-ui,sans-serif}
html.dark .mp-page{--mp-ink:#eff5fb;--mp-muted:#b5c5d4;--mp-surface:#203340;
  --mp-panel:#15232f;--mp-line:#829bac;--mp-accent:#9fe4cf}
.mp-page *{box-sizing:border-box}.mp-field{background:var(--mp-surface);color:var(--mp-ink);
  border:1.5px solid var(--mp-line);border-radius:8px;min-height:42px}
.mp-page :focus-visible{outline:3px solid var(--mp-accent);outline-offset:2px}
.mp-layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(220px,.7fr);gap:16px}
@media(max-width:900px){.mp-layout{grid-template-columns:1fr}}
@media(max-width:560px){.mp-page{padding:12px}.mp-field{font-size:16px;min-height:44px}}
`;

function authHeaders(extra = {}) {
  const token = localStorage.getItem('whatsbot_token') || '';
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

export default function MeuPlugin({ apiBase = '/api/plugins/meu_plugin' } = {}) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  async function load() {
    setLoading(true);
    try {
      const response = await fetch(`${apiBase}/items`, { headers: authHeaders() });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'Não foi possível carregar.');
      setItems(result.data || []); setError('');
    } catch (err) { setError(err.message); }
    finally { setLoading(false); }
  }
  useEffect(() => { load(); }, []);
  return html`<section class="mp-page" aria-label="Meu plugin"><style>${CSS}</style>
    <header><h2>Meu plugin</h2><p>Contexto curto para a tarefa desta página.</p></header>
    ${error ? html`<div role="alert">${error}<button onClick=${load}>Tentar novamente</button></div>` : null}
    ${loading ? html`<p>Carregando…</p>` : items.length === 0
      ? html`<div><h3>Nenhum item ainda</h3><p>Explique como o primeiro item aparecerá aqui.</p></div>`
      : html`<div class="mp-layout"><main><!-- estrutura principal adequada ao conteúdo --></main>
          <aside><!-- contexto ou ações secundárias --></aside></div>`}
  </section>`;
}
```

O exemplo demonstra fundação, estados e adaptação; ele não é um layout a ser copiado literalmente. Troque o
prefixo `mp`, a estrutura e a identidade conforme o plugin. Depois de implementar, faça uma autocrítica:

- a página usa a área horizontal sem esticar texto longo demais;
- pelo menos dois registros comuns caberiam em 1366×768 quando a tela for uma coleção;
- o dado mais importante é reconhecido em poucos segundos;
- campos e botões continuam visíveis em tema claro e escuro;
- a versão de 360px preserva conteúdo e ações sem overflow horizontal;
- não há decoração, estatística ou caixa que possa ser removida sem perder informação.

`validate_plugin_project` rejeita telas sem requisitos estruturais verificáveis, mas essa validação não mede
bom gosto. Revise as escolhas visuais antes de concluir, execute uma verificação de sintaxe do JavaScript com
o terminal quando houver runtime disponível e corrija todos os apontamentos do validador.

## Configurações

```python
from pydantic import BaseModel, Field

class Settings(BaseModel):
    delivery_message: str = Field(
        default="Seu pedido saiu para entrega!", title="Mensagem de saída para entrega"
    )
    notifications_enabled: bool = Field(default=True, title="Enviar avisos")
```

O WhatsBot-Lite salva esses campos como `plugin.<id>.<campo>`. Leia no backend com
`get_plugin_setting("meu_plugin", "delivery_message", valor_padrao)`. Não escreva configuração do plugin
diretamente no painel do core.

## Events e filters

Use events para observar sem bloquear. O handler pode ser síncrono ou assíncrono e recebe
`ctx.plugin_db`, `ctx.plugin_id`, `ctx.handler` e `ctx.event_name`:

```python
def on_message_saved(ctx, payload):
    phone = payload.get("phone")
    text = payload.get("text") or ""
    # trabalho curto e isolado

EVENT_HANDLERS = {"message.saved": on_message_saved}
```

Eventos mais usados: `message.received` (antes de salvar), `message.saved` (depois de salvar),
`message.sent`, `message.reaction`, `contact.updated`, `plugin.settings.changed`, `connection.changed`.
Prefira tools + prompt quando a decisão depende da interpretação da IA; prefira `message.saved` quando o
gatilho é objetivo e independe da IA.

**Mídia recebida pode não ter arquivo.** Por padrão o WhatsBot guarda só áudios; foto, figurinha, vídeo e
documento são descartados depois que a IA os processou (o usuário muda isso em Painel → Modelos e mídia).
Em `message.saved`, `media_type` continua preenchido, mas o arquivo em `media_path` pode ser apagado logo
em seguida: o payload traz `media_will_discard: true` quando isso vai acontecer. Nunca assuma que
`media_path` existe no disco depois do handler; copie o arquivo na hora se precisar dele, e trate
`media_path` vazio/`None` em qualquer leitura de mensagens salvas (a linha no banco tem `media_state` =
`discarded` ou `removed`). Arquivos de mídia ficam em `statics/media/` e `statics/operator/`, pastas que o
usuário pode apagar pela tela Arquivos: plugin que precisa de um arquivo para sempre deve gravar em
`storages/plugins/<id>/`, nunca nessas pastas.

Filters recebem `(ctx, value)` e devolvem o valor alterado; `None` cancela a etapa:

```python
def outgoing(ctx, value):
    return value

FILTERS = {"filter.reply.part": outgoing}
```

Use filters apenas quando precisar alterar o pipeline. Os mais comuns são `filter.system_prompt`,
`filter.llm.messages`, `filter.tool.args`, `filter.tool.result`, `filter.reply.raw`, `filter.reply.parts`,
`filter.reply.part` e `filter.message.before_save`.

## Validação e testes

`validate_plugin_project` verifica manifesto, compatibilidade, arquivos declarados, imports, dependências,
prefixo e segurança das migrations, sintaxe Python, aplica migrations num banco descartável, carrega os
entrypoints e executa `test_*.py`. A tela ainda precisa ser exercitada no navegador depois da instalação.
Os testes portáteis são funções sem parâmetros e usam `assert` sobre regras reais do plugin:
Durante os testes, `from tools import ...` funciona porque o runner executa na raiz do workspace. Nos
módulos carregados pelo WhatsBot-Lite (`routes.py`, `prompts.py` etc.), use sempre imports relativos entre
arquivos do plugin, como `from .settings import ...`.

```python
from tools import normalize_order

def test_confirmed_order_requires_items():
    try:
        normalize_order({"items": []})
    except ValueError as exc:
        assert "item" in str(exc).lower()
    else:
        raise AssertionError("pedido vazio deveria ser rejeitado")
```

Não declare conclusão antes de receber `valid: true`.
