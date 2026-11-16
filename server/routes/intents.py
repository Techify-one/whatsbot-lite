"""Administrative intentions API. Uses the same panel authentication/instance boundary."""
from typing import Any
import asyncio
from fastapi import Request
from agent.intent_config import read_config, mode, get_api_key, set_api_key, validate_config
from agent.intent_schema import validate_definition
from db.repositories import intent_repo
from server.helpers import _ok, _err

def register_routes(app: Any,deps: Any) -> None:
    settings=deps.settings
    handler=deps.agent_handler

    def tools() -> Any:
        return [{"name":name,"label":handler._tool_default_labels.get(name,name),"plugin_id":pid,
                 "available":name not in handler._disabled_tools,
                 **getattr(handler,"intent_tool_metadata",{}).get(name,{"effect":"write","capability":""})}
                for name,(_,pid) in handler._tool_executors.items() if pid]

    def available() -> Any: return {t["name"] for t in tools() if t["available"]}

    @app.get('/api/intents/settings')
    async def get_settings() -> Any:
        return _ok({**read_config(settings),"key_configured":bool(get_api_key(settings)),
                    "executor_configured":bool(handler.api_key),"panel_protected":bool(settings.get("web_password_hash",""))})

    @app.put('/api/intents/settings')
    async def put_settings(body:dict) -> Any:
        try:
            current=read_config(settings)
            data=validate_config({**current,**body})
            selected=body.get('mode',mode(settings))
            if selected not in {'disabled','assisted','automatic'}: raise ValueError('Modo inválido.')
            key=body.get('api_key') or get_api_key(settings)
            if body.get('clear_api_key'): key=''
            if selected=='assisted':
                if not key: raise ValueError('Configure a chave do classificador antes de ativar.')
                if not handler.api_key: raise ValueError('Configure o provedor de IA do executor nas configurações.')
                if not settings.get('web_password_hash',''): raise ValueError('Defina uma senha de painel antes de ativar ações assistidas.')
            if body.get('api_key') or body.get('clear_api_key'): set_api_key(settings,key)
            settings['intent_classifier']=data
            settings['intent_mode']=selected
            settings['auto_reply']=selected=='automatic'
            settings.save()
            if selected!='assisted':
                for task in intent_repo.list_tasks(10000):
                    if task['active_slot']: intent_repo.transition(task['id'],[task['status']],'paused' if task['payload'].get('outcome_unknown') else 'cancelled')
            return await get_settings()
        except (ValueError,TypeError) as exc: return _err(str(exc))

    @app.get('/api/intents/tools')
    async def get_tools() -> Any: return _ok(tools())

    @app.get('/api/intents')
    async def get_intents() -> Any: return _ok(intent_repo.list_intents())

    @app.post('/api/intents')
    async def create(body:dict) -> Any:
        try:
            data=validate_definition(body,available())
            data['plugin_id']=None
            if intent_repo.get_intent(data['id']): return _err('Identificador já cadastrado.',409)
            return _ok(intent_repo.save_intent(data))
        except (ValueError,TypeError) as exc: return _err(str(exc))

    @app.put('/api/intents/{id}')
    async def edit(id:str,body:dict) -> Any:
        old=intent_repo.get_intent(id)
        if not old: return _err('Intenção não encontrada.',404)
        try:
            data=validate_definition({**old,**body,'id':id,'plugin_id':old.get('plugin_id')},available())
            # Editing an authorization invalidates active tasks, not just their next classification.
            for task in intent_repo.list_tasks(10000):
                if task['intent_id']==id and task['active_slot']:
                    intent_repo.transition(task['id'],[task['status']],'paused' if task['payload'].get('outcome_unknown') else 'cancelled')
            return _ok(intent_repo.save_intent(data))
        except (ValueError,TypeError) as exc: return _err(str(exc))

    @app.delete('/api/intents/{id}')
    async def delete(id:str) -> Any:
        intent_repo.delete_intent(id)
        return _ok()

    @app.get('/api/intents/decisions')
    async def decisions() -> Any: return _ok(intent_repo.list_audit(None))

    @app.get('/api/intents/tasks')
    async def tasks() -> Any:
        rows=intent_repo.list_tasks()
        for row in rows:
            row['audit']=intent_repo.list_audit(row['id'])
        return _ok(rows)

    @app.post('/api/intents/tasks/{id}/{action}')
    async def control(id:str,action:str,body:dict = {}) -> Any:
        task=intent_repo.get_task(id)
        if not task: return _err('Tarefa não encontrada.',404)
        if task['status'] in intent_repo.TERMINAL: return _err('Tarefa já encerrada.',409)
        if task['payload'].get('outcome_unknown'):
            if action not in {'cancel','transfer'} or body.get('reconciled') is not True:
                return _err('Resultado externo incerto. Verifique a integração e confirme reconciliação antes de liberar a conversa.',409)
        if action=='approve':
            if mode(settings)!='assisted': return _err('Modo assistido desativado.')
            if not await deps.intent_runtime.approve(id): return _err('Tarefa não pode ser retomada neste estado.',409)
        else:
            target={'pause':'paused','cancel':'cancelled','transfer':'transferred'}.get(action)
            if not target: return _err('Ação inválida.')
            if not intent_repo.transition(id,[task['status']],target): return _err('Tarefa já alterada.',409)
            intent_repo.append_audit(id,'owner_control',{'action':action})
        return _ok(intent_repo.get_task(id))

    @app.post('/api/intents/test')
    async def test(body:dict) -> Any:
        text=str(body.get('text',''))[:12000]
        if not text.strip(): return _err('Informe uma mensagem de teste.')
        try:
            return _ok(await deps.intent_runtime.simulate(str(body.get('phone','')),text))
        except Exception:
            return _err('Não foi possível classificar. Confira configuração e diagnósticos; nenhuma ação foi executada.',502)
