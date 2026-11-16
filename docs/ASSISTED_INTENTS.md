# Modo assistido por intenções

## Decisão arquitetural

A implementação é **híbrida**: o core controla eventos, classificação, permissões, persistência, concorrência e ciclo de vida; plugins oferecem ferramentas e declarações de intenções. A tela administrativa também permite intenções personalizadas sem mudar código.

Alternativas avaliadas:

| Opção | Vantagem | Motivo da decisão |
| --- | --- | --- |
| Tudo no core | Controle uniforme | Exigiria alterar o core para cada integração; não adotada. |
| Tudo em plugin | Distribuição independente | Observação de mensagens, prioridade humana e exclusão mútua não podem depender de cada integração; não adotada. |
| Híbrida | Política central com integrações extensíveis | Adotada, reaproveitando handler, ferramentas, runner AGNO, painel, banco e migrações existentes. |

O classificador nunca recebe ferramentas executáveis. O executor reutiliza o provedor de IA configurado como planejador JSON, sem acesso nativo a ferramentas; um broker valida a allowlist e os parâmetros fixos antes de chamar executores de plugins. A arquitetura existente é por instalação/banco: não se deve compartilhar o mesmo banco entre tenants independentes. O isolamento de tarefas é por conversa dentro dessa instalação.

Arquivos centrais: `agent/intent_classifier.py`, `intent_config.py`, `intent_schema.py`, `intent_runtime.py`, `db/repositories/intent_repo.py`, `server/routes/intents.py`, `web/static/js/components/IntentsManager.js`. As tabelas são adicionadas por migração Alembic; não é necessário converter históricos existentes.

## Pesquisa e escolha do classificador

A referência **Jev da TypeSafe AI** foi confirmada em documentação oficial: [introdução](https://docs.typesafe.ai/introduction), [Choice](https://docs.typesafe.ai/primitives/choice), [confiança](https://docs.typesafe.ai/confidence). Não é um agente conversacional. A API `POST https://api.typesafe.ai/v1/systemone` usa Bearer token, modelo Jev e perguntas estruturadas. A integração usa `Choice` para escolher uma intenção e `Noul` para abstenção, combinando limiar individual e margem entre categorias. O executor continua separado.

| Alternativa | Avaliação |
| --- | --- |
| Jev | Classificação especializada com probabilidades e saída estruturada; adaptador padrão. O preço oficial consultado de US$0,042/milhão de tokens de entrada e saída gratuita serve apenas como estimativa, não garantia contratual. |
| LLM pequeno com JSON Schema | Flexível para contexto e português informal; adaptador alternativo compatível com Chat Completions. Custo, latência e qualidade variam por modelo. |
| Classificador local treinado / regras | Privacidade e custo marginal favoráveis; exigiria corpus rotulado PT-BR, manutenção de treinamento e avaliação que este projeto não possui. Não adotado como padrão. |

Não foi executado benchmark próprio de precisão em português, negação ou mensagens informais. Probabilidade/confiança do provedor não é garantia de acerto. A janela inclui mensagens do contato e do proprietário para reduzir pendências já resolvidas. Use testes representativos antes de habilitar, mantendo escopos restritos e aprovação humana. Não há fallback pago silencioso. A interface `Classifier.classify` permite trocar o adaptador sem mudar o orquestrador.

## Configurar pela interface

1. Atualize a instalação pelo procedimento normal do projeto, preservando o diretório de dados e fazendo backup antes da migração.
2. Abra engrenagem → **Intenções** (`/intents`). A atualização preserva o modo automático anterior quando já usado; o novo modo assistido e novas intenções não são ativados automaticamente.
3. Configure a IA executora e senha do painel em Configurações. Em Intenções, escolha Jev ou LLM com JSON estruturado, modelo, chave, janela (10–50), timeout, taxa e orçamento estimado. Para o adaptador LLM, configure explicitamente URL HTTPS e modelo compatível; trocar somente o seletor não converte URL/modelo do provedor anterior.
4. A chave fica criptografada no armazenamento da instalação. Campo vazio preserva a chave; a opção de remover exclui o valor salvo. Não há chave exposta de volta ao frontend.
5. Crie a intenção: nome/ID, descrição, exemplos, instruções, dados necessários, critérios, escopos e política. Escolha explicitamente ferramentas e parâmetros fixos. Nenhuma ferramenta é concedida por omissão.
6. Teste classificação com uma conversa autorizada. A simulação não envia mensagens nem chama ferramentas, mas usa a API do classificador e pode custar crédito.
7. Habilite a intenção e selecione Assistido por Intenções. Mudanças são aplicadas sem reinício. Se faltarem chave, IA executora ou senha de painel, a ativação é rejeitada com explicação.

Políticas de início: automática, confirmação do contato, aprovação do proprietário ou não executar. Por segurança, **operações de escrita exigem aprovação específica**, mesmo após início automático. O painel exibe ferramenta e argumentos exatos antes da aprovação. Leia destinatários, datas e efeitos antes de confirmar.

## Fluxo e prioridade humana

Mensagens elegíveis são classificadas fora do recebimento normal. Sem intenção elegível/confiança suficiente, silêncio. Tarefa ativa recebe as respostas subsequentes, sem criar novo agente para cada resposta. Estados persistidos incluem `detected`, `validating`, `executing`, `waiting_owner`, `waiting_contact`, `paused`, `completed`, `cancelled`, `expired`, `transferred`, `failed`.

Há lock local por conversa, unicidade persistida de tarefa ativa e deduplicação de eventos/operações. Conclusão libera a conversa para novas intenções. Resposta manual do proprietário transfere a tarefa para humano. Cancelamento, desativação e revogação de ferramentas impedem novas chamadas. Pausa não executa novas ações; aprovação retoma uma tarefa pausada dentro das mesmas permissões. Estados e resultados incertos após falha/reinício devem ser tratados conservadoramente, nunca repetindo escrita às cegas.

O agente é limitado ao objetivo cadastrado e a rodadas limitadas de planejamento. Ações sensíveis não ganham autorização a partir de texto recebido do contato. A aprovação de início não é aprovação genérica de todos os efeitos futuros.

## Exemplo: agendar reunião

Não há plugin nativo de calendário incluído por esta feature. Instale e configure uma integração real antes de habilitar `agendar_reuniao`. Defina `required_capabilities` como `calendar.availability` e `calendar.create`. Associe ferramenta de disponibilidade e de criação, com calendário fixado nos parâmetros quando possível.

Teste em contato autorizado: “Podemos marcar uma reunião amanhã?”. O executor deve consultar disponibilidade real, pedir/confirmar data, fuso e participantes, oferecer opções e interpretar a resposta no contexto. A criação fica pendente de aprovação específica no painel. Só confirme ao contato após resultado real da integração, incluindo link se fornecido. Sem integração, credenciais ou horário, a tarefa deve apontar configuração pendente/erro ou transferir para humano, nunca simular sucesso.

Teste também horário indisponível, alteração, cancelamento, resposta vaga, falha de API e intervenção manual. Simulação de classificação não é teste ponta a ponta de agendamento.

## Extensão por plugins

Veja `agent/PLUGIN_CREATOR.md`, seção “Intenções assistidas: contrato versão 1”. `plugin.yaml` pode declarar `intents` e metadados `intent_tools`. IDs usam prefixo do plugin. O registro inicial é sempre desabilitado. Ferramentas de outro plugin não podem ser declaradas por esse contrato; o administrador pode escolher ferramentas instaladas para sua intenção personalizada. Configurações administrativas persistem sem reinstalar plugins.

Metadados `effect: read|write` e `capability` identificam o tipo e capacidade. Ferramentas sem metadados são tratadas como escrita. Não marque efeitos mutáveis como leitura. O plugin é código confiável da instalação, não uma sandbox para software malicioso.

## Segurança e operação

- Proteja painel, banco e diretório de dados; a autorização segue a fronteira da instalação existente, não cria autenticação multi-tenant nova.
- Faça backup conjunto da configuração criptografada e da chave local `storages/intent.key` no diretório de dados (ou caminho configurado por `WHATSBOT_INTENT_KEY_FILE`). Sem essa chave, credenciais existentes não são recuperáveis. Não incluir chave, segredos ou backups em Git/releases. Restrinja acesso ao backup; cifre-o no armazenamento operacional.
- Contexto enviado ao classificador é limitado por janela e tamanho; não é resumo permanente. Provedores recebem esse texto: avalie retenção contratual, base legal e LGPD antes de habilitar conversas sensíveis.
- Auditoria registra decisões e resultados técnicos sem credenciais; contexto e resultados necessários à retomada continuam sendo dados privados no banco. Defina retenção/backup adequados à operação.
- Limites de taxa, timeout e circuito de falhas reduzem chamadas em erro. O orçamento é estimativo, não trava financeira garantida: preços variam e limite do provedor deve ser configurado também. O custo da IA executora é separado.
- Não existe garantia de exactly-once em serviços externos. Integrações devem implementar idempotência própria e reconciliação de resultado incerto; não repetir operações de escrita após timeout sem verificar o destino.

## API administrativa

Todas seguem autenticação do painel e envelope `{ok, data}` ou erro:

- `GET /api/intents`, `POST /api/intents`
- `PUT /api/intents/{id}`, `DELETE /api/intents/{id}`
- `GET /api/intents/tools`
- `GET /api/intents/settings`, `PUT /api/intents/settings`
- `POST /api/intents/test` com `{text, phone}`
- `GET /api/intents/tasks` e `GET /api/intents/decisions` (últimas 200 decisões silenciosas)
- `POST /api/intents/tasks/{id}/{action}`: `pause`, `cancel`, `transfer`, `approve`

`api_key` é somente escrita; `clear_api_key: true` remove. Não envie segredos em URLs. A validação de escopos, ferramentas e argumentos é backend, não apenas formulário.

## Validação e limites conhecidos

Os testes automatizados usam provedores/integrações simulados para validar contratos sem enviar mensagens ou criar eventos reais. Resultados e comandos efetivamente executados devem constar na release; este documento não afirma integração real testada. Nenhuma precisão PT-BR foi medida com corpus rotulado. Não há integração de calendário pronta, renovação de credenciais de calendário ou garantia de entrega exactly-once incluída. O executor padrão é a IA da instalação; a presença de `agent_id` no contrato não representa seleção de agentes independentes nesta interface.

### Resultado externo incerto

Timeout não interrompe uma requisição que já chegou ao serviço externo. A tarefa conserva o bloqueio da conversa, inclusive após reinício, edição da intenção ou desativação do modo. A aprovação fica indisponível. Verifique a operação na integração e só então use Cancelar/Transferir confirmando a reconciliação. A API exige `reconciled: true` para liberar uma tarefa nesse estado. Nunca aprove novamente para tentar descobrir se uma operação ocorreu.

### Condições do fornecedor verificadas

A [política da TypeSafe](https://help.typesafe.ai/articles/5974925716) informa processamento nos EUA, sem treinamento sobre entradas/saídas. Retenção padrão não tem prazo fixo; Zero Data Retention é gratuito, mas requer ativação e confirmação na conta. [Preço oficial](https://help.typesafe.ai/articles/5187202169-how-much-does-jev-cost): US$0,042 por milhão de tokens de entrada, saída grátis, sem créditos gratuitos de teste. O fabricante trata contexto longo como experimental e [recomenda avaliações próprias](https://typesafe.ai/blog/jev-1-14). Esses termos não substituem avaliação de LGPD/retenção para a instalação.
