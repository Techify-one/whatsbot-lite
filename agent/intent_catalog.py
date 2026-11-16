"""Confirmed Decisions models; account availability comes only from Techify GET catalog."""
from __future__ import annotations
import hashlib
import time
from urllib.parse import urlsplit, urlunsplit
from typing import Any
import httpx
from config.settings import LLM_API_BASE_URL

MODELS = {
    'perplexity/pplx-decider-flash-v1.2': 'Perplexity Decider Flash V1.2',
    'perplexity/pplx-decider-v1.2': 'Perplexity Decider V1.2',
    'typesafe/jev-1.14': 'TypeSafe Jev 1.14',
    'google/gemini-decision-1': 'Google Gemini Decision 1',
    'openai/gpt-6-luna-decisions': 'OpenAI GPT-6 Luna Decisions',
}
DEFAULT_MODEL = 'perplexity/pplx-decider-flash-v1.2'
_CACHE: dict[str, tuple[float, dict]] = {}

def proxy_urls(base_url: str = LLM_API_BASE_URL) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Proxy Techify deve usar HTTPS sem credenciais na URL.')
    path = parsed.path.rstrip('/')
    if not path.endswith('/v1'):
        raise ValueError('Base do proxy incompatível: esperado caminho terminado em /v1.')
    catalog = urlunsplit((parsed.scheme, parsed.netloc, path+'/models', 'output_modalities=decisions', ''))
    decisions = urlunsplit((parsed.scheme, parsed.netloc, path[:-3]+'/alpha/decisions', '', ''))
    return catalog, decisions

async def fetch_catalog(api_key: str, *, base_url: str = LLM_API_BASE_URL, transport: Any = None, force: bool = False) -> dict:
    rows = [{'id':id, 'name':name, 'available':False, 'pricing':{}} for id,name in MODELS.items()]
    if not api_key:
        return {'models':rows,'available':False,'reason':'Configure a credencial Techify nas Configurações do WhatsBot.'}
    cache_key = hashlib.sha256((base_url+'\0'+api_key).encode()).hexdigest()
    cached = _CACHE.get(cache_key)
    if not force and transport is None and cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        catalog_url, _ = proxy_urls(base_url)
        async with httpx.AsyncClient(timeout=15,follow_redirects=False,transport=transport) as client:
            response = await client.get(catalog_url,headers={'Authorization':'Bearer '+api_key})
            if response.status_code != 200: raise RuntimeError('catalog_unavailable')
            data=response.json().get('data')
            if not isinstance(data,list): raise ValueError('invalid_catalog')
        found={m.get('id'):m for m in data if isinstance(m,dict)}
        for row in rows:
            model=found.get(row['id'])
            endpoints=model.get('endpoints') if model else None
            architecture=model.get('architecture') if model else None
            modalities=architecture.get('output_modalities') if isinstance(architecture,dict) else None
            row['available']=bool(isinstance(endpoints,list) and all(isinstance(v,str) for v in endpoints)
                and isinstance(modalities,list) and all(isinstance(v,str) for v in modalities)
                and '/api/alpha/decisions' in endpoints and 'decisions' in modalities)
            pricing=model.get('pricing',{}) if model else {}
            if isinstance(pricing,dict):
                row['pricing']={k:str(pricing[k])[:100] for k in ('prompt','completion') if k in pricing}
        result={'models':rows,'available':any(r['available'] for r in rows),
                'reason':'' if any(r['available'] for r in rows) else 'Nenhum modelo confirmado está disponível no catálogo desta conta Techify.'}
        if transport is None:
            if len(_CACHE)>64: _CACHE.clear()
            _CACHE[cache_key]=(time.monotonic()+300,result)
        return result
    except Exception:
        for row in rows: row.update(available=False, pricing={})
        return {'models':rows,'available':False,'reason':'Não foi possível verificar o catálogo da conta Techify. Confira a credencial e a conexão; nenhum modelo foi substituído.'}

async def require_model(api_key: str, model: str, *, transport: Any = None) -> dict:
    if model not in MODELS: raise ValueError('Modelo Decisions não confirmado.')
    result=await fetch_catalog(api_key,transport=transport)
    selected=next((m for m in result['models'] if m['id']==model and m['available']),None)
    if not result['available'] or not selected: raise ValueError(result['reason'] or 'Modelo indisponível para esta conta Techify.')
    return selected
