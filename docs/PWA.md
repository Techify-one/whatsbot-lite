# PWA do WhatsBot-Lite

## Implantação

- O app continua sendo servido pelo FastAPI na raiz do domínio (`/`). Subpastas não são suportadas pelo frontend existente.
- Use HTTPS com certificado válido e proteja o painel com a senha existente antes de disponibilizá-lo na internet. Não exponha portas do GOWA ou do banco. A PWA não cria autenticação nova e não corrige permissões preexistentes de mídia.
- O proxy deve encaminhar `/sw.js`, `/manifest.webmanifest`, `/offline.html` e `/static/icons/*` ao mesmo app. Preserve MIME e Cache-Control desses endpoints; não crie regras de cache do proxy/CDN para API, autenticação ou mídia privada.
- Docker já copia `web/` e `server/`; não precisa de novo serviço, pacote de produção ou build JavaScript. Reinicie a instalação após atualizar os arquivos, para carregar as novas rotas.
- `localhost` é uma exceção segura apenas para desenvolvimento no próprio dispositivo. Um IP HTTP da rede local não é um caminho para instalação completa no celular.

## Privacidade e atualizações

O service worker fornece uma página offline genérica. Não armazena HTML autenticado,
respostas da API, credenciais, WebSockets, chats, anexos ou arquivos de plugins. O
login e o localStorage usados pelo app continuam como antes. Não há fila de envios
offline, background sync ou push adicionado por esta versão.

O worker não força recarregamento durante conversas/formulários. Para ativar um
worker novo que esteja aguardando, feche todas as abas e janelas instaladas deste
WhatsBot-Lite e abra novamente. Recursos e telas normais continuam vindo da rede.

## Validar antes de publicar

1. Rode os testes de endpoints existentes conforme AGENTS.md, os testes PWA e a
   conferência de CSS de `tools/css`. Os testes de navegador usam Chromium e não
   substituem instalação em aparelhos físicos.
2. No HTTPS de homologação, confira manifesto, ícones, MIME do worker, escopo `/`
   e ausência de erros no console. Abra uma rota profunda e recarregue.
3. Android/Chrome: abra o guia no menu, instale, abra pelo ícone e faça login.
4. iPhone/Safari: Compartilhar → Adicionar à Tela de Início → Adicionar. Confira
   abertura standalone, login, teclado, rotação e navegação.
5. Feche o app, tire a rede e abra novamente: deve aparecer somente a página
   genérica. Volte à rede e tente novamente. Confira Cache Storage: nenhum dado
   privado pode aparecer.
6. Teste cancelamento, cliques repetidos, Fechar/Escape, modo escuro e logout.

## Pacote 0.2.10 preparado sem deploy

Este pacote contém código-fonte e a versão em `WHATSBOT_VERSION`. Não é um EXE novo,
e não foi aplicado automaticamente a nenhuma instalação. Faça backup dos dados
antes de atualizar. Para instalação a partir de fonte, pare o app, substitua apenas
os arquivos de código e preserve `storages/`, `statics/`, `logs/`, `venv/`, `.git/`,
`bin/` e `.env`; depois use seu launcher habitual. Se usa Docker/Coolify, reconstrua
a imagem pelo fluxo administrado da sua instalação, somente quando quiser atualizar.

O botão de atualização do painel só detecta uma versão depois de uma release/tag
ser publicada no repositório configurado. Este ZIP local, sozinho, não a publica.
Não envie `main` para publicar esta versão sem verificar o deploy automático do Coolify.
