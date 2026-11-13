"""Contatos (com tags e observações) e mensagens.

É a parte mais sensível do pacote: é o que os CLIENTES escreveram. No lite a conversa é implícita por
contato (``messages.contact_id``), então não há conversa, caixa nem canal para casar — a mensagem do
pacote aponta para o contato do pacote (``contact`` = id de origem) e o import mantém em memória o mapa
``id de origem → id aqui`` (contatos e mensagens sempre viajam juntos: ver ``registry.SCOPES``).

Regras:

* **Chave natural, nunca id**: contato = telefone (variantes BR, via ``contact_repo``); tag = nome;
  observação = texto; mensagem = ``msg_id`` ou (``ts``, ``role``, md5 do texto) DENTRO do contato.
  Reimportar o mesmo pacote não duplica nada, e importar sobre um destino que já tem histórico só
  acrescenta o que falta.
* **Nada dispara automação**: escrita direta em tabela, sem evento de mensagem/contato, sem
  ``new_message`` no WebSocket. Plugin nenhum reage, a IA não responde, ninguém é notificado.
* **Importar não é atividade**: o ``updated_at`` do contato não é carimbado com "agora" (a lista ordena
  por ``COALESCE(última mensagem, updated_at)``; um contato sem mensagens subiria ao topo). Os
  contadores de não lidas e a menção não lida entram ZERADOS: histórico não inunda o painel.
* O pacote é dado NÃO CONFIÁVEL: ``media_path`` NUNCA é usado como veio (ver ``_resolve_media``).
* Mensagens entram em lote (``INSERT`` múltiplo, transações curtas: no SQLite a escrita disputa lock com
  o webhook); lote que falha é regravado linha a linha e a contagem do relatório é corrigida.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from typing import Any, Iterator

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from portability import avatars as avatars_io
from portability import media as media_io
from portability import registry as reg
from portability.base import CREATE, ERROR, SKIP, UPDATE, Env, _is_text, MAX_PROMPT_BYTES

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"^[\w@.:+\-]{3,100}$")
#: ``geo:lat,lng`` é o ``media_path`` de localização: não é arquivo, é o próprio dado.
_GEO_RE = re.compile(r"^geo:-?\d{1,3}(?:\.\d{1,12})?,-?\d{1,3}(?:\.\d{1,12})?$")
#: Papéis que o lite grava (conferido no código) e que o painel sabe desenhar.
ROLES = frozenset({"user", "assistant", "system", "transcription", "system_notice",
                   "error", "private_note", "tool_call"})
_MEDIA_STATES = (None, "discarded", "removed")

MAX_FIELD = 2000
MAX_TAGS = 200
MAX_OBS = 2000
MAX_OBS_TEXT = 10_000
_CHUNK = 500
_PAGE = 2000
FLUSH_AT = 500
_CONTACT_TEXT = ("name", "email", "profession", "company", "address", "group_name")
_LOCK_RETRIES = 4

# ── validação ───────────────────────────────────────────────────────────────

def _txt(v: Any, limit: int = MAX_FIELD, *, default: str = "") -> str:
    if v is None:
        return default
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError("text")
    return v

def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None

def _flag(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int) and v in (0, 1):
        return v
    raise ValueError("flag")

def _int_id(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError("id")
    return v

def _str_items(v: Any, limit: int, item_limit: int) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > limit:
        raise ValueError("list")
    out = []
    for i in v:
        if not isinstance(i, str) or not i or len(i) > item_limit:
            raise ValueError("item")
        if i not in out:
            out.append(i)
    return out

def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()

def _msg_key(msg_id: str | None, ts: float, role: str, content: str) -> tuple:
    """Chave natural da mensagem DENTRO do contato. Com ``msg_id`` (id do WhatsApp) ele basta; sem ele
    (nota privada, aviso, resposta antiga) vale (ts, papel, md5 do texto)."""
    return ("m", msg_id) if msg_id else ("h", repr(float(ts)), role, _md5(content))

def _variants(phone: str) -> list[str]:
    from db.repositories import contact_repo
    return contact_repo._br_phone_variants(phone)

# ═══ contatos ═══════════════════════════════════════════════════════════════

def export_contacts(env: Env) -> Iterator[dict]:
    from db.engine import get_engine
    from db.tables import contact_tags, contacts, observations, tags
    last = 0
    while True:
        with get_engine().connect() as conn:
            rows = conn.execute(select(contacts).where(contacts.c.id > last)
                                .order_by(contacts.c.id).limit(_CHUNK)).mappings().all()
            if not rows:
                return
            ids = [r["id"] for r in rows]
            tag_map: dict[int, list[str]] = {}
            for cid, name in conn.execute(
                    select(contact_tags.c.contact_id, tags.c.name)
                    .join(tags, contact_tags.c.tag_id == tags.c.id)
                    .where(contact_tags.c.contact_id.in_(ids)).order_by(tags.c.id)):
                tag_map.setdefault(cid, []).append(name)
            obs: dict[int, list[dict]] = {}
            for o in conn.execute(
                    select(observations.c.contact_id, observations.c.text, observations.c.created_at)
                    .where(observations.c.contact_id.in_(ids))
                    .order_by(observations.c.created_at, observations.c.id)):
                obs.setdefault(o.contact_id, []).append({"text": o.text, "created_at": o.created_at})
        for r in rows:
            avatar = avatars_io.plan_export(env, r["phone"])
            yield {"id": r["id"], "phone": r["phone"], **{f: r[f] or "" for f in _CONTACT_TEXT},
                   **({"avatar_file": avatar} if avatar else {}),
                   "ai_enabled": bool(r["ai_enabled"]), "ai_disabled_at": r["ai_disabled_at"],
                   "is_group": bool(r["is_group"]), "is_archived": bool(r["is_archived"]),
                   "is_pinned": bool(r["is_pinned"]), "can_send": bool(r["can_send"]),
                   "tags": tag_map.get(r["id"], []), "observations": obs.get(r["id"], []),
                   "created_at": r["created_at"], "updated_at": r["updated_at"]}
        last = ids[-1]

def _parse_contact(row: dict) -> dict:
    phone = row.get("phone")
    if not isinstance(phone, str) or not _PHONE_RE.match(phone):
        raise ValueError("phone")
    raw_obs = row.get("observations") or []
    if not isinstance(raw_obs, list) or len(raw_obs) > MAX_OBS:
        raise ValueError("obs")
    obs = []
    for o in raw_obs:
        if not isinstance(o, dict):
            raise ValueError("obs")
        text = _txt(o.get("text"), MAX_OBS_TEXT)
        if text.strip():
            obs.append({"text": text, "created_at": _num(o.get("created_at"))})
    now = time.time()
    ai_enabled = _flag(row.get("ai_enabled"), 1)
    created = _num(row.get("created_at")) or now
    return {
        "src": _int_id(row["id"]) if row.get("id") is not None else None,
        "phone": phone, **{f: _txt(row.get(f)) for f in _CONTACT_TEXT},
        "ai_enabled": ai_enabled,
        # só faz sentido com a IA desligada; ligada, NULL (como o app grava)
        "ai_disabled_at": None if ai_enabled else (_num(row.get("ai_disabled_at")) or created),
        "is_group": _flag(row.get("is_group")), "is_archived": _flag(row.get("is_archived")),
        "is_pinned": _flag(row.get("is_pinned")), "can_send": _flag(row.get("can_send"), 1),
        "tags": _str_items(row.get("tags"), MAX_TAGS, 30), "observations": obs,
        "created_at": created, "updated_at": _num(row.get("updated_at")) or created,
        "avatar_file": row.get("avatar_file") if isinstance(row.get("avatar_file"), str) else None,
    }

def _tag_exists(env: Env, name: str) -> bool:
    from db.repositories import tag_repo
    cache = env.cache.setdefault("tag_exists", {})
    if name not in cache:
        cache[name] = tag_repo.get_by_name(name) is not None
    return cache[name] or env.known("tags", name)

def _bump(env: Env, key: str) -> None:
    env.cache[key] = True

def apply_contact(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.engine import get_engine
    from db.repositories import contact_repo, tag_repo
    from db.tables import observations
    try:
        c = _parse_contact(row)
    except (ValueError, TypeError, KeyError):
        env.warn("contacts", "*", "invalid_row")
        return ERROR
    phone = c["phone"]
    seen = env.cache.setdefault("contact_variants", {})
    variants = _variants(phone)
    # O MESMO contato duas vezes no pacote (variantes BR do mesmo número): a segunda não cria outro
    # nem reaplica nada — e é igual no dry-run e no import, que é o que mantém as contagens.
    for v in variants:
        if v in seen:
            env.warn("contacts", phone, "duplicate_in_package")
            if c["src"] is not None:
                env.contact_map[c["src"]] = seen[v]
            return SKIP
    wanted_tags = []
    for name in c["tags"]:
        if _tag_exists(env, name):
            wanted_tags.append(name)
        else:
            env.warn("contacts", phone, "tag_unknown")

    existing = contact_repo.get_by_phone(phone)
    if existing is None:
        cid = None
        if not dry_run:
            cid = _create_contact(c)
            if cid is None:                       # corrida: o webhook criou entre o get e o insert
                existing = contact_repo.get_by_phone(phone)
            else:
                _add_links(cid, wanted_tags, c["observations"], set())
                _bump(env, "contacts_changed")
        if existing is None:
            for v in variants:
                seen[v] = cid
            if c["src"] is not None:
                env.contact_map[c["src"]] = cid
            _import_avatar(env, c, phone, replace=False, dry_run=dry_run)
            env.mark("contacts", phone)
            return CREATE

    cid = existing["id"]
    for v in variants:
        seen[v] = cid
    if c["src"] is not None:
        env.contact_map[c["src"]] = cid
    # A foto vem ANTES do retorno do ``skip``: foto ausente no destino é preenchida mesmo em "só adicionar
    # o que falta"; só ``overwrite`` troca uma existente. Usa o telefone do contato do DESTINO (a variante
    # BR pode diferir do que veio no pacote).
    avatar_changed = _import_avatar(env, c, existing.get("phone") or phone,
                                    replace=strategy == reg.OVERWRITE, dry_run=dry_run)
    if strategy == reg.SKIP:
        return UPDATE if avatar_changed else SKIP

    changes: dict[str, Any] = {}
    if strategy == reg.MERGE:
        for f in _CONTACT_TEXT:
            if c[f] and not (existing.get(f) or ""):
                changes[f] = c[f]
    else:  # overwrite
        for f in _CONTACT_TEXT:
            if c[f] != (existing.get(f) or ""):
                changes[f] = c[f]
        for f in ("ai_enabled", "is_group", "is_archived", "is_pinned", "can_send"):
            if c[f] != int(bool(existing.get(f))):
                changes[f] = c[f]
        if "ai_enabled" in changes:
            changes["ai_disabled_at"] = c["ai_disabled_at"]

    cur_tags = set(tag_repo.get_contact_tags(cid))
    new_tags = [t for t in wanted_tags if t not in cur_tags]
    drop_tags = (cur_tags - set(wanted_tags)) if strategy == reg.OVERWRITE else set()
    with get_engine().connect() as conn:
        have_obs = {r[0] for r in conn.execute(
            select(observations.c.text).where(observations.c.contact_id == cid))}
    new_obs = [o for o in c["observations"] if o["text"] not in have_obs]
    if not (changes or new_tags or drop_tags or new_obs):
        return UPDATE if avatar_changed else SKIP
    if not dry_run:
        if changes:
            _update_contact(cid, changes)
        if drop_tags:
            tag_repo.set_contact_tags(cid, sorted((cur_tags | set(new_tags)) - drop_tags))
        else:
            _add_links(cid, new_tags, new_obs, have_obs)
        _bump(env, "contacts_changed")
    return UPDATE

def _import_avatar(env: Env, c: dict, dest_phone: str, *, replace: bool, dry_run: bool) -> bool:
    """Foto do contato. Falha nunca derruba a linha (vira o aviso ``avatar_failed``)."""
    if not c.get("avatar_file"):
        return False
    try:
        return avatars_io.restore(env, c["avatar_file"], dest_phone, replace=replace, dry_run=dry_run)
    except Exception:  # noqa: BLE001
        env.warn("contacts", "*", "avatar_failed")
        return False

def _create_contact(c: dict) -> int | None:
    """Insere o contato SEM ``contact_repo.get_or_create``: ele carimba ``updated_at`` com agora e a
    lista ordena contatos sem mensagem por ele. ``None`` se outro processo criou o telefone antes."""
    from db.engine import get_engine
    from db.tables import contacts
    values = {"phone": c["phone"], **{f: c[f] for f in _CONTACT_TEXT},
              "ai_enabled": c["ai_enabled"], "ai_disabled_at": c["ai_disabled_at"],
              "is_group": c["is_group"], "is_archived": c["is_archived"], "archived_by_app": 0,
              "is_pinned": c["is_pinned"], "can_send": c["can_send"],
              "unread_count": 0, "unread_ai_count": 0, "has_unread_mention": 0,
              "created_at": c["created_at"], "updated_at": c["updated_at"]}
    try:
        with get_engine().begin() as conn:
            return conn.execute(insert(contacts).values(**values)).inserted_primary_key[0]
    except IntegrityError:
        return None

def _update_contact(contact_id: int, changes: dict) -> None:
    """Atualiza SEM tocar ``updated_at`` (importar não é atividade do contato)."""
    from db.engine import get_engine
    from db.tables import contacts
    with get_engine().begin() as conn:
        conn.execute(update(contacts).where(contacts.c.id == contact_id).values(**changes))

def _add_links(contact_id: int, tag_names: list[str], obs: list[dict], have_obs: set) -> None:
    from db.engine import get_engine
    from db.tables import contact_tags, observations, tags
    from db.upsert import upsert_ignore
    if not tag_names and not obs:
        return
    now = time.time()
    with get_engine().begin() as conn:
        if tag_names:
            for tid in [r[0] for r in conn.execute(select(tags.c.id).where(tags.c.name.in_(tag_names)))]:
                conn.execute(upsert_ignore(contact_tags, {"contact_id": contact_id, "tag_id": tid},
                                           conflict_cols=["contact_id", "tag_id"]))
        seen_text = set(have_obs)
        rows = []
        for o in obs:
            if o["text"] in seen_text:
                continue
            seen_text.add(o["text"])
            rows.append({"contact_id": contact_id, "text": o["text"],
                         "created_at": o["created_at"] or now})
        if rows:
            conn.execute(insert(observations), rows)

# ═══ mensagens ══════════════════════════════════════════════════════════════

def export_messages(env: Env) -> Iterator[dict]:
    """Por contato, paginado pelo cursor composto ``(ts, id)`` (o ``ts`` sozinho pularia mensagens
    com o mesmo timestamp), sem carregar a conversa inteira na memória."""
    from db.engine import get_engine
    from db.tables import contacts, messages as m
    last_contact = 0
    while True:
        with get_engine().connect() as conn:
            batch = [r[0] for r in conn.execute(
                select(contacts.c.id).where(contacts.c.id > last_contact)
                .order_by(contacts.c.id).limit(_CHUNK))]
        if not batch:
            return
        for cid in batch:
            cur_ts, cur_id = -1e18, 0
            while True:
                with get_engine().connect() as conn:
                    rows = conn.execute(
                        select(m).where(m.c.contact_id == cid, or_(
                            m.c.ts > cur_ts, and_(m.c.ts == cur_ts, m.c.id > cur_id)))
                        .order_by(m.c.ts, m.c.id).limit(_PAGE)).mappings().all()
                for r in rows:
                    yield _export_message_row(env, cid, r)
                if len(rows) < _PAGE:
                    break
                cur_ts, cur_id = rows[-1]["ts"], rows[-1]["id"]
        last_contact = batch[-1]

def _export_message_row(env: Env, contact_id: int, r) -> dict:
    path = r["media_path"]
    geo = path if isinstance(path, str) and _GEO_RE.match(path) else None
    extra = media_io.plan_export(env, path, r["ts"]) if path and geo is None else None
    return {
        **(extra or {}),
        "contact": contact_id, "role": r["role"], "content": r["content"], "ts": r["ts"],
        # o caminho de disco da ORIGEM nunca viaja: o arquivo vai pelo nome de pacote (``media_file``)
        # e ganha caminho novo no destino; ``has_file`` só diz que ELE existia na origem
        "media_type": r["media_type"], "media_geo": geo,
        "has_file": bool(path) and geo is None,
        "media_state": r["media_state"], "media_name": r["media_name"],
        "status": r["status"], "msg_id": r["msg_id"], "revoked": bool(r["revoked"]),
        "reactions": r["reactions"], "reply_to_msg_id": r["reply_to_msg_id"],
    }

def _parse_message(row: dict) -> dict:
    role = row.get("role")
    if role not in ROLES:
        raise ValueError("role")
    content = row.get("content", "")
    if not _is_text(content, MAX_PROMPT_BYTES):
        raise ValueError("content")
    ts = _num(row.get("ts"))
    if ts is None:
        raise ValueError("ts")
    reactions = row.get("reactions")
    if reactions is not None:
        if not isinstance(reactions, str) or len(reactions) > 64 * 1024:
            raise ValueError("reactions")
        try:
            if not isinstance(json.loads(reactions), dict):
                raise ValueError("reactions")
        except ValueError:
            raise ValueError("reactions") from None
    state = row.get("media_state")
    if state not in _MEDIA_STATES:
        raise ValueError("media_state")
    geo = row.get("media_geo")
    if geo is not None and not (isinstance(geo, str) and _GEO_RE.match(geo)):
        raise ValueError("media_geo")
    return {
        "contact": _int_id(row.get("contact")), "role": role, "content": content, "ts": ts,
        "media_type": _txt(row.get("media_type"), 32) or None, "media_geo": geo,
        "has_file": _flag(row.get("has_file")), "media_state": state,
        "media_name": _txt(row.get("media_name"), 255) or None,
        "status": _txt(row.get("status"), 32) or None,
        "msg_id": _txt(row.get("msg_id"), 256) or None,
        "revoked": _flag(row.get("revoked")), "reactions": reactions,
        "reply_to_msg_id": _txt(row.get("reply_to_msg_id"), 256) or None,
        "media_file": row.get("media_file") if isinstance(row.get("media_file"), str) else None,
        "media_root": row.get("media_root") if isinstance(row.get("media_root"), str) else None,
    }

def _existing_counter(contact_id: int) -> Counter:
    """O que o contato JÁ tem no destino, como multiconjunto de chaves. Cada linha do pacote consome
    uma ocorrência; o que sobra é o que falta importar (e idêntico legítimo no mesmo segundo não
    colapsa). O md5 é calculado aqui, em Python: o SQLite não tem ``md5()``."""
    from db.engine import get_engine
    from db.tables import messages as m
    out: Counter = Counter()
    with get_engine().connect() as conn:
        res = conn.execution_options(stream_results=True).execute(
            select(m.c.msg_id, m.c.ts, m.c.role, m.c.content).where(m.c.contact_id == contact_id))
        for msg_id, ts, role, content in res:
            out[_msg_key(msg_id, ts, role, content or "")] += 1
    return out

def _resolve_media(env: Env, msg: dict, dry_run: bool) -> tuple[str | None, str | None]:
    """``(media_path, media_state)`` que a mensagem terá no destino.

    ``media_path`` do pacote NUNCA é confiado (é caminho vindo de fora): só localização (``geo:``, que é
    o dado e passa por regex) ou um arquivo que ESTE import copiou com nome novo (``media_io.restore``).
    Mensagem que tinha arquivo na origem e não o traz vira "Arquivo removido" — e o texto fica. Enquete,
    contato, pedido (``media_type`` sem arquivo) NÃO são mídia perdida."""
    if not msg["media_type"]:
        return None, None
    if msg["media_geo"]:
        return msg["media_geo"], None
    if msg["media_state"] in ("discarded", "removed"):
        return None, msg["media_state"]
    if not msg["has_file"] and not msg["media_file"]:
        return None, None
    verdict, path = media_io.restore(env, msg["media_file"], msg["media_root"], dry_run)
    if verdict == "ok":
        return path, None
    if verdict == "absent":
        env.warn("messages", "*", "media_not_included")
    return None, "removed"

def apply_message(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    row_index = env.cache.get("row_index")        # posição da linha no pacote (laço de handlers)
    try:
        msg = _parse_message(row)
    except (ValueError, TypeError):
        env.warn("messages", "*", "invalid_row")
        return ERROR
    if msg["contact"] not in env.contact_map:
        env.warn("messages", "*", "contact_unavailable")
        return SKIP
    dst_id = env.contact_map[msg["contact"]]       # None = contato que o dry-run diz que CRIARIA
    st = env.cache.setdefault("msg_state", {})
    if st.get("contact") != msg["contact"]:
        st["contact"] = msg["contact"]
        st["seen"] = Counter() if dst_id is None else _existing_counter(dst_id)
    seen: Counter = st["seen"]
    key = _msg_key(msg["msg_id"], msg["ts"], msg["role"], msg["content"])
    if seen[key] > 0:
        seen[key] -= 1
        return SKIP
    media_path, media_state = _resolve_media(env, msg, dry_run)
    if dry_run:
        return CREATE
    values = {
        "contact_id": dst_id, "role": msg["role"], "content": msg["content"], "ts": msg["ts"],
        "media_type": msg["media_type"], "media_path": media_path, "media_state": media_state,
        "media_name": msg["media_name"], "status": msg["status"], "msg_id": msg["msg_id"],
        "revoked": msg["revoked"], "reactions": msg["reactions"],
        "reply_to_msg_id": msg["reply_to_msg_id"],
    }
    if "msg_hook" not in env.cache:
        env.cache["msg_hook"] = True
        env.flush_hooks.append(_flush_messages)
    env.msg_buffer.append((values, row_index))
    if len(env.msg_buffer) >= FLUSH_AT:
        _flush_messages(env, False)
    return CREATE

def _insert_batch(batch: list[dict]) -> None:
    """Uma transação curta. No SQLite o webhook disputa o lock de escrita (``busy_timeout`` de 5 s já
    espera); se ainda assim estourar, tenta de novo em vez de perder o lote."""
    from db.engine import get_engine
    from db.tables import messages as m
    for attempt in range(_LOCK_RETRIES):
        try:
            with get_engine().begin() as conn:
                conn.execute(insert(m), batch)
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == _LOCK_RETRIES - 1:
                raise
            time.sleep(0.5 * (attempt + 1))

def _flush_messages(env: Env, dry_run: bool = False) -> None:
    """Grava o lote pendente. Falhou? Regrava linha a linha para achar a ruim."""
    batch, env.msg_buffer = env.msg_buffer, []
    if not batch:
        return
    try:
        _insert_batch([b[0] for b in batch])
        return
    except Exception:  # noqa: BLE001
        logger.warning("[Portability] lote de mensagens falhou; regravando linha a linha", exc_info=True)
    for values, row_index in batch:
        try:
            _insert_batch([values])
        except Exception:  # noqa: BLE001
            env.late_errors["messages"] = env.late_errors.get("messages", 0) + 1
            env.warn("messages", "*", "row_failed")
            if env.error_sink is not None:
                # só a POSIÇÃO e o contato de destino: nunca o conteúdo da mensagem
                env.error_sink.add("messages", row_index, "row_failed",
                                   {"key": f"contato #{values.get('contact_id') or '?'}",
                                    "reason": "row_failed"})
            media_io.discard_if_unreferenced(values.get("media_path"))
