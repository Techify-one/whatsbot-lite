## Classificadores Decisions pelo proxy Techify

Escolha na tela **Intenções** entre cinco modelos oficialmente confirmados:
- Perplexity Decider Flash V1.2 (`perplexity/pplx-decider-flash-v1.2`)
- Perplexity Decider V1.2 (`perplexity/pplx-decider-v1.2`)
- TypeSafe Jev 1.14 (`typesafe/jev-1.14`)
- Google Gemini Decision 1 (`google/gemini-decision-1`)
- OpenAI GPT-6 Luna Decisions (`openai/gpt-6-luna-decisions`)

### Configuração
1. Atualize pelo painel para v0.3.1 e reinicie o serviço.
2. Em **Intenções**, selecione **Techify · Decisions**. A credencial Techify já configurada no WhatsBot é reutilizada; não precisa de segunda chave.
3. Escolha um modelo disponível no catálogo autenticado e salve. O teste fica bloqueado enquanto houver alterações não salvas.
4. Use **Testar sem executar** antes de ativar para contatos reais. Esse botão pode consumir créditos de classificação, mas não envia mensagens nem executa ferramentas.

A chamada usa a API **Decisions** no proxy, não chat/completions. Catálogo usa filtro `output_modalities=decisions`; IDs, modalidade e endpoint precisam corresponder ao contrato confirmado. Sem catálogo/chave/modelo compatível, falha fechado e explica indisponibilidade, sem fallback pago.

Novas configurações pré-selecionam Decider Flash V1.2, mas modo assistido continua desligado. Configurações legadas salvas são preservadas sem migração silenciosa. Credenciais Techify e legadas permanecem separadas e nunca são reutilizadas entre destinos. Não há migração de banco nesta versão.

### Preço e limites
Só são exibidos metadados de preço retornados pelo catálogo, em USD/token, sem prometer cobrança final Techify. Quando falta preço válido, o custo fica desconhecido; a reserva conservadora do orçamento é separada e não é cotação. Extras de busca não são habilitados automaticamente. Retenção de dados varia por fornecedor escolhido.

### Verificações e limitações
- Testes offline cobrem os cinco modelos, URL e credencial do proxy, aliases seguros para intenções, recusa/tipo incorreto, catálogo malformado/indisponível, separação de credenciais e configuração da interface.
- 715 testes pytest, 489 checks de endpoints e 131 testes Node aprovados; sintaxe Python/JS e build/check CSS verificados. O workflow desta release repete as verificações antes da publicação.
- IDs/contratos confirmados em documentação e catálogo públicos oficiais. Encaminhamento Techify verificado no código do proxy; GET sem credencial no deployment retornou 403, como esperado.
- Não houve chamada paga, inferência autenticada real no deployment, benchmark de precisão, teste PostgreSQL ou validação visual de navegador. Presença no catálogo não garante disponibilidade de inferência futura.
- Integração de calendário continua dependendo de plugin real; esta versão não cria conexão Google Calendar.

[Guia e fontes oficiais](https://github.com/Techify-one/whatsbot-lite/blob/v0.3.1/docs/ASSISTED_INTENTS.md).
