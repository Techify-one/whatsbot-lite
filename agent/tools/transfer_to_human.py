"""Tool: transfer_to_human — transfers the conversation to a human agent."""

import logging

logger = logging.getLogger(__name__)

# Tag put on the conversation by this tool. The automatic AI re-enabling removes
# it again (server/ai_reactivation.py), so the two must agree on the name.
TRANSFER_TAG = "transferido_atendente"

TRANSFER_TO_HUMAN_TOOL = {
    "type": "function",
    "display_label": "Transferir para Humano",
    "function": {
        "name": "transfer_to_human",
        "description": (
            "Transfere o atendimento para um atendente humano. "
            "Use esta função quando: "
            "1) O cliente pedir explicitamente para falar com um humano, atendente ou pessoa real. "
            "2) O cliente fizer uma pergunta específica que você não sabe responder com certeza "
            "(ex: preços, prazos, disponibilidade, detalhes técnicos do negócio). "
            "NÃO transfira quando o cliente está apenas se apresentando, fornecendo dados pessoais "
            "(nome, email, profissão, endereço), cumprimentando ou fazendo conversa casual. "
            "Nesses casos, use save_contact_info se houver dados pessoais e responda normalmente."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Motivo da transferência (ex: 'cliente pediu atendente humano', 'dúvida fora do escopo')",
                },
            },
            "required": ["reason"],
        },
    },
}


_TRANSFER_FEEDBACK = (
    "Transferência realizada. Responda ao cliente de forma curta e natural, "
    "apenas confirmando que já vai ser atendido pela pessoa solicitada. "
    "NÃO mencione 'humano', 'atendente' nem 'transferência'."
)


def execute(ctx, args: dict) -> str | None:
    """Disable AI for the contact and tag the conversation as transferred.

    Returns a feedback string that the handler will inject as the tool reply
    when the model issues only the tool call (no inline text).
    """
    try:
        ctx.contact.set_ai_enabled(False)
        ctx.tag_registry.create(TRANSFER_TAG, "#ef4444")
        ctx.contact.add_tag(TRANSFER_TAG)
        ctx.contact.save()
        logger.info("Transfer to human for %s: %s", ctx.contact.phone, args.get("reason", ""))
    except Exception as e:
        logger.warning("transfer_to_human failed for %s: %s", ctx.contact.phone, e)
    return _TRANSFER_FEEDBACK
