"""Real webhook send closure with offline GOWA, including interleaved takeover."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from fastapi import FastAPI
from db import init_db
from db.repositories import intent_repo
from server.state import AppState
from server.routes import webhook
from agent.intent_schema import IntentDefinition
from gowa.client import GOWASendError

@pytest.fixture
def setup(tmp_path,monkeypatch):
    init_db(tmp_path/'db.sqlite')
    class Settings(dict):
        data_dir=tmp_path
    settings=Settings(intent_mode='assisted',response_delay_min=0,response_delay_max=0,split_messages=False)
    gowa=MagicMock();gowa.send_message.return_value={'results':{'message_id':'sent'}}
    handler=MagicMock();handler._get_contact.return_value.can_send=True
    deps=SimpleNamespace(settings=settings,gowa_client=gowa,agent_handler=handler,ws_manager=SimpleNamespace(broadcast=AsyncMock()),state=AppState(),statics_media_dir=tmp_path)
    webhook.register_routes(FastAPI(),deps)
    intent=IntentDefinition(id='test',name='Test',description='test',enabled=True,allow_all_contacts=True,policy='automatic').model_dump()
    intent_repo.save_intent(intent)
    task=intent_repo.create_task('123','test','event',{'intent':intent,'context':[]},300)
    intent_repo.transition(task['id'],['detected'],'executing')
    monkeypatch.setattr(webhook,'atrack_step',AsyncMock())
    monkeypatch.setattr(webhook,'emit_with_filter',AsyncMock())
    return deps,task

def test_manual_reply_during_async_filter_prevents_send(setup,monkeypatch):
    deps,task=setup
    async def run():
        reached=asyncio.Event();release=asyncio.Event()
        async def filter_(name,value,context):
            if name=='filter.reply.part':
                reached.set();await release.wait()
            return value
        monkeypatch.setattr(webhook,'apply_filter',filter_)
        pending=asyncio.create_task(deps.intent_runtime.send_reply('123','answer'))
        await reached.wait()
        deps.intent_runtime.owner_intervened('123');release.set()
        with pytest.raises(RuntimeError): await pending
    asyncio.run(run())
    deps.gowa_client.send_message.assert_not_called()
    assert intent_repo.get_task(task['id'])['status']=='transferred'

def test_send_failure_not_marked_delivered(setup,monkeypatch):
    deps,task=setup
    async def filter_(name,value,context):return value
    monkeypatch.setattr(webhook,'apply_filter',filter_)
    deps.gowa_client.send_message.side_effect=GOWASendError('offline')
    async def run():
        with pytest.raises(RuntimeError): await deps.intent_runtime._send(task,'answer','one')
    asyncio.run(run())
    assert intent_repo.get_operation(task['id'],'send:one')['status']=='pending'
