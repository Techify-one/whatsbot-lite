# PWA do WhatsBot-Lite

Identidade no aparelho: nome exatamente "Whatsbot-lite" (manifest `name`/`short_name`, `<title>`, título
dinâmico, notificações) e ícone verde com selo "LITE". O Whatsbot-pro tem identidade própria (selo "PRO"), então os
dois podem ser instalados lado a lado.

**Ícones:** a arte final é `tools/icon-src/icon.png`; `python tools/generate_pwa_icons.py` (precisa de Pillow, só em
desenvolvimento) apenas redimensiona, sem redesenhar, e gera em `web/static/icons/`: `icon-192/512` (cantos
transparentes), `maskable-512` (fundo verde sólido + arte a 70%, para o Android não cortar o "W" nem o selo),
`apple-touch-icon` (opaco, iOS não tem alpha) e `favicon-32/48.png` + `favicon.ico` (aba do navegador, também servido
em `/favicon.ico`). Os `<link>`/manifest levam `?v=2`: suba o número ao trocar a arte (o `StaticFiles` do lite não
força revalidação). Um app já instalado só troca o ícone quando o Chrome revalida o manifest; no Android pode ser
preciso desinstalar e instalar de novo. Ao mudar o texto de `offline.html`, suba a versão do cache em `sw.js`.

**Instalação:** `pwa.js` expõe `isMobileDevice()` (userAgentData → UA), `installPromptDue()` e
`getInstalledRelatedApps` (só Chrome/Android; o manifest declara `related_applications` apontando para ele mesmo).
`InstallPrompt.js` abre o pop-up 1x por sessão (`sessionStorage`) e "Agora não" adia 7 dias (`localStorage`); o item
"Instalar no celular" (`InstallApp.js`) não é renderizado em modo standalone nem em computador. Nenhum navegador
permite abrir um PWA instalado por script: com o app já instalado o pop-up só orienta a abrir pelo ícone. iOS/Firefox
não informam se está instalado.

**Teclado e altura:** `interactive-widget=resizes-content` no viewport + `utils/viewport.js`, que mantém `--app-h` =
`visualViewport.height` e cancela a rolagem que empurrava o cabeçalho para fora; a raiz usa
`height: var(--app-h, 100dvh)`. **Seleção de texto:** em `display-mode: standalone` o toque longo não seleciona texto
(campos, `pre/code` e `.select-text` continuam selecionáveis).

**Notificações:** com o worker ativo, `showBrowserNotification` usa `registration.showNotification` (exigido pelo
Android em PWA instalado) e o worker trata `notificationclick`. Notificação com o app fechado (push) continua
fora de escopo. **Notch/barras do sistema:** `viewport-fit=cover` + a classe `.app-safe-area` no contêiner raiz.

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

## Atualização para 0.2.10

Este pacote contém código-fonte e a versão em `WHATSBOT_VERSION`. Não é um EXE novo. Faça backup dos dados
antes de atualizar. Para instalação a partir de fonte, pare o app, substitua apenas
os arquivos de código e preserve `storages/`, `statics/`, `logs/`, `venv/`, `.git/`,
`bin/` e `.env`; depois use seu launcher habitual. Se usa Docker/Coolify, reconstrua
a imagem pelo fluxo administrado da sua instalação, somente quando quiser atualizar.

O botão de atualização do painel só detecta uma versão depois de uma release/tag
ser publicada no repositório configurado. Este ZIP local, sozinho, não a publica.
Não envie `main` para publicar esta versão sem verificar o deploy automático do Coolify.

O nome atual do repositório é `Techify-one/whatsbot-lite` (anteriormente
`Techify-one/whatsbot`). O atualizador usa esse endereço diretamente para consultar
a última release estável e baixar o ZIP da tag. Publicar apenas main não basta.
