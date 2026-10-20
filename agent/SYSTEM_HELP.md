# Base oficial de ajuda do WhatsBot-Lite

Esta é a fonte principal para orientar quem usa o WhatsBot-Lite. Os links usam `{{base_url}}`, que é substituído automaticamente pelo domínio ou pelo endereço local aberto pela pessoa.

Ao responder:

- Dê uma orientação curta, em linguagem comum.
- Inclua somente o link mais específico para a ação solicitada.
- Não liste configurações que a pessoa não perguntou.
- Se esta base não cobrir a dúvida, consulte as referências do sistema antes de responder.
- Se a interface ou o comportamento encontrado nas referências divergir desta base, siga a versão atual do sistema e explique o caminho correto.

## Instalar no Android ou iPhone

Abra o [WhatsBot-Lite]({{base_url}}/) pelo endereço HTTPS da sua instalação. Depois de entrar e concluir a configuração inicial, abra a engrenagem no canto superior direito e escolha **Instalar no celular**.

- **Android:** use o Chrome. Quando disponível, toque em **Instalar agora** e confirme no navegador. Também pode usar o menu do Chrome → **Instalar app** ou **Adicionar à tela inicial**.
- **iPhone/iPad:** abra no Safari, toque em **Compartilhar → Adicionar à Tela de Início**. Se houver a opção **Abrir como App da Web**, deixe ativada e toque em **Adicionar**.
- A opção **Fechar** sai do guia sem instalar. Cancelar o aviso do navegador mantém o painel funcionando normalmente.
- O endereço precisa ser acessível pelo celular e usar HTTPS válido. `localhost` no celular não abre o servidor do computador; HTTP com IP da rede local não oferece a instalação completa.
- O ícone abre o painel como aplicativo. É preciso internet e manter o servidor WhatsBot-Lite e a conexão WhatsApp funcionando. O bot não passa a rodar dentro do celular.
- Sem conexão, uma nova abertura mostra uma página genérica para tentar novamente. Conversas, mídias, credenciais e respostas da API não são guardadas no cache offline do PWA. O login existente continua usando o armazenamento do navegador e pode ser solicitado de novo ao instalar.
- Esta versão não adiciona notificações push com o aplicativo fechado. Para remover o ícone, use as opções do sistema do celular.

## Conversas e painel principal

### Conversas do WhatsApp

Esta tela mostra as conversas, mensagens, contatos e o estado da conexão com o WhatsApp. É nela que a pessoa acompanha e responde os atendimentos.

[Abrir conversas]({{base_url}}/)

### Ler mensagens antigas de uma conversa

Ao abrir uma conversa, o WhatsBot-Lite mostra primeiro as mensagens mais recentes, para que ela abra rápido mesmo quando o histórico tem milhares de mensagens. Nada é apagado: o histórico completo continua guardado.

- Para ver mensagens mais antigas, role a conversa para cima. O botão **Carregar mensagens anteriores** no topo faz o mesmo e mais mensagens aparecem sem você perder o lugar onde estava.
- A busca da lista de conversas procura em todo o histórico, inclusive nas mensagens que ainda não apareceram na tela. Clique no resultado e a conversa abre direto na mensagem encontrada.
- Se uma mensagem responde a outra bem antiga, clique na citação e a conversa vai até a mensagem original.
- Depois de saltar para uma mensagem antiga, aparece o botão **Ir para as mensagens mais recentes**. Ao enviar uma mensagem, a conversa volta sozinha ao final.
- Áudios e fotos de perfil só são baixados quando chegam perto da tela, então uma conversa longa não baixa tudo de uma vez.

O atalho de busca do navegador (Ctrl+F) só encontra o que já está carregado na tela. Para procurar em todo o histórico, use a busca do WhatsBot-Lite.

[Abrir conversas]({{base_url}}/)

### Abrir a conversa de quem falou em um grupo

Dentro de um grupo, o nome de quem enviou cada mensagem aparece acima do texto. Quando o nome está em azul e fica sublinhado ao passar o mouse, clique nele para falar com essa pessoa em particular:

- Se a pessoa já é um contato, a conversa dela abre na hora.
- Se ainda não é, o WhatsBot-Lite mostra o aviso sobre os riscos de iniciar uma conversa por aqui. Confirme para criar o contato e abrir a conversa, ou peça para receber o link no seu próprio WhatsApp e começar pelo aplicativo oficial.

O nome só vira clicável para números do Brasil e depois que a lista de participantes do grupo terminar de carregar. Clicar não cria nenhum contato por si só.

[Abrir conversas]({{base_url}}/)

### Abas de configuração do painel

O painel separa as configurações em três abas:

- **Agente** reúne ativação da IA, instruções, contexto e comportamento das respostas. [Abrir aba Agente]({{base_url}}/painel?aba=agente)
- **Modelos e mídia** reúne chave de API, modelos, leitura de áudios, imagens e documentos e o download automático de arquivos. [Abrir aba Modelos e mídia]({{base_url}}/painel?aba=modelos-midia)
- **Sistema** reúne acesso, atualizações, banco de dados e o motor do WhatsApp. [Abrir aba Sistema]({{base_url}}/painel?aba=sistema)

Cada opção abaixo tem um link direto. Ao abrir esse link, o WhatsBot-Lite seleciona a aba certa e leva a pessoa até a configuração.

### Ativar ou interromper respostas automáticas

Em **Painel → Agente → Automação**, use **Respostas automáticas**. Quando estiver desativado, o WhatsBot-Lite continua recebendo as conversas, mas não responde automaticamente.

[Abrir respostas automáticas]({{base_url}}/painel?aba=agente#auto-reply)

### Escolher se novos contatos começam com IA

Em **Painel → Agente → Automação**, altere **IA padrão para novos contatos**. A mudança vale para contatos que chegarem depois dela; cada conversa também pode ter seu próprio estado de IA.

[Abrir IA para novos contatos]({{base_url}}/painel?aba=agente#default-ai)

### Respostas em grupos

Em **Painel → Agente → Automação**, escolha se a IA responde sempre, somente quando for mencionada ou nunca. A opção vale para grupos que estejam com a IA ativada.

[Abrir respostas em grupos]({{base_url}}/painel?aba=agente#groups)

### Religar a IA automaticamente na conversa

A IA fica desligada em uma conversa quando ela transfere o atendimento para uma pessoa, quando alguém a desliga na própria conversa ou quando novos contatos começam com a IA desligada. Em **Painel → Agente → Automação**, marque **Religar a IA automaticamente na conversa** para ela voltar sozinha. Por padrão essa opção vem desligada.

Passos comuns:

1. Marque **Religar a IA automaticamente na conversa**.
2. Em **Quando religar**, escolha uma das formas:
   - **Minutos sem mensagens**, **Horas sem mensagens** ou **Dias sem mensagens**: informe o tempo. A contagem começa na última mensagem do cliente ou do atendente. Se a IA foi desligada depois dessa mensagem, a contagem começa nesse momento. As respostas da própria IA não contam.
   - **Dias da semana e horário**: marque os dias e escolha a hora. Nesses dias, nesse horário, a IA volta nas conversas em que está desligada. O horário vale no fuso do navegador de quem salvou.
3. Em **Dono da última mensagem**, escolha **Cliente**, **Usuário/atendente** ou **Cliente ou atendente (qualquer um)**. A IA só volta nas conversas cuja última mensagem foi enviada por quem você escolheu. Esse filtro vale nas duas formas.
4. Clique em salvar.

Condições importantes:

- Ao religar uma conversa que foi transferida, a etiqueta **transferido_atendente** é removida.
- **Religar IA em grupos e mensagens arquivadas** vem desmarcado: grupos e conversas arquivadas continuam como estão. Se marcar, eles também são religados, mas nos grupos a IA ainda segue a opção **Resposta da IA em grupos**.
- A opção **Respostas automáticas** precisa estar ativa, senão a IA não responde mesmo religada.
- O contato usado para pedir a chave de acesso nunca é religado.

[Abrir religar a IA automaticamente]({{base_url}}/painel?aba=agente#ai-reactivation)

## IA, modelos e instruções

### Alterar as instruções, o prompt ou a personalidade do agente

Em **Painel → Agente → Comportamento da IA**, edite **Instruções** e salve. Esse texto define como a IA deve falar e agir nas conversas.

[Abrir instruções do agente]({{base_url}}/painel?aba=agente#prompt)

### Alterar a chave de API

Em **Painel → Modelos e mídia → API e Modelos**, altere a chave usada pelo serviço de IA e salve.

[Abrir chave de API]({{base_url}}/painel?aba=modelos-midia#api-key)

### Alterar o modelo de IA

Em **Painel → Modelos e mídia → API e Modelos**, escolha o modelo principal. Ele será usado nas respostas das conversas e no Chat.

[Abrir modelo de IA]({{base_url}}/painel?aba=modelos-midia#model)

### Alterar o modelo de melhoria

Em **Painel → Modelos e mídia → API e Modelos**, escolha o modelo usado para analisar respostas marcadas como incorretas. Se ficar vazio, o WhatsBot-Lite usa o modelo principal.

[Abrir modelo de melhoria]({{base_url}}/painel?aba=modelos-midia#improvement-model)

### Alterar quantas mensagens entram no contexto

Em **Painel → Agente → Comportamento**, altere **Mensagens de contexto**. Um número maior dá mais histórico à IA, mas pode aumentar o uso de tokens.

[Abrir mensagens de contexto]({{base_url}}/painel?aba=agente#context)

### Agrupar mensagens enviadas em sequência

Em **Painel → Agente → Comportamento**, altere o tempo de **Agrupamento de mensagens**. Durante esse intervalo, mensagens seguidas da mesma pessoa são reunidas antes da resposta.

[Abrir agrupamento de mensagens]({{base_url}}/painel?aba=agente#batch)

### Dividir uma resposta em várias mensagens

Em **Painel → Agente → Comportamento**, ative ou desative **Dividir respostas** e ajuste o intervalo entre as partes.

[Abrir divisão das respostas]({{base_url}}/painel?aba=agente#split-messages)

## Áudios, imagens e documentos

### Parar ou configurar a leitura de documentos

Em **Painel → Modelos e mídia → API e Modelos**, desative **Ler documento** para o WhatsBot-Lite deixar de extrair o conteúdo de PDFs e outros documentos recebidos.

[Abrir leitura de documentos]({{base_url}}/painel?aba=modelos-midia#document-transcription)

### Parar ou configurar a descrição de imagens

Em **Painel → Modelos e mídia → API e Modelos**, desative **Descrever imagem** para a IA deixar de analisar automaticamente as imagens recebidas.

[Abrir descrição de imagens]({{base_url}}/painel?aba=modelos-midia#image-transcription)

### Configurar a transcrição de áudios recebidos

Em **Painel → Modelos e mídia → API e Modelos**, escolha o modo de **Transcrição de áudio**, onde ela aparece e, quando disponível, o texto colocado antes da transcrição.

[Abrir transcrição de áudio]({{base_url}}/painel?aba=modelos-midia#audio-transcription)

### Escolher quais arquivos recebidos o WhatsBot guarda

Em **Painel → Modelos e mídia → Download automático de mídia**, há uma tabela com áudios, fotos e figurinhas, vídeos e documentos. Para cada tipo, marque se o arquivo deve ser guardado nas **conversas individuais** e nos **grupos**, e clique em **Salvar configurações** no fim do painel. O padrão guarda só os áudios; fotos, figurinhas, vídeos e documentos não são guardados, o que poupa muito espaço, principalmente em grupos movimentados.

O que não for guardado continua aparecendo na conversa como “Foto não baixada” (ou Vídeo, Documento...), e a IA ainda descreve a imagem, lê o documento ou transcreve o áudio antes de o arquivo ser descartado, então o texto fica na conversa. Quem quiser ver o arquivo depois pode clicar em **Baixar** no balão: o WhatsBot tenta buscá-lo de novo no WhatsApp. Isso só funciona se o WhatsApp ainda tiver o arquivo; mensagens com mais de algumas semanas costumam não estar mais disponíveis, e o aviso aparece no próprio balão.

[Abrir download automático de mídia]({{base_url}}/painel?aba=modelos-midia#download-midia)

### Gravar áudio dentro do Chat

No Chat, use o botão de microfone ao lado do campo de mensagem. É possível cancelar a gravação ou parar; ao parar, o áudio é transcrito e a transcrição é enviada como mensagem.

[Abrir Chat]({{base_url}}/chat)

## Atendimento e avisos

### Aviso de transferência para uma pessoa

Em **Painel → Agente → Comportamento**, configure o alerta mostrado quando a ferramenta de transferência para atendimento humano for usada.

[Abrir alerta de transferência]({{base_url}}/painel?aba=agente#transfer-alert)

### Aviso de saldo baixo

Em **Painel → Agente → Comportamento**, ative ou desative o aviso de saldo baixo e defina o valor que dispara o alerta.

[Abrir aviso de saldo baixo]({{base_url}}/painel?aba=agente#low-balance)

### Marcar conversas como lidas ou não lidas

Em **Painel → Agente → Comportamento**, use **Marcar conversas** para marcar todas como lidas ou todas como não lidas. Para mudar somente uma conversa, clique com o botão direito sobre o contato na lista.

[Abrir estado de leitura]({{base_url}}/painel?aba=agente#mark-conversations)

## Custos e diagnóstico

### Ver tokens, cache e valores gastos

A página **Custos** mostra o consumo e os valores estimados das chamadas de IA. No Chat, o resumo de tokens, cache, custo da resposta e total da conversa aparece quando a execução termina.

O valor aparece como **Custo estimado** porque é calculado pelo preço listado de cada modelo; o valor realmente cobrado pelo provedor pode ser um pouco diferente. O cartão **Cache da entrada** mostra quanto do texto enviado à IA foi reaproveitado de chamadas anteriores, o que sai mais barato, e a **Economia estimada** que isso gerou. A tabela por contato tem a coluna **Cache**. Períodos anteriores a esse recurso não têm dado de cache e aparecem com "—"; eles não entram na porcentagem.

[Abrir custos de IA]({{base_url}}/costs)

### Ver o que ocupa espaço e apagar arquivos

A página **Arquivos** (menu da engrenagem → **Arquivos**) mostra quanto espaço o WhatsBot está usando e lista os arquivos recebidos e os que você enviou pelo painel. Nela é possível ordenar por mais recentes, mais antigos, mais pesados ou por tipo, filtrar por nome, texto, conversa, período e tipo (veja **Filtrar a lista** abaixo), ver um arquivo antes de decidir e apagar um ou vários de uma vez. Todos os arquivos ficam na mesma lista, inclusive os que você recuperou com o botão **Baixar** da conversa; a coluna **Conversa** diz de quem veio cada um. Se aparecer alguma pasta (de arquivos baixados em versões antigas), é só clicar nela para abrir. Antes de apagar, a tela mostra quantos MB serão liberados. Apagar não pode ser desfeito, mas as conversas continuam: a mensagem passa a mostrar “removido para liberar espaço”, com o botão **Baixar** para tentar buscar o arquivo de novo no WhatsApp (só funciona se ele ainda estiver lá).

**Nome dos arquivos.** Quando o arquivo chega como **documento** (no WhatsApp, o clipe → Documento), a lista mostra o nome que a pessoa deu a ele, por exemplo `Teste.mp4` ou `Contrato.pdf`, e o botão **Baixar** salva o arquivo com esse mesmo nome. O mesmo vale para o que você envia pelo painel: aparece e baixa com o nome do arquivo que você escolheu. Já **fotos, vídeos e áudios enviados pela galeria ou gravados na hora** chegam do WhatsApp sem nome nenhum, então a lista mostra um código (por exemplo `1792159285-ec185065-….jpg`); para saber de quem é e quando chegou, use as colunas **Conversa** e **Data e hora**. Se você quiser que um vídeo ou uma foto chegue com o nome original, peça para a pessoa enviar como **Documento**. Documentos que chegaram antes desta melhoria também aparecem com o nome certo. A opção **Por nome** ordena pelos nomes mostrados.

**Filtrar a lista.** Acima da lista há campos para achar arquivos específicos. Eles funcionam juntos (por exemplo, fotos de um grupo dos últimos 30 dias) e valem tanto para **Recebidos** quanto para **Enviados por você**:

- **Buscar por nome ou texto**: procura pelo nome do arquivo e também pelo texto da mensagem que o trouxe. Isso inclui a legenda de uma foto, a **transcrição** de um áudio (a fala convertida em texto) e a **descrição** que a IA escreveu de uma imagem. Assim dá para achar um áudio sem nome procurando uma palavra que a pessoa disse. Letras maiúsculas, minúsculas e acentos não importam (“construcao” acha “construção”). Quando o arquivo aparece pelo texto, a linha mostra “na mensagem:” com o trecho encontrado. Só há texto para buscar se a transcrição de áudio, a descrição de imagens ou a leitura de documentos estavam ligadas quando o arquivo chegou; sem isso, o arquivo só é achado pelo nome ou pela legenda.
- **Conversa (nome, número ou grupo)**: mostra só os arquivos de um contato ou grupo. Ao digitar, aparece uma lista com a **foto, o nome e o número** de cada contato encontrado, para você não confundir dois clientes com o mesmo nome. Pode digitar parte do nome, o número do jeito que você escreve (por exemplo `(11) 99999-0001`, com ou sem o 55 e com ou sem o 9 extra) ou o nome do grupo. Clique no contato (ou use as setas do teclado e **Enter**) para ver só os arquivos **daquela** conversa; o campo passa a mostrar a foto, o nome e o número escolhidos, e o **×** ao lado remove o filtro para você escolher outra. Se você apenas digitar o nome e não escolher ninguém na lista, a tela mostra os arquivos de todos os contatos que combinam com o texto, inclusive os que têm o mesmo nome. Grupos aparecem na lista como “Grupo”, sem número. Se aparecer “Nenhuma conversa encontrada”, confira o nome ou o número digitado.
- **De** e **Até**: escolhem o período. O dia escolhido em **Até** entra inteiro. Os botões **Hoje**, **Últimos 7 dias** e **Últimos 30 dias** preenchem as datas com um clique. A data considerada é a que aparece na coluna **Data e hora**.
- **Tipo de conversa**: só conversas individuais ou só grupos. Grupos costumam juntar muita mídia que ninguém precisa guardar.
- **Tipo de arquivo**: fotos, vídeos, áudios ou documentos.

Enquanto houver filtro, a lista aparece sem pastas e o rodapé mostra quantos arquivos e quantos MB o filtro encontrou, o que ajuda a saber quanto espaço você liberaria. O botão **Limpar filtros** volta ao normal. Os filtros continuam ao trocar entre **Recebidos** e **Enviados por você**. Arquivos que nenhuma conversa usa (**Sem conversa**) não aparecem quando você filtra por conversa ou por tipo de conversa, porque não pertencem a nenhuma. Se nada combinar com os filtros, a tela avisa e oferece **Limpar filtros**.

**Selecionar tudo o que o filtro encontrou.** A lista mostra 100 arquivos por vez (o botão **Mostrar mais** carrega os próximos), mas quando há filtro a caixinha do topo da tabela, ao lado de **Nome**, seleciona **todos** os arquivos que o filtro encontrou, sejam 300 ou 3.000, mesmo os que ainda não apareceram na lista. O resumo acima da lista diz quantos arquivos e quantos MB estão selecionados e avisa quando parte deles ainda está fora da tela. Em seguida, **Apagar selecionados** mostra a confirmação com o total que será liberado. Sem filtro, a caixinha continua selecionando apenas o que está na lista. Desmarcar a caixinha ou mudar qualquer filtro limpa a seleção.

Para ver um arquivo, clique no nome dele na lista: ele abre numa janela, sem precisar baixar. Fotos (inclusive as enviadas como documento, como `.jfif`), vídeos, áudios, PDFs e arquivos de texto (`.txt`, `.csv`, `.json`, `.log` e parecidos) aparecem ali mesmo. Em PDFs há também **Abrir em nova aba**, e todo arquivo aberto tem o botão **Baixar**. Para fechar, use **Fechar**, a tecla Esc ou clique fora da janela. Arquivos de Word, Excel, zip e fotos de iPhone (`.heic`) nem sempre abrem no navegador: nesses casos a janela avisa e oferece o botão **Baixar** para abrir no computador. Páginas da web (`.html`) e imagens `.svg` não são exibidas por segurança e só podem ser baixadas.

Cada arquivo mostra a coluna **Data e hora** (no celular, a data e a hora aparecem junto do tamanho), com o dia e o horário em que ele foi salvo no WhatsBot, no fuso do seu navegador. Em uma pasta, vale o horário do arquivo mais recente dela.

A coluna **Conversa** (no celular, o nome logo abaixo do arquivo) mostra de qual contato ou grupo o arquivo veio. Esse nome é um link: ao clicar, o WhatsBot abre a conversa já na mensagem que trouxe o arquivo, que fica destacada por alguns segundos, mesmo que seja uma mensagem muito antiga. Para voltar à lista de arquivos, use o botão voltar do navegador. Se o arquivo não tem nenhuma mensagem ligada a ele, a coluna mostra “Sem conversa”, sem link.

A tela só mostra o que é seguro apagar. Conversas, contatos, configurações, a conexão com o WhatsApp e os plugins nunca aparecem nem são mexidos; eles entram apenas no total como “Sistema e dados do app”. A opção **Só arquivos sem conversa** lista arquivos que nenhuma mensagem usa mais.

Se você quiser acompanhar o espaço, use **Definir um limite de espaço** (em GB) para ver a barra de uso; sem limite, a tela mostra só o total. Quando o limite é definido pela hospedagem, o campo fica bloqueado e a tela avisa.

[Abrir arquivos]({{base_url}}/arquivos)

### Ver o que aconteceu durante uma resposta

A página **Execuções** mostra as execuções e permite abrir os detalhes usados para diagnosticar um atendimento.

[Abrir execuções]({{base_url}}/executions)

### Ativar ou desativar ferramentas da IA

A página **Ferramentas** lista as ações disponíveis para a IA e permite controlar quais podem ser usadas.

[Abrir ferramentas]({{base_url}}/tools)

### Alterar quantas execuções ficam guardadas

Em **Painel → Sistema → Avançado**, altere **Execuções salvas**.

[Abrir limite de execuções]({{base_url}}/painel?aba=sistema#max-executions)

## Chat e projetos de plugin

### Usar a ajuda do sistema

Abra o **Chat** e use o projeto **WhatsBot-Lite — Ajuda do sistema** para perguntar como usar ou configurar o WhatsBot-Lite. Cada conversa possui seu próprio link.

[Abrir Chat]({{base_url}}/chat)

### Usar o Chat no celular

No celular, toque no botão de menu no alto do Chat para abrir os projetos e as conversas. Depois de escolher uma conversa, o menu fecha para deixar a tela livre. Os arquivos de um projeto abrem em tela cheia, e os controles de modelo, áudio e envio ficam organizados abaixo da mensagem. Para mudar a ordem dos projetos pelo celular, use as setas para cima e para baixo.

[Abrir Chat no celular]({{base_url}}/chat)

### Criar ou alterar um plugin

A ajuda do sistema não cria plugins. No **Chat**, clique no botão **+** no topo da barra lateral esquerda, crie um projeto e descreva com suas palavras o que o plugin deve fazer. Se houver poucos detalhes, o criador fará perguntas curtas antes de começar. O formato completo dos plugins já é carregado pelo criador; depois de entender o pedido, ele trabalha diretamente nos arquivos e executa a validação, sem pesquisar o código do WhatsBot-Lite. É possível abrir outra página ou fechar o navegador durante a criação: o trabalho continua no servidor e o andamento reaparece ao voltar. Quando terminar, a conversa mostra **Sim, instalar**. Essa oferta continua disponível ao reabrir a conversa. Também é possível escrever “instale por favor” para autorizar a instalação direta pelo WhatsBot-Lite.

[Abrir criador de plugins]({{base_url}}/chat)

### Instalar, configurar ou consultar plugins

A página **Plugins** lista os plugins instalados. Nela é possível importar um arquivo de plugin, ativar, desativar, configurar, exportar ou remover um plugin. As opções exatas dependem de cada plugin.

[Abrir plugins]({{base_url}}/plugins)

### Organizar projetos e conversas do Chat

No Chat, projetos podem ser renomeados, removidos da lista e arrastados para mudar de posição. Remover um projeto é uma exclusão lógica e não apaga os arquivos nem o plugin. Conversas podem ser renomeadas ou apagadas separadamente, e um projeto aberto pode ser recolhido sem abrir outro.

[Abrir Chat]({{base_url}}/chat)

## Segurança, dados e atualizações

### Criar, trocar ou remover a senha do painel

Em **Painel → Sistema → Avançado**, use **Senha do painel**. Sem uma senha configurada, qualquer pessoa que alcançar o endereço do WhatsBot-Lite poderá abrir o painel.

[Abrir senha do painel]({{base_url}}/painel?aba=sistema#password)

### Configurar ou migrar o banco de dados

Em **Painel → Sistema → Banco de dados**, consulte o banco em uso ou informe uma conexão PostgreSQL e execute a migração disponível na tela.

[Abrir banco de dados]({{base_url}}/painel?aba=sistema#database)

### Atualizar o WhatsBot-Lite

Quando uma nova versão estiver disponível, o WhatsBot-Lite mostra um aviso ao abrir ou atualizar a página. Nele é possível atualizar agora, ignorar somente aquela versão ou nunca receber avisos. Se uma versão for ignorada, a próxima volta a ser avisada. Para reativar ou desligar os avisos, abra **Painel → Sistema → Avançado → Atualizar WhatsBot-Lite**. Quando uma atualização é aplicada, o WhatsBot-Lite reinicia automaticamente para carregar a nova versão.

[Abrir atualizações]({{base_url}}/painel?aba=sistema#update)

### Ativar ou desativar os avisos de novas versões

Em **Painel → Sistema → Avançado → Atualizar WhatsBot-Lite**, altere **Avisar quando houver uma nova versão** e salve. A verificação manual continua disponível mesmo com os avisos desligados.

[Abrir avisos de atualização]({{base_url}}/painel?aba=sistema#update-notifications)

### Configurar ou atualizar o motor do WhatsApp

Em **Painel → Sistema → GOWA**, consulte a versão, atualização e opções de conexão do motor responsável pelo WhatsApp.

[Abrir versão e atualização do GOWA]({{base_url}}/painel?aba=sistema#gowa-version)

### Configurar o proxy da conexão do WhatsApp

Em **Painel → Sistema → GOWA**, abra **Proxy da conexão do WhatsApp**. É possível informar IP e porta ou uma URL de proxy, testar a conexão e salvar. Ao salvar, o motor do WhatsApp reinicia e se reconecta.

[Abrir proxy do WhatsApp]({{base_url}}/painel?aba=sistema#gowa-proxy)

### Ativar ou desativar a verificação automática do GOWA

Em **Painel → Sistema → GOWA**, altere **Verificar atualizações do GOWA diariamente**. Essa opção usa o botão geral **Salvar configurações** no fim do painel.

[Abrir verificação automática do GOWA]({{base_url}}/painel?aba=sistema#gowa-auto-update)

## Telas dos plugins

As telas criadas por plugins aparecem no menu da engrenagem e usam a largura disponível do painel. Elas se
adaptam ao celular e ao tema claro ou escuro. A forma de organizar os dados muda conforme a finalidade de
cada plugin; listas, agendas, formulários e relatórios podem ter estruturas diferentes.

## Quando esta base não tiver a resposta

Pesquise nesta ordem:

1. A interface e as rotas atuais do WhatsBot-Lite.
2. A documentação e o código diretamente relacionados à dúvida.
3. O manifesto e o código dos plugins instalados, quando a dúvida envolver um plugin.
4. A estrutura e as migrations do banco, quando forem necessárias para confirmar o comportamento.

Não leia nem revele registros de conversas, contatos, credenciais ou outros dados privados. Depois da pesquisa, traduza a descoberta para passos simples e inclua o link direto disponível.

### Origem das atualizações

O repositório WhatsBot foi renomeado para WhatsBot-Lite. O atualizador consulta
diretamente a última release estável de `Techify-one/whatsbot-lite`, não a branch
main. Depois que uma nova release é publicada, ela pode levar até cinco minutos
para aparecer devido ao cache da consulta. Abra [Atualizar WhatsBot-Lite]({{base_url}}/painel?aba=sistema#update).
