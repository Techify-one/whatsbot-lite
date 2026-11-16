"""Techify Decisions contract with offline HTTP transport; no paid inference."""
import asyncio
import json
from unittest.mock import AsyncMock
import httpx
import pytest
from agent.intent_catalog import MODELS, DEFAULT_MODEL, fetch_catalog, proxy_urls
from agent.intent_classifier import ProviderClassifier
from agent.intent_config import get_api_key, set_api_key, validate_config

def catalog_row(id):
    return {'id':id,'architecture':{'output_modalities':['decisions']},'endpoints':['/api/alpha/decisions'],
            'pricing':{'prompt':'0.00000001','completion':'0'}}

@pytest.mark.parametrize('model',list(MODELS))
def test_all_confirmed_models_use_proxy_decisions_and_safe_alias(model):
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.headers['authorization']=='Bearer techify-test'
        assert request.url.host=='llm.techify.one'
        if request.method=='GET':
            assert request.url.params['output_modalities']=='decisions'
            return httpx.Response(200,json={'data':[catalog_row(model)]})
        assert request.url.path=='/api/alpha/decisions'
        payload=json.loads(request.content)
        assert payload['model']==model
        assert list(payload['questions']['intent']['criteria'])==['intent_0']
        assert 'web_search_options' not in payload
        return httpx.Response(200,json={'answers':{'intent':{'type':'choice','choice':'intent_0','probabilities':{'intent_0':.99},'confidence':.99},'actionable':{'type':'noul','noul':.99}},'usage':{'input_tokens':100,'output_tokens':0}})
    classifier=ProviderClassifier({'provider':'techify_decisions','model':model},'techify-test',transport=httpx.MockTransport(handler))
    result=asyncio.run(classifier.classify([{'role':'user','content':'pedido'}],[{'id':'cost','description':'pedido','min_confidence':.9}]))
    assert result.intent_id=='cost' and result.actionable
    assert len(seen)==2
    assert result.estimated_cost_usd==pytest.approx(.000001)

def test_catalog_absence_malformed_or_auth_failure_prevents_post():
    for response in [httpx.Response(401,json={'error':'denied'}),httpx.Response(200,json={'data':[]}),httpx.Response(200,json={'data':[{'id':DEFAULT_MODEL}]}),httpx.Response(200,json={'data':None})]:
        calls=[]
        def handler(request):
            calls.append(request.method);return response
        async def run():
            classifier=ProviderClassifier({'provider':'techify_decisions','model':DEFAULT_MODEL},'bad',transport=httpx.MockTransport(handler))
            with pytest.raises(ValueError): await classifier.classify([{'role':'user','content':'pedido'}],[{'id':'meeting'}])
        asyncio.run(run());assert calls==['GET']

def test_refusal_or_wrong_type_abstains():
    for wrong in [{'type':'refusal'}, {'type':'score','choice':'intent_0','confidence':1,'probabilities':{'intent_0':1}}]:
        def handler(request):
            if request.method=='GET':return httpx.Response(200,json={'data':[catalog_row(DEFAULT_MODEL)]})
            return httpx.Response(200,json={'answers':{'intent':wrong,'actionable':{'type':'noul','noul':1}}})
        result=asyncio.run(ProviderClassifier({'provider':'techify_decisions','model':DEFAULT_MODEL},'key',transport=httpx.MockTransport(handler)).classify([], [{'id':'meeting'}]))
        assert not result.actionable and result.intent_id is None

def test_credentials_do_not_cross_providers(tmp_path,monkeypatch):
    monkeypatch.setenv('WHATSBOT_INTENT_KEY_FILE',str(tmp_path/'key'))
    settings={'openrouter_api_key':'techify-only','intent_classifier':{'provider':'techify_decisions'}}
    set_api_key(settings,'legacy-only')
    assert get_api_key(settings)=='techify-only'
    settings['intent_classifier']['provider']='jev'
    assert get_api_key(settings)=='legacy-only'
    settings['intent_classifier']['provider']='openai'
    assert get_api_key(settings)=='legacy-only'

def test_proxy_destination_cannot_be_overridden_by_panel():
    settings=validate_config({'provider':'techify_decisions','model':DEFAULT_MODEL,'base_url':'https://other.example/v1'})
    assert settings['base_url']=='https://llm.techify.one/api/v1'
    assert proxy_urls()[1]=='https://llm.techify.one/api/alpha/decisions'
    with pytest.raises(ValueError):validate_config({'provider':'techify_decisions','model':'invented/model'})
    with pytest.raises(ValueError):proxy_urls('http://llm.techify.one/api/v1')

def test_empty_key_never_requests_catalog():
    def handler(request):raise AssertionError('Must not request')
    result=asyncio.run(fetch_catalog('',transport=httpx.MockTransport(handler)))
    assert not result['available'] and all(not m['available'] for m in result['models'])


def test_malformed_metadata_never_confirms_availability():
    malformed=[{'id':DEFAULT_MODEL,'endpoints':'not-/api/alpha/decisions-wrong','architecture':{'output_modalities':'not_decisions'}},
               {'id':DEFAULT_MODEL,'endpoints':['/api/alpha/decisions'],'architecture':['decisions']}]
    for row in malformed:
        result=asyncio.run(fetch_catalog('key',transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[row]}))))
        assert not result['available'] and not any(m['available'] for m in result['models'])
