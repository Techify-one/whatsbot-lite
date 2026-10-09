# WhatsBot-Lite 0.2.10 — PWA

Versão preparada a partir do commit 43f0415e6c95d7e6d736d09a1bb7ab0334ee67ce,
sem publicar no GitHub, alterar produção ou atualizar sua instalação.

## O que mudou

- Manifesto para abrir o painel como app no Android/iPhone.
- Ícones de instalação, inclusive ícone Apple e maskable.
- Menu da engrenagem → **Instalar no celular**, com instalação pelo navegador
  quando disponível e orientações para Safari.
- Página genérica de conexão indisponível. Apenas essa página é guardada pelo
  service worker; conversas, mídias, credenciais e respostas da API não são
  adicionadas ao cache offline. A autenticação existente permanece igual.

## Antes de atualizar

Este é um pacote de código-fonte, não um novo executável Windows compilado. Faça
backup da instalação e dos dados. Se houver código local modificado, revise o patch
antes de substituir arquivos. Preserve a pasta `bin` existente, inclusive a versão
GOWA que você já usa; não substitua esse binário para instalar o recurso PWA.

### Instalação a partir de fonte

1. Pare o WhatsBot-Lite.
2. Faça backup de `storages/`, `statics/`, `logs/`, `.env` e demais configurações.
3. Copie o código de `codigo-fonte/` para sua instalação, preservando `storages/`,
   `statics/`, `logs/`, `venv/`, `.git/`, `bin/` e `.env`. Não apague essas pastas.
   Nenhuma nova dependência de produção foi adicionada nesta versão.
4. Inicie pelo launcher habitual. Reiniciar é necessário para as rotas PWA.
5. Confira a versão 0.2.10 no painel e teste o acesso HTTPS pelo celular.

Se usa Docker/Coolify, atualize o código e reconstrua a imagem pelo processo
administrado da sua instalação quando desejar. Confirme branch/gatilhos antes de
fazer push: o projeto documenta deploy automático. Não aplique este ZIP sobre um
container ativo. O ZIP não cria release/tag; sozinho não faz a nova versão aparecer
no botão de atualização do painel.

## Instalar no celular

Use o endereço HTTPS válido da sua instalação, acessível pelo celular. Ative a senha
do painel antes de expô-lo à internet. Não exponha GOWA nem banco de dados.

- Android/Chrome: engrenagem → **Instalar no celular** → **Instalar agora**, quando
  disponível; ou menu do Chrome → **Instalar app / Adicionar à tela inicial**.
- iPhone/iPad/Safari: **Compartilhar → Adicionar à Tela de Início**. Quando aparecer,
  mantenha **Abrir como App da Web** ativado e confirme **Adicionar**.

O servidor e GOWA continuam ligados no computador/servidor, e o celular precisa de
conexão. Não há WhatsApp offline, transferência do bot para o telefone, fila offline
ou novas notificações push com o app fechado. A instalação pode pedir novo login.

## Verificações realizadas

- 14 testes Node aprovados: ciclo do prompt, cancelamento, clique repetido,
  falhas de registro, contexto seguro, standalone/iPad e política do worker.
- 4 testes FastAPI isolados aprovados: rotas, MIME/cabeçalhos e contrato de integração.
- Verificação de sintaxe dos arquivos JS modificados, compilação dos módulos Python
  modificados, revisão independente de privacidade/autenticação e git diff --check.

## Limitações da validação

- Suíte completa de endpoints: bloqueada por dependências ausentes (SQLAlchemy,
  AGNO e outras); tentativa de instalar requirements bloqueada pelo proxy (403).
- Conferência de Tailwind: bloqueada pela ausência do CLI local 3.4.17. O CSS novo
  usa classes próprias em custom.css; nenhum CSS utilitário foi editado manualmente.
- Teste visual Chromium: bloqueado pelo ambiente (`socket() Operation not permitted`).
- Instalação real em Android/iPhone, teclado, rotação, botão Voltar e integração com
  sua instância HTTPS ainda precisam ser validados. Não foram apresentados como aprovados.

Consulte `codigo-fonte/docs/PWA.md` para a lista completa de verificação e o
relatório de validação no pacote. O patch aplica-se somente ao baseline indicado.
