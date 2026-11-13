"""Códigos de aviso do Importar/Exportar: a lista ÚNICA do que o motor pode dizer.

O servidor manda CÓDIGO, nunca texto com dado (nome, telefone, conteúdo): a frase que o usuário lê
vive só no frontend (``WARNING_LABELS`` em ``web/static/js/utils/portabilityLogic.js``). Dois testes
mantêm as duas pontas honestas:

* ``tests/test_portability_warnings.py`` varre o código-fonte do motor e falha se alguma chamada
  ``env.warn(..., "codigo")`` usar um código que não esteja aqui — e se aqui sobrar um código que
  nenhuma chamada emite (lista morta engana quem escreve a frase);
* o mesmo teste lê ``portabilityLogic.js`` e falha se algum código daqui ficar sem frase na tela, ou se
  a tela tiver frase para código que o motor não conhece.

Acrescentar um aviso novo = uma linha aqui + a frase em pt-BR no JS, no mesmo commit.
"""

from __future__ import annotations

#: código → onde nasce (só para quem for ler; o teste não depende do texto).
WARNING_CODES: dict[str, str] = {
    # configuração
    "key_not_importable": "config: chave fora da allowlist (estado ou segurança) — ignorada",
    "secret_missing": "config: o pacote não trouxe o segredo (sem senha ou sem a chave)",
    "secret_kept": "config: já existe um segredo no destino e ele foi mantido",
    "invalid_value": "config: valor que não passa na validação — não gravado",
    "env_override_active": "config: uma variável de ambiente deste servidor vai mascarar o valor importado",
    # linhas em geral
    "invalid_row": "linha malformada (tag, contato ou mensagem) — ignorada",
    "row_failed": "exceção ao processar a linha — contada como erro",
    # contatos
    "duplicate_in_package": "dois contatos do pacote são o mesmo número (variante BR) — o 2º foi fundido",
    "tag_unknown": "tag do contato que não existe no destino nem no pacote — não aplicada",
    # mensagens
    "contact_unavailable": "o contato da mensagem não foi importado — mensagem ignorada",
    # motor de IA
    "agent_prompt_missing": "agente aponta para um prompt que não existe aqui nem veio no pacote",
    "agent_tool_missing": "agente lista uma ferramenta que não existe aqui nem veio no pacote",
    "tool_imported_disabled": "ferramenta com código entrou DESLIGADA (precisa ser ligada à mão)",
    "tool_replaced_disabled": "ferramenta com código foi substituída e ficou DESLIGADA",
    "tool_code_not_accepted": "o pacote traz código de ferramenta e a confirmação não foi dada — não gravado",
    "tool_has_dependencies": "a ferramenta pede pacotes pip, instalados quando for ligada",
    "override_tool_unknown": "ajuste de uma ferramenta que não está registrada aqui — pulado",
    # plugins (código, configuração e dados)
    "plugin_code_unavailable": "o pacote não traz o código do plugin (ou ele não pôde ser zipado)",
    "plugin_package_invalid": "o zip do plugin é inseguro, grande demais ou o manifest não confere — não instalado",
    "plugin_api_incompatible": "o plugin pede uma versão da API que esta instalação não tem — não instalado",
    "plugin_downgrade_skipped": "o pacote traz uma versão MENOR que a instalada — não rebaixa",
    "plugin_orphan_dir": "existe uma pasta com esse nome sem registro — não adivinhamos o que é, pulado",
    "plugin_has_dependencies": "o plugin pede pacotes pip, instalados quando ele for ativado",
    "plugin_installed_disabled": "plugin instalado DESLIGADO (precisa ser ativado à mão)",
    "plugin_replaced_disabled": "plugin atualizado e DESLIGADO (precisa ser ativado de novo)",
    "plugin_code_not_accepted": "o pacote traz código de plugin e a confirmação não foi dada — não gravado",
    "plugin_unknown": "plugin que não está instalado aqui nem veio no pacote — pulado",
    "plugin_data_pending": "dados de plugin esperam o plugin ser ativado e o servidor reiniciado",
    "plugin_table_unknown": "o plugin não tem essa tabela (nome de outra versão ou forjado) — pulada",
    "plugin_table_not_empty": "a tabela do plugin aqui já tem dados — pulada inteira",
    "plugin_row_id_conflict": "linha com id já ocupado no destino — mantida a do destino",
    "plugin_table_failed": "a tabela do plugin falhou e nada dela foi gravado",
    "plugin_table_truncated": "tabela grande demais: só as primeiras linhas entraram no pacote",
    "plugin_blob_dropped": "coluna binária de plugin não viaja — ficou vazia",
    "environment_value_dropped": "valor de configuração que aponta para a própria máquina — não viaja",
    "secret_dropped": "coluna secreta de linha sem chave primária — não pôde ser cifrada e foi omitida",
    "reference_cleared": "a linha apontava para um contato que não existe aqui — vínculo removido",
    "reference_missing": "a linha apontava para um contato que não existe aqui — linha ignorada",
    # mídia
    "media_not_included": "o pacote não traz o arquivo da mensagem — entra só com o texto",
    "media_missing": "o arquivo não existe mais no disco da origem — vai só o texto",
    "media_out_of_period": "mídia fora do período escolhido — vai só o texto",
    "media_too_large": "arquivo acima do teto por arquivo — vai só o texto",
    "media_limit_reached": "o teto de tamanho do pacote foi atingido — o resto da mídia ficou de fora",
    "media_failed": "não foi possível gravar o arquivo no destino — a mensagem entrou só com o texto",
    "media_disk_full": "disco cheio no destino — as mídias restantes não foram gravadas",
    # fotos de perfil
    "avatar_failed": "foto de perfil inválida ou sem espaço — o contato entrou sem ela",
    "avatar_too_large": "foto de perfil acima do teto — ficou de fora",
}

#: Observações da verificação (``report["notes"]``): não são avisos por registro, são sobre o pacote todo.
NOTE_CODES: dict[str, str] = {
    "alembic_head_differs": "o pacote veio de uma versão diferente do banco",
    "same_instance": "o pacote foi exportado desta mesma instalação",
}

def is_known(code: str) -> bool:
    return code in WARNING_CODES
