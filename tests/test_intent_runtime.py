import asyncio
import copy
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from agent.intent_runtime import IntentRuntime
from agent.intent_classifier import Classification, bounded_context

class Repo:
    def __init__(self):
        self.intents=[dict(id='meeting',name='Meeting',enabled=True,allow_all_contacts=True,tools=[],policy='automatic',min_confidence=.9)]
        self.tasks={}; self.epochs={}; self.events=set(); self.ops={}; self.audit=[]
    def reserve_classification(self,*args): return True
    def owner_epoch(self,phone,advance=False):
        self.epochs[phone]=self.epochs.get(phone,0)+int(advance)
        return self.epochs[phone]
    def list_intents(self): return self.intents
    def get_intent(self,id): return next((i for i in self.intents if i['id']==id),None)
    def get_task(self,id): return copy.deepcopy(self.tasks.get(id))
    def active_task(self,phone): return next((self.get_task(i) for i,t in self.tasks.items() if t['phone']==phone and t['status'] in ['detected','waiting_contact','waiting_owner','paused','executing']),None)
    def create_task(self,phone,intent_id,event_key,snapshot,timeout_seconds,expected_owner_epoch=None):
        if expected_owner_epoch is not None and expected_owner_epoch!=self.owner_epoch(phone): return None
        if self.active_task(phone): return None
        id=str(len(self.tasks)+1); self.tasks[id]=dict(id=id,phone=phone,intent_id=intent_id,payload=copy.deepcopy(snapshot),status='detected',expires_at=time.time()+timeout_seconds)
        return self.get_task(id)
    def transition(self,id,states,status,**fields):
        if self.tasks[id]['status'] not in states: return False
        self.tasks[id].update(status=status,**copy.deepcopy(fields)); return True
    def claim_event(self,key):
        if key in self.events: return False
        self.events.add(key); return True
    def claim_operation(self,id,key):
        if (id,key) in self.ops: return False
        self.ops[id,key]={'status':'pending'}; return True
    def get_operation(self,id,key): return self.ops.get((id,key))
    def finish_operation(self,id,key,result): self.ops[id,key]={'status':'completed','result':result}
    def append_audit(self,*args): self.audit.append(args)

class Classifier:
    calls=0
    async def classify(self,*args):
        self.calls+=1
        return Classification(intent_id='meeting',confidence=.99,actionable=True)

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=Repo(); self.sent=[]; self.classifier=Classifier()
        self.handler=SimpleNamespace(api_key='test',_tool_executors={},_tool_originals={'create':{'function':{'parameters':{'type':'object','properties':{'hour':{'type':'integer'}},'required':['hour'],'additionalProperties':False}}}},_disabled_tools=set(),intent_tool_metadata={})
        self.settings={'intent_mode':'assisted'}
        self.patch=patch('agent.intent_runtime.get_api_key',return_value='test');self.patch.start();self.addCleanup(self.patch.stop)
        async def send(phone,text): self.sent.append((phone,text))
        async def plan(task,obs): return {'reply':'feito','status':'completed'}
        self.runtime=IntentRuntime(self.settings,self.handler,send,repo=self.repo,classifier=self.classifier,planner=plan)
    async def test_modes_silent(self):
        for mode in ['disabled','automatic']:
            self.settings['intent_mode']=mode;await self.runtime.handle('1','hello',mode)
        self.assertEqual(self.classifier.calls,0);self.assertEqual(self.sent,[])
    async def test_complete_and_new_task_duplicate(self):
        await self.runtime.handle('1','meeting','a');await self.runtime.handle('1','meeting','a');await self.runtime.handle('1','another','b')
        self.assertEqual(len(self.repo.tasks),2);self.assertEqual(len(self.sent),2)
        self.assertTrue(all(t['status']=='completed' for t in self.repo.tasks.values()))
    async def test_disabled_and_scope(self):
        self.repo.intents[0]['enabled']=False;await self.runtime.handle('1','meeting','a')
        self.repo.intents[0].update(enabled=True,allow_all_contacts=False);await self.runtime.handle('1','meeting','b')
        self.assertEqual(self.repo.tasks,{})
    async def test_followup_not_reclassified(self):
        async def plan(task,obs): return {'reply':'Qual horário?','status':'waiting_contact'}
        self.runtime.planner=plan
        await self.runtime.handle('1','meeting','a');await self.runtime.handle('1','15h','b')
        self.assertEqual(self.classifier.calls,1);self.assertEqual(len(self.repo.tasks),1)
        self.assertIn('15h',str(self.repo.tasks['1']['payload']['context']))
    async def test_owner_takeover_no_conflict(self):
        async def plan(task,obs):
            self.runtime.owner_intervened('1')
            return {'reply':'wrong','status':'completed'}
        self.runtime.planner=plan;await self.runtime.handle('1','meeting','a')
        self.assertEqual(self.sent,[]);self.assertEqual(self.repo.tasks['1']['status'],'transferred')
    async def test_unauthorized_tool_fails(self):
        async def plan(task,obs): return {'tool':'dangerous','args':{}}
        self.runtime.planner=plan;await self.runtime.handle('1','meeting','a')
        self.assertEqual(self.repo.tasks['1']['status'],'failed');self.assertEqual(self.sent,[])
    async def test_missing_key(self):
        with patch('agent.intent_runtime.get_api_key',return_value=''):
            await self.runtime.handle('1','meeting','a')
        self.assertFalse(self.repo.tasks)
    async def test_unverified_tool_cannot_complete(self):
        self.repo.intents[0]['tools']=['calendar_create']
        await self.runtime.handle('1','meeting','a')
        self.assertEqual(self.repo.tasks['1']['status'],'transferred')
    async def test_write_requires_exact_owner_approval(self):
        self.repo.intents[0]['tools']=['create'];calls=[]
        self.handler._tool_executors={'create':(lambda ctx,args: calls.append(args) or {'ok':True},None)}
        self.handler._get_contact=lambda phone: phone;self.handler._make_tool_ctx=lambda contact,plugin: None
        async def plan(task,obs): return {'reply':'Criado','status':'completed'} if obs else {'tool':'create','args':{'hour':15}}
        self.runtime.planner=plan
        await self.runtime.handle('1','meeting','a')
        self.assertEqual(calls,[]);self.assertEqual(self.repo.tasks['1']['status'],'waiting_owner')
        await self.runtime.approve('1');self.assertEqual(calls,[{'hour':15}]);self.assertEqual(self.repo.tasks['1']['status'],'completed')
    async def test_missing_calendar_never_claims_success(self):
        self.repo.intents[0]['required_capabilities']=['calendar.create']
        await self.runtime.handle('1','meeting','a')
        self.assertEqual(self.repo.tasks['1']['status'],'transferred')
        self.assertIn('não está configurada',self.sent[0][1])
    async def test_parallel_conversations_isolated(self):
        await asyncio.gather(self.runtime.handle('1','meeting','a'),self.runtime.handle('2','meeting','b'))
        self.assertEqual({t['phone'] for t in self.repo.tasks.values()},{'1','2'})
    async def test_classification_failure_silent_circuit(self):
        class Broken:
            async def classify(self,*args): raise TimeoutError()
        self.runtime.classifier=Broken()
        for n in range(4): await self.runtime.handle('1','meeting',str(n))
        self.assertFalse(self.repo.tasks);self.assertFalse(self.sent)
        self.assertGreater(self.runtime._open_until,time.time())
    async def test_tool_revoked_after_approval_request(self):
        self.repo.intents[0].update(tools=['create'],policy='owner_approval')
        await self.runtime.handle('1','meeting','a')
        self.repo.intents[0]['tools']=[]
        self.assertFalse(await self.runtime.approve('1'))
        self.assertEqual(self.repo.tasks['1']['status'],'transferred')
    async def test_expired_task_returns_monitoring(self):
        self.repo.intents[0]['policy']='owner_approval'
        await self.runtime.handle('1','meeting','a')
        self.repo.tasks['1']['expires_at']=time.time()-1
        await self.runtime.handle('1','15h','b')
        self.assertEqual(self.repo.tasks['1']['status'],'expired')
        await self.runtime.handle('1','meeting again','c')
        self.assertEqual(len(self.repo.tasks),2)
    def test_redacts_result_secrets(self):
        from agent.intent_runtime import sanitize_result
        value=sanitize_result({'ok':True,'access_token':'secret','nested':{'password':'secret','id':'event'}})
        self.assertNotIn('secret',str(value));self.assertEqual(value['nested']['id'],'event')
    async def test_optional_write_alternative_not_required(self):
        self.repo.intents[0].update(tools=['create','cancel'],required_capabilities=['calendar.create'])
        self.handler.intent_tool_metadata={'create':{'effect':'write','capability':'calendar.create'},'cancel':{'effect':'write','capability':'calendar.cancel'}}
        self.handler._tool_executors={'create':(lambda ctx,args: {'ok':True},None)}
        self.handler._get_contact=lambda phone: phone;self.handler._make_tool_ctx=lambda contact,plugin: None
        async def plan(task,obs): return {'reply':'Criado','status':'completed'} if obs else {'tool':'create','args':{'hour':15}}
        self.runtime.planner=plan
        await self.runtime.handle('1','meeting','a');await self.runtime.approve('1')
        self.assertEqual(self.repo.tasks['1']['status'],'completed')
    async def test_invalid_schema_args_cannot_execute(self):
        self.repo.intents[0]['tools']=['create'];calls=[]
        self.handler._tool_executors={'create':(lambda *args: calls.append(1),None)}
        async def plan(task,obs): return {'tool':'create','args':{'hour':{'inject':True}}}
        self.runtime.planner=plan;await self.runtime.handle('1','meeting','a')
        self.assertEqual(self.repo.tasks['1']['status'],'failed');self.assertEqual(calls,[])
    async def test_new_input_invalidates_pending_operation(self):
        self.repo.intents[0]['tools']=['create'];calls=[]
        self.handler._tool_executors={'create':(lambda ctx,args: calls.append(args) or {'ok':True},None)}
        self.handler._get_contact=lambda phone: phone;self.handler._make_tool_ctx=lambda contact,plugin: None
        async def plan(task,obs): return {'tool':'create','args':{'hour':16 if '16h' in str(task['payload']['context']) else 15}}
        self.runtime.planner=plan;await self.runtime.handle('1','meeting','a')
        await self.runtime.handle('1','melhor 16h','b')
        self.assertNotIn('pending_operation',self.repo.tasks['1']['payload'])
        await self.runtime.approve('1')
        self.assertEqual(calls,[])
        self.assertEqual(self.repo.tasks['1']['payload']['pending_operation']['args'],{'hour':16})
    async def test_contact_cancels_pending(self):
        self.repo.intents[0]['policy']='owner_approval';await self.runtime.handle('1','meeting','a')
        await self.runtime.handle('1','cancelar','b');self.assertEqual(self.repo.tasks['1']['status'],'cancelled')
    async def test_ambiguous_requires_owner(self):
        self.repo.intents[0]['ambiguous_policy']='owner_approval'
        class Ambiguous:
            async def classify(self,*args): return Classification(candidate_id='meeting',confidence=.6,actionable=True,ambiguous=True)
        self.runtime.classifier=Ambiguous();await self.runtime.handle('1','meeting?','a')
        self.assertEqual(self.repo.tasks['1']['status'],'waiting_owner');self.assertFalse(self.sent)
    async def test_owner_during_classification_blocks_stale_task(self):
        entered=asyncio.Event();release=asyncio.Event()
        class Delayed:
            async def classify(inner,*args):
                entered.set();await release.wait()
                return Classification(intent_id='meeting',confidence=.99,actionable=True)
        self.runtime.classifier=Delayed()
        worker=asyncio.create_task(self.runtime.handle('1','meeting','a'))
        await entered.wait();self.runtime.owner_intervened('1');release.set();await worker
        self.assertFalse(self.repo.tasks);self.assertFalse(self.sent)
    async def test_simulation_no_side_effects_disabled_preview(self):
        self.repo.intents[0]['enabled']=False
        result=await self.runtime.simulate('1','meeting')
        self.assertEqual(result['intent_id'],'meeting');self.assertFalse(result['would_execute'])
        self.assertFalse(self.repo.tasks);self.assertFalse(self.sent);self.assertFalse(self.repo.ops)
    async def test_timed_out_tool_retains_slot_blocks_new_tasks_and_resume(self):
        self.repo.intents[0]['tools']=['create']
        calls=[]
        def slow(ctx,args):
            time.sleep(.15);calls.append(args);return {'ok':True}
        self.handler._tool_executors={'create':(slow,None)}
        self.handler._get_contact=lambda phone: phone;self.handler._make_tool_ctx=lambda contact,plugin: None
        async def plan(task,obs): return {'tool':'create','args':{'hour':15}}
        self.runtime.planner=plan
        await self.runtime.handle('1','meeting','a')
        with patch('agent.intent_runtime.read_config',return_value={'timeout_seconds':.03}):
            await self.runtime.approve('1')
        self.assertEqual(self.repo.tasks['1']['status'],'paused')
        self.assertTrue(self.repo.tasks['1']['payload']['outcome_unknown'])
        self.repo.tasks['1']['expires_at']=time.time()-1
        await self.runtime.handle('1','meeting again','b')
        self.assertEqual(len(self.repo.tasks),1)
        self.assertFalse(await self.runtime.approve('1'))
        await asyncio.sleep(.17)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.repo.tasks['1']['status'],'paused')
    async def test_uncertain_send_retains_slot(self):
        async def broken(phone,text): raise TimeoutError()
        self.runtime.send_reply=broken
        await self.runtime.handle('1','meeting','a')
        self.assertEqual(self.repo.tasks['1']['status'],'paused')
        self.assertTrue(self.repo.tasks['1']['payload']['outcome_unknown'])
        await self.runtime.handle('1','meeting','b')
        self.assertEqual(len(self.repo.tasks),1)
        self.assertFalse(await self.runtime.approve('1'))
    def test_context_idempotent_bounded(self):
        context=bounded_context([{'role':'user','content':'hello'},{'role':'operator','content':'done'}])
        self.assertEqual(bounded_context(context),context)

class ClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_jev_official_numeric_noul(self):
        import httpx
        from agent.intent_classifier import ProviderClassifier
        def response(request):
            return httpx.Response(200,json={'answers':{'intent':{'choice':'meeting','confidence':.98,'probabilities':{'meeting':.99}},'actionable':{'noul':.99}},'usage':{'input_tokens':200}})
        classifier=ProviderClassifier({'provider':'jev'},'test',transport=httpx.MockTransport(response))
        result=await classifier.classify([{'role':'user','content':'vamos marcar'}],[{'id':'meeting','min_confidence':.9}])
        self.assertEqual(result.intent_id,'meeting')
    async def test_jev_abstains_not_actionable(self):
        import httpx
        from agent.intent_classifier import ProviderClassifier
        def response(request):
            return httpx.Response(200,json={'answers':{'intent':{'choice':'meeting','confidence':1,'probabilities':{'meeting':1}},'actionable':{'noul':.1}}})
        classifier=ProviderClassifier({'provider':'jev'},'test',transport=httpx.MockTransport(response))
        result=await classifier.classify([{'role':'user','content':'obrigado'}],[{'id':'meeting'}])
        self.assertIsNone(result.intent_id)
    async def test_no_fallback_on_error(self):
        import httpx
        from agent.intent_classifier import ProviderClassifier
        calls=[]
        def response(request): calls.append(str(request.url));return httpx.Response(401)
        classifier=ProviderClassifier({'provider':'jev'},'test',transport=httpx.MockTransport(response))
        with self.assertRaises(RuntimeError): await classifier.classify([],[{'id':'meeting'}])
        self.assertEqual(calls,['https://api.typesafe.ai/v1/systemone'])
