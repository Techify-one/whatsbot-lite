"""Atomic, installation-local intent persistence (SQLite/PostgreSQL)."""
from typing import Any
import json
import time
import uuid
from sqlalchemy import select, insert, update, delete
from sqlalchemy.exc import IntegrityError
from db.engine import get_engine
from db.tables import intents, intent_tasks, intent_events, intent_operations, intent_audit
from db.upsert import upsert

TERMINAL = {"completed", "cancelled", "expired", "transferred", "failed"}

def _task(row: Any) -> Any:
    if row is None:
        return None
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result

def list_intents() -> list[dict]:
    with get_engine().connect() as conn:
        return [json.loads(r.definition) for r in conn.execute(select(intents))]

def get_intent(id: str) -> dict | None:
    with get_engine().connect() as conn:
        raw = conn.execute(select(intents.c.definition).where(intents.c.id == id)).scalar_one_or_none()
        return json.loads(raw) if raw else None

def save_intent(definition: dict) -> dict:
    values = {"id": definition["id"], "definition": json.dumps(definition), "updated_at": time.time()}
    with get_engine().begin() as conn:
        conn.execute(upsert(intents, values, conflict_cols=["id"], update_cols=["definition", "updated_at"]))
    return definition

def delete_intent(id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(delete(intents).where(intents.c.id == id))
        rows=conn.execute(select(intent_tasks).where(intent_tasks.c.intent_id==id,intent_tasks.c.active_slot.is_not(None))).mappings().all()
        for row in rows:
            if not json.loads(row["payload"]).get("outcome_unknown"):
                conn.execute(update(intent_tasks).where(intent_tasks.c.id==row["id"],intent_tasks.c.payload==row["payload"]).values(status="cancelled",active_slot=None,updated_at=time.time()))

def active_task(phone: str) -> dict | None:
    with get_engine().connect() as conn:
        return _task(conn.execute(select(intent_tasks).where(intent_tasks.c.active_slot == phone)).mappings().first())

def get_task(id: str) -> dict | None:
    with get_engine().connect() as conn:
        return _task(conn.execute(select(intent_tasks).where(intent_tasks.c.id == id)).mappings().first())

def create_task(phone: str, intent_id: Any, event_key: str, snapshot: Any, timeout_seconds: int, expected_owner_epoch: Any=None) -> dict | None:
    now = time.time()
    row = dict(id=str(uuid.uuid4()), phone=phone, active_slot=phone, intent_id=intent_id,
               status="detected", payload=json.dumps(snapshot), created_at=now, updated_at=now,
               expires_at=now + timeout_seconds)
    try:
        with get_engine().begin() as conn:
            if expected_owner_epoch is not None:
                from db.tables import intent_limits
                checked=conn.execute(update(intent_limits).where(intent_limits.c.key=="owner:"+phone,
                    intent_limits.c.used==expected_owner_epoch).values(used=intent_limits.c.used))
                if not checked.rowcount: return None
            conn.execute(insert(intent_tasks).values(**row))
    except IntegrityError:
        return None
    return _task(row)

def transition(id: str, expected_states: Any, status: Any, **fields: Any) -> bool:
    allowed = {k: v for k, v in fields.items() if k in {"payload", "expires_at"}}
    if "payload" in allowed:
        allowed["payload"] = json.dumps(allowed["payload"])
    allowed.update(status=status, updated_at=time.time())
    if status in TERMINAL:
        allowed["active_slot"] = None
    with get_engine().begin() as conn:
        result = conn.execute(update(intent_tasks).where(intent_tasks.c.id == id, intent_tasks.c.status.in_(expected_states)).values(**allowed))
        return bool(result.rowcount)

def claim_event(key: str) -> bool:
    try:
        with get_engine().begin() as conn:
            conn.execute(insert(intent_events).values(key=key, created_at=time.time()))
        return True
    except IntegrityError:
        return False

def append_audit(task_id: str, event: str, details: dict) -> None:
    # Callers supply decisions/metrics, never prompts, API keys or raw tool arguments.
    with get_engine().begin() as conn:
        conn.execute(insert(intent_audit).values(task_id=task_id or "", event=event,
                     details=json.dumps(details, ensure_ascii=False), created_at=time.time()))

def list_tasks(limit: int=100) -> list[dict]:
    with get_engine().connect() as conn:
        return [_task(r) for r in conn.execute(select(intent_tasks).order_by(intent_tasks.c.created_at.desc()).limit(limit)).mappings()]

def list_audit(task_id: str, limit: int=200) -> list[dict]:
    with get_engine().connect() as conn:
        return [dict(r, details=json.loads(r["details"])) for r in conn.execute(select(intent_audit).where(intent_audit.c.task_id == (task_id or "")).order_by(intent_audit.c.id.desc()).limit(limit)).mappings()]

def claim_operation(task_id: str, key: str) -> bool:
    try:
        with get_engine().begin() as conn:
            conn.execute(insert(intent_operations).values(task_id=task_id,key=key,status="pending",created_at=time.time()))
        return True
    except IntegrityError:
        return False

def finish_operation(task_id: str, key: str, result: Any) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(intent_operations).where(intent_operations.c.task_id==task_id,intent_operations.c.key==key).values(status="completed",result=json.dumps(result)))

def get_operation(task_id: str,key: str) -> dict | None:
    with get_engine().connect() as conn:
        row=conn.execute(select(intent_operations).where(intent_operations.c.task_id==task_id,intent_operations.c.key==key)).mappings().first()
        if not row: return None
        return dict(row, result=json.loads(row["result"]) if row["result"] else None)
operation_result = get_operation

def recover_stale() -> None:
    now=time.time()
    with get_engine().begin() as conn:
        rows=conn.execute(select(intent_tasks).where(intent_tasks.c.active_slot.is_not(None))).mappings().all()
        for row in rows:
            payload=json.loads(row["payload"])
            if payload.get("outcome_unknown"):
                fields={"status":"paused", "updated_at":now}
            elif row["expires_at"]<=now:
                fields={"status":"expired","active_slot":None,"updated_at":now}
            elif row["status"] in {"executing","validating","detected"}:
                fields={"status":"paused","updated_at":now}
            else: continue
            conn.execute(update(intent_tasks).where(intent_tasks.c.id==row["id"],intent_tasks.c.payload==row["payload"],intent_tasks.c.status==row["status"]).values(**fields))


def reserve_classification(max_per_minute: int, daily_budget_usd: float, reservation_usd: float=0.01) -> bool:
    """Atomic conservative estimated-cost reservation; survives restart/workers.

    Reservations are not refunded after provider errors or unknown outcomes.
    This is an application estimate, not the provider's billing hard limit.
    """
    from db.tables import intent_limits
    from sqlalchemy.exc import IntegrityError
    now=time.time()
    keys=[("minute:"+str(int(now//60)),1.0,float(max_per_minute)),
          ("day:"+time.strftime("%Y-%m-%d",time.gmtime(now)),reservation_usd,float(daily_budget_usd))]
    # Initialize counters without overwriting an existing reservation.
    for key,_,_ in keys:
        try:
            with get_engine().begin() as conn:
                conn.execute(insert(intent_limits).values(key=key,used=0))
        except IntegrityError:
            pass
    class Limited(Exception): pass
    try:
        with get_engine().begin() as conn:
            for key,amount,limit in keys:
                result=conn.execute(update(intent_limits).where(intent_limits.c.key==key,
                    intent_limits.c.used+amount<=limit+1e-10).values(used=intent_limits.c.used+amount))
                if not result.rowcount: raise Limited()
        return True
    except Limited:
        return False


def owner_epoch(phone: str, advance: bool=False) -> float:
    from db.tables import intent_limits
    key="owner:"+phone
    try:
        with get_engine().begin() as conn:
            conn.execute(insert(intent_limits).values(key=key,used=0))
    except IntegrityError: pass
    with get_engine().begin() as conn:
        if advance:
            conn.execute(update(intent_limits).where(intent_limits.c.key==key).values(used=intent_limits.c.used+1))
        return conn.execute(select(intent_limits.c.used).where(intent_limits.c.key==key)).scalar_one()
