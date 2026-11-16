"""Offline administrative API and durable repository integration checks."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from db import init_db
from db.repositories import intent_repo as repo
from server.routes.intents import register_routes

@pytest.fixture
def env(tmp_path,monkeypatch):
    init_db(tmp_path/'test.db')
    monkeypatch.setenv('WHATSBOT_INTENT_KEY_FILE',str(tmp_path/'key'))
    class Settings(dict):
        def save(self): pass
    settings=Settings(auto_reply=False,web_password_hash='protected',intent_classifier={'provider':'jev','model':'jev-1.14.0','base_url':'https://api.typesafe.ai/v1'})
    handler=SimpleNamespace(api_key='executor',_tool_executors={'calendar_free':(lambda:None,'calendar')},
        _tool_default_labels={},_disabled_tools=set(),intent_tool_metadata={'calendar_free':{'effect':'read','capability':'calendar.availability'}})
    runtime=SimpleNamespace(approve=AsyncMock(),simulate=AsyncMock(return_value={'actionable':False}))
    app=FastAPI();register_routes(app,SimpleNamespace(settings=settings,agent_handler=handler,intent_runtime=runtime))
    return TestClient(app),settings,handler,runtime

def definition(**kw):
    return dict(id='meeting',name='Reunião',description='Agendar reunião',**kw)

def test_crud_scope_tools_and_disabled_default(env):
    client,_,_,_=env
    assert client.post('/api/intents',json=definition()).status_code==200
    assert repo.get_intent('meeting')['enabled'] is False
    assert client.put('/api/intents/meeting',json={'enabled':True}).status_code==400
    assert client.put('/api/intents/meeting',json={'enabled':True,'contacts':['123'],'tools':['not_allowed']}).status_code==400
    assert client.put('/api/intents/meeting',json={'enabled':True,'contacts':['123'],'tools':['calendar_free']}).status_code==200
    assert repo.get_intent('meeting')['tools']==['calendar_free']
    assert client.post('/api/intents',json=definition()).status_code==409
    assert client.delete('/api/intents/meeting').status_code==200
    assert repo.get_intent('meeting') is None

def test_settings_credential_write_only_and_activation(env):
    client,settings,_,_=env
    assert client.put('/api/intents/settings',json={'mode':'assisted'}).status_code==400
    r=client.put('/api/intents/settings',json={'api_key':'test_secret','mode':'assisted'})
    assert r.status_code==200
    assert 'test_secret' not in r.text
    assert 'test_secret' not in str(settings)
    assert settings['auto_reply'] is False
    assert client.put('/api/intents/settings',json={'clear_api_key':True,'mode':'disabled'}).status_code==200
    assert client.put('/api/intents/settings',json={'mode':'automatic'}).status_code==200
    assert settings['auto_reply'] is True

def test_task_isolation_terminal_and_edit_revocation(env):
    client,_,_,_=env
    client.post('/api/intents',json=definition())
    a=repo.create_task('123','meeting','a',{},300)
    assert repo.create_task('123','meeting','b',{},300) is None
    b=repo.create_task('456','meeting','b',{},300)
    assert b
    client.put('/api/intents/meeting',json={'description':'Changed'})
    assert repo.get_task(a['id'])['status']=='cancelled'
    assert client.post('/api/intents/tasks/'+a['id']+'/pause').status_code==409
    assert repo.active_task('123') is None

def test_dry_run_no_external_action(env):
    client,_,_,runtime=env
    assert client.post('/api/intents/test',json={'text':'Não quero reunião','phone':'123'}).status_code==200
    runtime.simulate.assert_awaited_once()
    repo.append_audit(None,'classification',{'confidence':.2,'reason':'below_threshold'})
    decisions=client.get('/api/intents/decisions')
    assert decisions.status_code==200
    assert decisions.json()['data'][0]['details']['reason']=='below_threshold'
    assert repo.list_tasks()==[]

def test_plugin_contract_registration_is_disabled(env):
    from agent.intent_schema import register_plugin_intents
    _,_,handler,_=env
    handler._tool_executors={'calendar_free':(lambda:None,'calendar')}
    manifest=SimpleNamespace(raw={'intent_tools':{'calendar_free':{'effect':'read','capability':'calendar.availability'}},
        'intents':[dict(id='calendar_meeting',name='Meeting',description='Schedule',tools=['calendar_free'],enabled=True,allow_all_contacts=True)]})
    loaded=SimpleNamespace(id='calendar',manifest=manifest,tools=[({'function':{'name':'calendar_free'}},lambda:None)])
    register_plugin_intents(SimpleNamespace(loaded={'calendar':loaded}),handler)
    assert repo.get_intent('calendar_meeting')['enabled'] is False

def test_real_runtime_simulation_route(env,monkeypatch):
    from agent.intent_runtime import IntentRuntime
    from agent.intent_classifier import Classification
    from types import SimpleNamespace
    from server.routes.intents import register_routes
    client,settings,handler,_=env
    repo.save_intent(definition(enabled=False,allow_all_contacts=True))
    classifier=SimpleNamespace(classify=AsyncMock(return_value=Classification(intent_id='meeting',confidence=.98,actionable=True)))
    monkeypatch.setattr('agent.intent_runtime.get_api_key',lambda _: 'test')
    sender=AsyncMock()
    runtime=IntentRuntime(settings,handler,sender,classifier=classifier)
    app=FastAPI();register_routes(app,SimpleNamespace(settings=settings,agent_handler=handler,intent_runtime=runtime))
    response=TestClient(app).post('/api/intents/test',json={'text':'Reunião amanhã?','phone':'123'})
    assert response.status_code==200,response.text
    assert response.json()['data']['simulation'] is True
    assert response.json()['data']['would_execute'] is False
    sender.assert_not_called()
    assert repo.list_tasks()==[]

def test_uncertain_operation_requires_reconciliation_before_slot_release(env):
    client,_,_,_=env
    client.post('/api/intents',json=definition())
    task=repo.create_task('123','meeting','e',{'outcome_unknown':True},300)
    repo.transition(task['id'],['detected'],'paused')
    assert client.post('/api/intents/tasks/'+task['id']+'/approve').status_code==409
    assert client.post('/api/intents/tasks/'+task['id']+'/transfer',json={}).status_code==409
    client.put('/api/intents/meeting',json={'description':'new'})
    client.put('/api/intents/settings',json={'mode':'disabled'})
    assert repo.active_task('123') is not None
    assert client.post('/api/intents/tasks/'+task['id']+'/transfer',json={'reconciled':True}).status_code==200
    assert repo.active_task('123') is None

def test_restart_and_delete_do_not_release_uncertain_operation(env):
    client,_,_,_=env
    client.post('/api/intents',json=definition())
    task=repo.create_task('123','meeting','e',{'outcome_unknown':True},-1)
    repo.transition(task['id'],['detected'],'executing')
    repo.recover_stale()
    assert repo.active_task('123')['status']=='paused'
    client.delete('/api/intents/meeting')
    assert repo.active_task('123')['payload']['outcome_unknown'] is True

def test_techify_activation_requires_existing_credential_and_verified_catalog(env,monkeypatch):
    from agent.intent_catalog import DEFAULT_MODEL
    from agent.intent_config import get_api_key
    client,settings,_,_=env
    request={'provider':'techify_decisions','model':DEFAULT_MODEL,'mode':'assisted'}
    assert client.put('/api/intents/settings',json=request).status_code==400
    settings['openrouter_api_key']='techify-secret'
    verify=AsyncMock(return_value={'id':DEFAULT_MODEL})
    monkeypatch.setattr('agent.intent_catalog.require_model',verify)
    result=client.put('/api/intents/settings',json=request)
    assert result.status_code==200,result.text
    verify.assert_awaited_once_with('techify-secret',DEFAULT_MODEL)
    assert 'techify-secret' not in result.text
    assert get_api_key(settings)=='techify-secret'
    assert client.put('/api/intents/settings',json={'clear_api_key':True}).status_code==400
    # Switching to direct TypeSafe must never reuse the Techify credential.
    assert client.put('/api/intents/settings',json={'provider':'jev','model':'jev-1.14.0'}).status_code==400
    verify.side_effect=ValueError('Modelo indisponível')
    assert client.put('/api/intents/settings',json=request).status_code==400

def test_techify_models_route_is_read_only_and_unavailable_is_explicit(env,monkeypatch):
    client,settings,_,_=env
    fetch=AsyncMock(return_value={'models':[],'available':False,'reason':'Sem credencial'})
    monkeypatch.setattr('agent.intent_catalog.fetch_catalog',fetch)
    result=client.get('/api/intents/models')
    assert result.status_code==200 and result.json()['data']['available'] is False
    fetch.assert_awaited_once_with('')
