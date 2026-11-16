## Modo Assistido por Intenções

Novo terceiro modo: a IA monitora silenciosamente e assume apenas tarefas temporárias autorizadas. O modo assistido vem desligado; configurações e conversas existentes são preservadas, inclusive o modo automático já habilitado.

### Incluído
- Tela **Intenções**: criar, editar, testar, habilitar/desabilitar e excluir intenções, com escopos, exemplos, prioridade, confiança, dados necessários, critérios, timeout e ferramentas explicitamente permitidas.
- Arquitetura híbrida: motor persistente no core e contrato versão 1 para plugins registrarem intenções/capacidades. Instalar plugin não habilita uma intenção.
- Jev oficial da TypeSafe AI como opção padrão de classificação; alternativa OpenAI-compatible com saída estruturada. Credencial cifrada localmente, sem fallback pago silencioso.
- Tarefas por conversa, deduplicação persistente por mensagem, prioridade humana, aprovação de operações exatas, simulação sem efeitos, auditoria, limites de taxa/custo estimado, timeout e circuit breaker.
- Modelo desativado `agendar_reuniao`. Exige capacidades reais de disponibilidade/criação de calendário; não simula sucesso quando faltam integrações.

### Atualização e ativação
1. Faça backup do banco/configuração e do diretório de dados; atualize pelo painel para esta release e reinicie o serviço.
2. A migração aditiva Alembic `0016_intents` cria as tabelas sem alterar históricos. SQLite foi testado; SQLAlchemy mantém o contrato PostgreSQL, mas não houve execução contra servidor PostgreSQL nesta entrega.
3. Defina senha do painel e configure o provedor de IA do executor. Abra **Intenções**, escolha classificador, modelo e chave, e salve. A chave local `storages/intent.key` deve acompanhar backups protegidos, nunca Git/release.
4. Configure uma intenção com contatos/grupos explícitos, ferramentas e parâmetros. Teste a classificação antes de habilitar **Assistido por Intenções**. Não é preciso reiniciar por mudanças simples de configuração.
5. Para agendamento, instale/configure plugin real com `calendar.availability` e `calendar.create`, associe suas ferramentas ao modelo e habilite-o para um contato de teste. Peça reunião, escolha horário, confirme data/fuso/participantes e aprove a operação exata no painel. Verifique evento real e conclusão silenciosa antes de ampliar o escopo.

### Validação executada
- 702 testes pytest: runtime/classificador/API/transporte + portabilidade/plugins/migrações.
- 489 verificações do script de endpoints.
- 128 testes Node; build/check Tailwind e compilação de módulos Python.
- Scripts GOWA update (233), proxy (42), provisionamento (27) e custos LLM (26) aprovados.
- A suíte pytest genérica não é coletável: scripts legados executam `sys.exit()` durante importação. Foram executadas as suites explícitas acima e os scripts nos seus formatos suportados.

### Limitações e decisões conservadoras
- Não houve envio real a contatos, chamada real ao Jev, benchmark de precisão PT-BR, agendamento real, validação visual de navegador (Chromium não iniciou devido à restrição de sockets do ambiente) ou teste PostgreSQL. Testes de integrações usam dublês offline.
- Sem plugin de calendário configurado, a função permanece pendente; esta release fornece o contrato, não uma conta de calendário pronta.
- Escritas e ferramentas sem classificação de efeito exigem aprovação específica do proprietário. O executor disponível é a IA da instalação, não um catálogo de agentes independentes.
- O orçamento é estimativo para classificação, não trava de cobrança; custos do executor são separados. Configure limites também no provedor.
- Após reinício, tarefas interrompidas são pausadas para revisão. Operações com resultado incerto conservam o bloqueio da conversa e exigem reconciliação explícita antes de cancelar/transferir; não são repetidas automaticamente. Integrações precisam de idempotência/reconciliação próprias; nenhuma garantia de exactly-once externo é prometida.
- Contextos e resultados necessários à retomada são dados privados no banco. Avalie retenção do provedor/LGPD antes de habilitar contatos sensíveis.

Documentação: [guia completo](https://github.com/Techify-one/whatsbot-lite/blob/v0.3.0/docs/ASSISTED_INTENTS.md) e [contrato de plugins](https://github.com/Techify-one/whatsbot-lite/blob/v0.3.0/agent/PLUGIN_CREATOR.md).
