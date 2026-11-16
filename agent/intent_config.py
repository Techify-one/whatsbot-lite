"""Explicit assisted-mode configuration and encrypted installation-local credential."""
from typing import Any
import os
from pathlib import Path
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from config.settings import get_data_dir, LLM_API_BASE_URL
from agent.intent_catalog import DEFAULT_MODEL, MODELS

DEFAULTS = dict(provider="techify_decisions", model=DEFAULT_MODEL, base_url=LLM_API_BASE_URL, context_limit=20,
                timeout_seconds=30, max_calls_per_minute=20, daily_budget_usd=1.0,
                human_priority="transfer", ambiguity_margin=0.15)

def mode(settings: Any) -> str:
    explicit = settings.get("intent_mode", "")
    return explicit if explicit in {"disabled", "assisted", "automatic"} else ("automatic" if settings.get("auto_reply", False) else "disabled")

def read_config(settings: Any) -> dict:
    return {**DEFAULTS, **settings.get("intent_classifier", {}), "mode": mode(settings)}

def _cipher(create: bool=False) -> Fernet:
    path = Path(os.environ.get("WHATSBOT_INTENT_KEY_FILE", str(get_data_dir()/"storages"/"intent.key")))
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd,"wb") as stream: stream.write(Fernet.generate_key())
        except FileExistsError: pass
    return Fernet(path.read_bytes())

def get_api_key(settings: Any) -> str:
    if read_config(settings)["provider"] == "techify_decisions":
        return settings.get("openrouter_api_key", "")  # Legacy storage name; this is the Techify credential.
    return get_legacy_api_key(settings)

def get_legacy_api_key(settings: Any) -> str:
    value=settings.get("intent_api_key_encrypted", "")
    if not value: return ""
    try: return _cipher().decrypt(value.encode()).decode()
    except Exception: return ""

def set_api_key(settings: Any, value: Any) -> None:
    settings["intent_api_key_encrypted"] = _cipher(True).encrypt(value.encode()).decode() if value else ""

def validate_config(data: Any) -> dict:
    result={**DEFAULTS, **{k:v for k,v in data.items() if k in DEFAULTS}}
    if result["provider"] not in {"jev","openai","techify_decisions"}: raise ValueError("Provedor inválido.")
    if result["provider"] == "techify_decisions":
        result["base_url"] = LLM_API_BASE_URL
        if result["model"] not in MODELS: raise ValueError("Selecione um modelo Decisions confirmado.")
    parsed=urlparse(result["base_url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use uma URL HTTPS sem credenciais, consulta ou fragmento.")
    if not str(result["model"]).strip(): raise ValueError("Informe o modelo.")
    for name,low,high in [("context_limit",10,50),("timeout_seconds",5,120),("max_calls_per_minute",1,120)]:
        result[name]=int(result[name])
        if not low<=result[name]<=high: raise ValueError(f"{name}: use {low} a {high}.")
    result["daily_budget_usd"]=float(result["daily_budget_usd"])
    if not 0<result["daily_budget_usd"]<=100: raise ValueError("Limite diário deve ser maior que zero e até US$100.")
    result["ambiguity_margin"]=float(result["ambiguity_margin"])
    if not 0 <= result["ambiguity_margin"] <= 1: raise ValueError("Margem de ambiguidade deve estar entre 0 e 1.")
    result["human_priority"]="transfer"
    return result
