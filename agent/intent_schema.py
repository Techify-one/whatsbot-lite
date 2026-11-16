"""Version 1 intention registration contract; administrative enablement is separate."""
from typing import Any
import re
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

class IntentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    name: str = Field(min_length=1,max_length=120)
    description: str = Field(default="",max_length=3000)
    positive_examples: list[str] = Field(default_factory=list,max_length=30)
    negative_examples: list[str] = Field(default_factory=list,max_length=30)
    enabled: bool = False
    priority: int = Field(default=0,ge=-100,le=100)
    min_confidence: float = Field(default=.9,ge=.5,le=1)
    contacts: list[str] = Field(default_factory=list,max_length=1000)
    allow_all_contacts: bool = False
    allow_groups: bool = False
    tools: list[str] = Field(default_factory=list,max_length=30)
    tool_parameters: dict = Field(default_factory=dict)
    instructions: str = Field(default="",max_length=12000)
    required_capabilities: list[str] = Field(default_factory=list,max_length=30)
    required_data: list[str] = Field(default_factory=list,max_length=30)
    success_criteria: str = Field(default="",max_length=3000)
    cancel_criteria: str = Field(default="",max_length=3000)
    timeout_seconds: int = Field(default=1800,ge=60,le=86400)
    policy: Literal["automatic","contact_confirmation","owner_approval","deny"] = "owner_approval"
    ambiguous_policy: Literal["silent","owner_approval"] = "silent"
    agent_id: str | None = None
    plugin_id: str | None = None
    contract_version: int = Field(default=1,ge=1,le=1)

def validate_definition(data: Any, available_tools: Any, *, plugin_id: Any=None) -> dict:
    definition=IntentDefinition.model_validate(data).model_dump()
    if definition["agent_id"] not in (None, "", "default"):
        raise ValueError("Executor não suportado. Use o agente padrão (default).")
    if any(len(x)>2000 for x in definition["positive_examples"]+definition["negative_examples"]):
        raise ValueError("Exemplos devem ter até 2.000 caracteres.")
    if set(definition["tool_parameters"])-set(definition["tools"]):
        raise ValueError("Parâmetros só podem ser definidos para ferramentas selecionadas.")
    if definition["enabled"]:
        missing=set(definition["tools"])-set(available_tools)
        if missing: raise ValueError("Ferramentas indisponíveis: "+", ".join(sorted(missing)))
        if not definition["contacts"] and not definition["allow_all_contacts"]:
            raise ValueError("Escolha contatos ou autorize explicitamente todos os contatos.")
    if plugin_id:
        if not definition["id"].startswith(plugin_id+"_"):
            raise ValueError("ID de intenção do plugin deve começar com seu prefixo.")
        definition["plugin_id"]=plugin_id
        definition["enabled"]=False
    return definition

def register_plugin_intents(registry: Any, handler: Any) -> None:
    from db.repositories import intent_repo
    handler.intent_tool_metadata={}
    for plugin in registry.loaded.values():
        meta=plugin.manifest.raw.get("intent_tools", {})
        names={schema["function"]["name"] for schema,_ in plugin.tools}
        if isinstance(meta,dict):
            for name,value in meta.items():
                if name in names and isinstance(value,dict):
                    effect=value.get("effect","write")
                    handler.intent_tool_metadata[name]={"effect": effect if effect in {"read","write"} else "write", "capability":str(value.get("capability","")),"idempotent":value.get("idempotent") is True}
        declarations=plugin.manifest.raw.get("intents", [])
        if not isinstance(declarations,list): continue
        for declaration in declarations:
            try:
                definition=validate_definition(declaration,names,plugin_id=plugin.id)
                if set(definition["tools"])-names: continue
                if not intent_repo.get_intent(definition["id"]): intent_repo.save_intent(definition)
            except (ValueError,TypeError):
                import logging
                logging.getLogger(__name__).warning("Invalid intention declaration in plugin %s",plugin.id)


def ensure_builtin_intents() -> None:
    from db.repositories import intent_repo
    if not intent_repo.get_intent("agendar_reuniao"):
        intent_repo.save_intent(IntentDefinition(
            id="agendar_reuniao",name="Agendar reunião",enabled=False,
            description="Pedido atual de agendamento de reunião com o proprietário.",
            positive_examples=["Francisco, conseguimos marcar uma reunião amanhã?"],
            negative_examples=["Obrigado pela reunião de ontem", "Não quero marcar reunião"],
            required_capabilities=["calendar.availability","calendar.create"],
            required_data=["data", "horário", "fuso", "participantes"],
            success_criteria="Evento criado com sucesso real pela integração; informar link se fornecido.",
            instructions="Consulte disponibilidade real, apresente opções, confirme data, fuso e participantes antes de criar. Não invente disponibilidade. Sem integração, encaminhe ao proprietário.",
            policy="owner_approval").model_dump())
