"""Plugins no pacote: CÓDIGO (``plugins/<id>.zip``), CONFIGURAÇÃO (``plugin.<id>.*``) e DADOS (tabelas
``plugin_<id>_*``). Três coisas, três riscos:

**Código.** É código que passa a rodar NESTE servidor, então:

* instalação só por :mod:`plugins.package` — as MESMAS barreiras do "Importar (.zip)" da tela de plugins
  (zip-slip, link simbólico, zip-bomb contado em bytes reais). A prévia (dry-run) faz o ensaio da extração,
  então recusa a mesma bomba que o import recusaria;
* o plugin **entra sempre DESLIGADO**, e o que já estava instalado e é SUBSTITUÍDO também fica desligado
  (mesma regra das ferramentas de IA): trocar o código de algo que roda não pode ser silencioso;
* só é gravado com ``accept_code`` (a rota exige; o motor recusa de novo);
* ``skip`` por padrão. ``overwrite`` só ATUALIZA para versão MAIOR e nunca rebaixa;
* o manifest do zip é validado ANTES de tocar o disco: id do manifest = id do pacote, versão semver,
  ``whatsbot_api_version`` compatível com esta instalação.

**Configuração.** ``plugin.<id>.<campo>``, só de plugin que existe aqui (ou que o pacote traz). Valor de
campo cujo NOME indica credencial (regra ESTREITA ``registry.is_secret_field``) só viaja cifrado; o NOME
decide, nunca a marca ``secret`` do pacote. Valor que aponta para a própria máquina (``127.0.0.1``) não vai.

**Dados.** A tabela de um plugin só existe depois de ele ser ATIVADO e o servidor reiniciar (as migrations
rodam no boot). Então o import de dados tem DUAS etapas: o que o destino já tem entra agora; o resto espera
em ``awaiting_restart`` (``resume_stage='plugins'``) até o operador ativar o plugin, reiniciar e clicar em
"Continuar". Nunca criamos tabela de plugin por conta própria: o schema é do plugin.

⚠️ Sem ``eval`` e sem SQL montado com nome do pacote: todo nome de tabela e de coluna vem do CATÁLOGO do
banco destino (``inspect``), nunca do pacote; o pacote só fornece VALORES (parametrizados).

Diferenças para o Pro, por desenho: o Lite não tem o contrato ``export_data``/``import_data`` por serviço
(só o caminho genérico) nem tabela de mapa de ids. A referência a contato (``contact_id``) viaja pela chave
natural — o TELEFONE — em ``__refs``, então a 2ª etapa, depois de um reinício, a resolve sem estado salvo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import inspect, select, text

from plugins import package
from portability import registry as reg
from portability.base import CREATE, ERROR, OUTCOMES, SKIP, UPDATE, Env
from portability.format import PackageError, plugin_code_path
from portability.jobs import JobCancelled

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SUFFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_COL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_CONFIG_KEY_RE = re.compile(r"^plugin\.([a-z][a-z0-9_]{0,31})\.([A-Za-z0-9_.\-]{1,100})$")
_PAGE = 1000
_BATCH = 200
MAX_PLUGIN_ZIP = package.MAX_ZIP_BYTES
MAX_TABLE_ROWS = 5_000_000
MAX_CONFIG_BYTES = 256 * 1024

#: Tabelas que NUNCA saem de um plugin: nome que indica sessão, credencial ou estado efêmero.
#: Conservador — cai para o lado de NÃO levar.
_NEVER_SUFFIX_RE = re.compile(r"(session|sessions|token|tokens|cache|queue|lock|locks)$")

CONTACT_COL = "contact_id"
MEDIA_COL = "media_path"
REFS_KEY = "__refs"
_MISSING = object()

# ── descoberta ──────────────────────────────────────────────────────────────

def plugins_dir() -> Path:
    from config.settings import get_data_dir
    return Path(get_data_dir()) / "storages" / "plugins"

def installed_ids() -> list[str]:
    """Plugins instalados: com linha no banco E pasta de código em disco (sem link simbólico)."""
    from db.repositories import plugin_repo
    root = plugins_dir()
    out = []
    for r in plugin_repo.list_all():
        pid = r["id"]
        folder = root / pid
        if _ID_RE.match(pid) and folder.is_dir() and not folder.is_symlink():
            out.append(pid)
    return sorted(out)

def installed_summary() -> list[dict]:
    """``[{id, name, version}]`` para a tela escolher o que exportar."""
    from db.repositories import plugin_repo
    out = []
    for pid in installed_ids():
        meta = _manifest_summary(pid)
        row = plugin_repo.get(pid) or {}
        out.append({"id": pid, "name": meta["name"], "version": row.get("version") or meta["version"],
                    "enabled": bool(row.get("enabled"))})
    return out

def _manifest_summary(pid: str) -> dict:
    try:
        from plugins.manifest import load_manifest
        m = load_manifest(plugins_dir() / pid)
        return {"name": m.name, "version": m.version, "dependencies": bool(m.dependencies)}
    except Exception:  # noqa: BLE001 — manifest quebrado: exporta mesmo assim, com o que há
        return {"name": pid, "version": "0.0.0", "dependencies": False}

def _plugin_known(env: Env, pid: str) -> bool:
    if env.known("plugins", pid):
        return True
    return pid in installed_ids()

# ═══ código ═════════════════════════════════════════════════════════════════

def zip_plugin_dir(plugin_id: str, dest_dir: str) -> str | None:
    """Zipa a pasta do plugin em ``dest_dir`` (sem cache nem banco local). ``None`` se não há pasta ou
    se passa do teto. Mesmo critério de arquivos do ``GET /api/plugins/<id>/export``, mas NUNCA segue
    link simbólico (um link para ``/etc`` iria dentro do pacote)."""
    target = plugins_dir() / plugin_id
    if not target.is_dir() or target.is_symlink():
        return None
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, f"{plugin_id}.zip")
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(target, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__"
                             and not os.path.islink(os.path.join(base, d)))
            for fname in sorted(files):
                path = Path(base) / fname
                if path.is_symlink() or path.suffix in (".db", ".db-wal", ".db-shm"):
                    continue
                total += path.stat().st_size
                if total > package.MAX_UNCOMPRESSED_BYTES:
                    zf.close()
                    os.remove(out)
                    return None
                zf.write(path, path.relative_to(target).as_posix())
    return out

def export_plugins(env: Env) -> Iterator[dict]:
    """Uma linha por plugin; o ``.zip`` do código é zipado agora e COPIADO para o pacote depois das
    entidades (o ``zipfile`` não aceita dois fluxos de escrita abertos ao mesmo tempo)."""
    from db.repositories import plugin_repo
    for pid in env.plugin_ids:
        zpath = zip_plugin_dir(pid, env.plugin_tmp) if env.plugin_tmp else None
        if zpath is None:
            env.warn("plugins", pid, "plugin_code_unavailable")
            continue
        env.plugin_zips[pid] = zpath
        meta = _manifest_summary(pid)
        row = plugin_repo.get(pid) or {}
        env.cache.setdefault("exported_plugins", []).append(
            {"id": pid, "version": row.get("version") or meta["version"]})
        yield {"id": pid, "name": meta["name"], "version": row.get("version") or meta["version"],
               "dependencies": meta["dependencies"]}

class _Bad(Exception):
    """Zip de plugin recusado; ``code`` é o código de aviso."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

def _validate_zip(env: Env, pid: str) -> tuple[zipfile.ZipFile, dict]:
    """Lê e valida o zip do plugin. NÃO toca no disco (nem o ensaio de extração grava)."""
    from plugins.manifest import _is_semver, check_api_compat
    reader = env.media_reader
    member = plugin_code_path(pid)
    if reader is None or not reader.has(member):
        raise _Bad("plugin_code_unavailable")
    try:
        blob = reader.read_member(member, limit=MAX_PLUGIN_ZIP)
        zf, meta, zid = package.read_zip_manifest(blob)
        if zid != pid:
            raise ValueError("id diferente")
        package.reject_unsafe_zip_paths(zf)
        package.check_declared_size(zf)
        package.safe_extract(zf, None)               # ensaio: conta os bytes reais, não grava nada
    except (ValueError, PackageError, zipfile.BadZipFile, OSError, EOFError):
        raise _Bad("plugin_package_invalid") from None
    version = str(meta.get("version") or "0.0.0")
    if not _is_semver(version):
        raise _Bad("plugin_package_invalid")
    if not check_api_compat(str(meta.get("whatsbot_api_version") or "*")):
        raise _Bad("plugin_api_incompatible")
    return zf, meta

def _verdict(pid: str, meta: dict) -> str:
    """``new`` | ``newer`` | ``same`` | ``older`` | ``orphan`` — comparado ao que há AQUI."""
    from db.repositories import plugin_repo
    cur = plugin_repo.get(pid)
    folder = plugins_dir() / pid
    if not folder.exists():
        return "new"                                 # inclusive linha antiga sem pasta
    if cur is None:
        return "orphan"                              # pasta sem registro: não adivinhamos o que é
    new_v, old_v = str(meta.get("version") or "0.0.0"), str(cur.get("version") or "0.0.0")
    if package.version_key(new_v) > package.version_key(old_v):
        return "newer"
    if package.version_key(new_v) < package.version_key(old_v):
        return "older"
    return "same"

def apply_plugin(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.repositories import plugin_repo
    pid = row.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        env.warn("plugins", "?", "invalid_row")
        return ERROR
    try:
        zf, meta = _validate_zip(env, pid)
    except _Bad as bad:
        # Literais de propósito: ``tests/test_portability_warnings.py`` confere cada código emitido contra
        # a lista oficial e contra a frase da tela, e não consegue ler um código que vem de variável.
        if bad.code == "plugin_api_incompatible":
            env.warn("plugins", pid, "plugin_api_incompatible")
        elif bad.code == "plugin_code_unavailable":
            env.warn("plugins", pid, "plugin_code_unavailable")
        else:
            env.warn("plugins", pid, "plugin_package_invalid")
        return ERROR
    version = str(meta.get("version") or "0.0.0")
    state = _verdict(pid, meta)
    # Registra o que o pacote TRAZ antes de decidir (qualquer que seja a estratégia): a confirmação do
    # import é exigida pelo código que o pacote carrega, não pelo que a estratégia escolhida faria.
    env.cache.setdefault("code_plugins", {})[pid] = {
        "state": state, "version": version, "dependencies": bool(meta.get("dependencies"))}
    if state in ("same", "older", "newer", "orphan"):
        env.mark("plugins", pid)                     # já existe aqui: a config/dados podem usá-lo
    if state == "same":
        return SKIP
    if state == "older":
        env.warn("plugins", pid, "plugin_downgrade_skipped")
        return SKIP
    if state == "orphan":
        env.warn("plugins", pid, "plugin_orphan_dir")
        return SKIP
    if state == "newer" and strategy != reg.OVERWRITE:
        return SKIP
    if meta.get("dependencies"):
        env.warn("plugins", pid, "plugin_has_dependencies")
    env.warn("plugins", pid, "plugin_installed_disabled" if state == "new" else "plugin_replaced_disabled")
    if not dry_run:
        if not env.accept_code:
            # Defesa em profundidade: a rota já exige ``accept_code``, mas o motor não grava código sem ele.
            env.warn("plugins", pid, "plugin_code_not_accepted")
            return SKIP
        try:
            if state == "new":
                package.install_plugin_dir(zf, plugins_dir(), pid)
            else:
                package.swap_plugin_dir(zf, plugins_dir(), pid)
        except ValueError:
            env.warn("plugins", pid, "plugin_package_invalid")
            return ERROR
        plugin_repo.upsert(pid, version, enabled=False)        # DESLIGADO, sempre
        env.cache.setdefault("plugins_installed", []).append(pid)   # o opt-in "ativar" só mexe nestes
    env.mark("plugins", pid)
    return CREATE if state == "new" else UPDATE

def code_plugins_report(env: Env) -> dict:
    """``{"code_plugins": [{id, state, version, dependencies}], "code_plugins_total": N}``."""
    found = env.cache.get("code_plugins") or {}
    rows = [{"id": k, **v} for k, v in sorted(found.items())]
    return {"code_plugins": rows[:100], "code_plugins_total": len(rows)}

# ═══ configuração ═══════════════════════════════════════════════════════════

def export_plugin_config(env: Env) -> Iterator[dict]:
    from db.repositories import config_repo
    allc = config_repo.get_all()
    for pid in env.plugin_ids:
        prefix = f"plugin.{pid}."
        for key in sorted(k for k in allc if k.startswith(prefix)):
            if not _CONFIG_KEY_RE.match(key):
                continue
            val, field = allc[key], key[len(prefix):]
            if reg.is_secret_field(field):
                if env.include_secrets and isinstance(val, str) and val:
                    env.secrets.setdefault("plugin_config", {})[key] = val
                yield {"key": key, "value": None, "secret": True}
                continue
            if reg.is_environment_value(val):
                env.warn("plugin_config", pid, "environment_value_dropped")
                continue
            yield {"key": key, "value": val, "secret": False}

def _config_value_ok(v: Any) -> bool:
    if v is None or isinstance(v, (bool, int, float)):
        return True
    if isinstance(v, str):
        return len(v) <= 100_000
    if isinstance(v, (list, dict)):
        try:
            return len(json.dumps(v)) <= MAX_CONFIG_BYTES
        except (TypeError, ValueError):
            return False
    return False

def apply_plugin_config(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.repositories import config_repo
    key = row.get("key")
    m = _CONFIG_KEY_RE.match(key) if isinstance(key, str) else None
    if not m:
        env.warn("plugin_config", key if isinstance(key, str) else "?", "invalid_row")
        return ERROR
    pid, field = m.group(1), m.group(2)
    if not _plugin_known(env, pid):
        env.warn("plugin_config", pid, "plugin_unknown")
        return SKIP
    secret = reg.is_secret_field(field)          # o NOME decide, não a marca ``secret`` do pacote
    current = config_repo.get(key, _MISSING)
    if secret:
        value = (env.secrets.get("plugin_config") or {}).get(key)
        if not isinstance(value, str) or not value:
            env.warn("plugin_config", pid, "secret_missing")
            return SKIP
        if current not in (_MISSING, None, "") and not env.overwrite_secrets:
            env.warn("plugin_config", pid, "secret_kept")
            return SKIP
        new: Any = value
    else:
        new = row.get("value")
        if not _config_value_ok(new):
            env.warn("plugin_config", pid, "invalid_value")
            return ERROR
    if current is not _MISSING and current == new:
        return SKIP
    if strategy == reg.SKIP and current is not _MISSING and not (secret and env.overwrite_secrets):
        return SKIP
    if not dry_run:
        config_repo.set(key, new)
        env.cache["plugin_config_changed"] = True
    return CREATE if current is _MISSING else UPDATE

# ═══ dados: tabelas ═════════════════════════════════════════════════════════

def _is_int_column(col: dict) -> bool:
    return "INT" in str(col.get("type")).upper()

def _is_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)

def exportable_tables(pid: str) -> list[str]:
    """Sufixos das tabelas que SAEM no pacote. Lê o catálogo do banco, nunca um nome de fora."""
    from db.engine import get_engine
    prefix = f"plugin_{pid}_"
    out = []
    for t in sorted(inspect(get_engine()).get_table_names()):
        if not t.startswith(prefix):
            continue
        suffix = t[len(prefix):]
        if _SUFFIX_RE.match(suffix) and not _NEVER_SUFFIX_RE.search(suffix):
            out.append(suffix)
    return out

def _table_meta(pid: str, suffix: str) -> dict | None:
    """Colunas e chave primária da tabela (do CATÁLOGO), ou ``None`` se não existe."""
    from db.engine import get_engine
    if not _ID_RE.match(pid) or not _SUFFIX_RE.match(suffix):
        return None
    name = f"plugin_{pid}_{suffix}"
    insp = inspect(get_engine())
    if name not in insp.get_table_names():
        return None
    cols = {c["name"]: c for c in insp.get_columns(name)}
    if not cols or not all(_COL_RE.match(c) for c in cols):
        return None                      # nome de coluna que não dá para citar com segurança
    pk = insp.get_pk_constraint(name).get("constrained_columns") or []
    return {"name": name, "cols": cols, "pk": list(pk)}

def _iter_rows(env: Env, meta: dict, label: str) -> Iterator[dict]:
    """Linhas em páginas por chave primária (keyset: sem OFFSET, que é quadrático)."""
    from db.engine import get_engine
    name, pk = meta["name"], meta["pk"]
    cols = list(meta["cols"])
    sel = ", ".join(f'"{c}"' for c in cols)
    seen = 0
    after: Any = None
    offset = 0
    while True:
        with get_engine().connect() as conn:
            if len(pk) == 1:
                col = pk[0]
                where = f'WHERE "{col}" > :a ' if after is not None else ""
                rows = conn.execute(text(
                    f'SELECT {sel} FROM "{name}" {where}ORDER BY "{col}" LIMIT :n'),
                    {"n": _PAGE, **({"a": after} if after is not None else {})}).mappings().all()
            else:
                order = ", ".join(f'"{c}"' for c in (pk or cols[:1]))
                rows = conn.execute(text(
                    f'SELECT {sel} FROM "{name}" ORDER BY {order} LIMIT :n OFFSET :o'),
                    {"n": _PAGE, "o": offset}).mappings().all()
        for r in rows:
            yield dict(r)
        seen += len(rows)
        if len(rows) < _PAGE:
            return
        if seen >= MAX_TABLE_ROWS:
            env.warn("plugin_data", label, "plugin_table_truncated")
            return
        if len(pk) == 1:
            after = rows[-1][pk[0]]
        else:
            offset += _PAGE

def _pk_key(row: dict, pk: list[str]) -> str | None:
    if not pk or any(c not in row for c in pk):
        return None
    return json.dumps([row[c] for c in pk], default=str, ensure_ascii=False)

def _contact_phones(meta: dict, chunk: list[dict]) -> dict[int, str]:
    """``{id do contato na origem: telefone}`` das linhas do lote (a chave natural que sobrevive)."""
    c = meta["cols"].get(CONTACT_COL)
    if c is None or not _is_int_column(c):
        return {}
    ids = {r[CONTACT_COL] for r in chunk if _is_id(r.get(CONTACT_COL))}
    if not ids:
        return {}
    from db.engine import get_engine
    from db.tables import contacts
    with get_engine().connect() as conn:
        return {r.id: r.phone for r in conn.execute(
            select(contacts.c.id, contacts.c.phone).where(contacts.c.id.in_(ids)))}

def _export_rows(env: Env, pid: str, suffix: str, meta: dict) -> Iterator[dict]:
    """Linhas prontas para o pacote: coluna secreta vira ``None`` (e, com senha, vai para os segredos
    indexada pela chave primária); ``contact_id`` ganha o telefone em ``__refs``; blob não viaja."""
    label = f"{pid}/{suffix}"
    secret = [c for c in meta["cols"] if reg.is_secret_field(c)]
    bag = (env.secrets.setdefault("plugin_data", {}).setdefault(label, {})
           if secret and env.include_secrets else None)
    chunk: list[dict] = []

    def finish() -> Iterator[dict]:
        phones = _contact_phones(meta, chunk)
        for row in chunk:
            for k, v in list(row.items()):
                if isinstance(v, (bytes, bytearray, memoryview)):
                    row[k] = None
                    env.warn("plugin_data", label, "plugin_blob_dropped")
            if secret:
                vals = {c: row.get(c) for c in secret if row.get(c) not in (None, "")}
                if vals:
                    key = _pk_key(row, meta["pk"])
                    if bag is not None and key is not None:
                        bag[key] = vals
                    elif key is None:
                        env.warn("plugin_data", label, "secret_dropped")
                for c in secret:
                    if c in row:
                        row[c] = None
            phone = phones.get(row.get(CONTACT_COL)) if _is_id(row.get(CONTACT_COL)) else None
            if phone:
                row[REFS_KEY] = {"contacts": {CONTACT_COL: phone}}
            yield row
        chunk.clear()

    for row in _iter_rows(env, meta, label):
        chunk.append(row)
        if len(chunk) >= _BATCH:
            yield from finish()
    yield from finish()

def export_data(w: Any, env: Env, tick: Callable[[int], None]) -> int:
    """Grava ``plugin_data/<id>/<tabela>.ndjson`` de cada plugin escolhido. Devolve o total de linhas."""
    total = 0
    for pid in env.plugin_ids:
        for suffix in exportable_tables(pid):
            meta = _table_meta(pid, suffix)
            if meta is None:
                continue
            total += w.write_plugin_table(pid, suffix, _export_rows(env, pid, suffix, meta))
            tick(total)
    return total

# ═══ dados: import ══════════════════════════════════════════════════════════

def _coerce(col_type: Any, v: Any) -> Any:
    """Valor do pacote → o que a coluna aceita. Estrutura (dict/list) só em coluna JSON."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        if "JSON" in type(col_type).__name__.upper():
            return json.dumps(v, ensure_ascii=False)
        raise ValueError("estrutura em coluna escalar")
    if isinstance(v, (str, int, float, bool)):
        return v
    raise ValueError("tipo não suportado")

def _resolve_contact(env: Env, phone: Any, dry_run: bool) -> int | None:
    """Telefone da origem → id do contato AQUI. No dry-run, o contato que o pacote cria conta (-1)."""
    if not isinstance(phone, str) or not phone:
        return None
    cache = env.cache.setdefault("plugin_contacts", {})
    if phone in cache:
        return cache[phone]
    from db.repositories import contact_repo
    found = contact_repo.get_by_phone(phone)
    dst = found["id"] if found else (-1 if dry_run and env.known("contacts", phone) else None)
    cache[phone] = dst
    return dst

def _remap_row(env: Env, row: dict, meta: dict, tally: dict, label: str, dry_run: bool) -> dict | None:
    refs = row.get(REFS_KEY) if isinstance(row.get(REFS_KEY), dict) else {}
    ref_contacts = refs.get("contacts") if isinstance(refs.get("contacts"), dict) else {}
    out: dict = {}
    for col, val in row.items():
        if col == REFS_KEY:
            continue
        c = meta["cols"].get(col)
        if c is None:
            continue                       # coluna que o destino não tem (versão nova): descarta
        if col == CONTACT_COL and _is_int_column(c):
            if val is None:
                out[col] = None
                continue
            dst = _resolve_contact(env, ref_contacts.get(col), dry_run)
            if dst is None:
                if c["nullable"]:
                    out[col] = None        # nunca o id cru: apontaria para OUTRO contato daqui
                    env.warn("plugin_data", label, "reference_cleared")
                    continue
                tally["skip"] += 1
                env.warn("plugin_data", label, "reference_missing")
                return None
            out[col] = dst
            continue
        if col == MEDIA_COL and val:
            # arquivo de plugin não viaja (a mídia do pacote é a das mensagens): um caminho que veio
            # de fora nunca vira caminho de disco
            out[col] = None if c["nullable"] else ""
            env.warn("plugin_data", label, "media_not_included")
            continue
        try:
            out[col] = _coerce(c["type"], val)
        except ValueError:
            tally["error"] += 1
            env.warn("plugin_data", label, "invalid_row")
            return None
    return out

def _table_is_empty(name: str) -> bool:
    from db.engine import get_engine
    with get_engine().connect() as conn:
        return conn.execute(text(f'SELECT 1 FROM "{name}" LIMIT 1')).first() is None

def _can_merge_by_id(meta: dict) -> bool:
    """Mesclar por id só faz sentido com PK inteira de UMA coluna: é o que identifica a linha."""
    pk = meta["pk"]
    return len(pk) == 1 and _is_int_column(meta["cols"][pk[0]])

def _existing_ids(meta: dict) -> set[str]:
    from db.engine import get_engine
    col = meta["pk"][0]
    with get_engine().connect() as conn:
        return {str(r[0]) for r in conn.execute(text(f'SELECT "{col}" FROM "{meta["name"]}"'))}

def _count_rows(reader: Any, pid: str, suffix: str) -> int:
    try:
        return sum(1 for _ in reader.iter_plugin_table(pid, suffix))
    except PackageError:
        return 0

def _fill(conn: Any, reader: Any, env: Env, pid: str, suffix: str, meta: dict, strategy: str,
          busy: bool, dry_run: bool, tick: Callable[[], None]) -> dict:
    """Percorre as linhas da tabela do pacote e grava (``conn``) ou só conta (``conn is None``)."""
    label = f"{pid}/{suffix}"
    name = meta["name"]
    tally = {o: 0 for o in OUTCOMES}
    taken = _existing_ids(meta) if busy else set()
    pk0 = meta["pk"][0] if len(meta["pk"]) == 1 else None
    secret = [c for c in meta["cols"] if reg.is_secret_field(c)]
    secrets = (env.secrets.get("plugin_data") or {}).get(label) or {}
    n = 0
    for row in reader.iter_plugin_table(pid, suffix):
        n += 1
        if n % _BATCH == 0:
            tick()
        if secret:
            key = _pk_key(row, meta["pk"])
            vals = secrets.get(key) if key is not None else None
            if isinstance(vals, dict):
                row = {**row, **{c: v for c, v in vals.items() if c in secret and isinstance(v, str)}}
            elif any(row.get(c) is None for c in secret) and any(c in row for c in secret):
                env.warn("plugin_data", label, "secret_missing")
            # Segredo que não veio: NULL numa coluna NOT NULL derrubaria a tabela inteira. Vazio é o que
            # o operador preenche de novo; o valor nunca vai em claro.
            for c in secret:
                if row.get(c) is None and not meta["cols"][c]["nullable"]:
                    row = {**row, c: ""}
        fixed = _remap_row(env, row, meta, tally, label, dry_run)
        if fixed is None:
            continue
        if busy and pk0 is not None:
            key = str(fixed.get(pk0))
            if key in taken:
                tally["skip"] += 1                 # id ocupado: o dado do destino nunca é trocado
                env.warn("plugin_data", label, "plugin_row_id_conflict")
                continue
            taken.add(key)
        if conn is None:
            tally["create"] += 1
            continue
        cols = sorted(fixed)
        if not cols:
            continue
        sql = text(f'INSERT INTO "{name}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                   f'VALUES ({", ".join(f":p{i}" for i in range(len(cols)))}) ON CONFLICT DO NOTHING')
        made = conn.execute(sql, {f"p{i}": fixed.get(c) for i, c in enumerate(cols)}).rowcount or 0
        tally["create"] += made
        tally["skip"] += 1 - made
    return tally

def _import_table(reader: Any, env: Env, pid: str, suffix: str, strategy: str, dry_run: bool,
                  tally: dict, tick: Callable[[], None], sink: Any) -> str:
    """Importa UMA tabela. Devolve ``'done'`` | ``'pending'`` (a tabela ainda não existe: o plugin
    precisa ser ativado e o servidor reiniciado) | ``'skipped'`` | ``'failed'``."""
    from db.engine import get_engine
    from db.repositories import plugin_repo
    label = f"{pid}/{suffix}"
    meta = _table_meta(pid, suffix)
    if meta is None:
        # Plugin que JÁ rodou migrations e não tem esta tabela: ela não existe nesse plugin (nome forjado
        # ou de outra versão) e ativar/reiniciar não vai criá-la — não vale pausar o job. Só espera quem
        # ainda não migrou (o plugin chegou agora, desligado).
        if plugin_repo.applied_migrations(pid):
            env.warn("plugin_data", label, "plugin_table_unknown")
            tally["skip"] += _count_rows(reader, pid, suffix)
            return "skipped"
        env.warn("plugin_data", pid, "plugin_data_pending")
        return "pending"
    busy = not _table_is_empty(meta["name"])
    if busy and not (strategy == reg.MERGE and _can_merge_by_id(meta)):
        # Sem a chave natural da tabela, ids de origem colidiriam com os do destino (os dois começam em
        # 1): gravar por cima seria perder dado em silêncio. Tabela com dados é pulada INTEIRA, e
        # reimportar o mesmo pacote cai aqui. "Mesclar" é opt-in: só entra linha de id LIVRE.
        tally["skip"] += _count_rows(reader, pid, suffix)
        env.warn("plugin_data", label, "plugin_table_not_empty")
        return "skipped"
    try:
        if dry_run:
            local = _fill(None, reader, env, pid, suffix, meta, strategy, busy, True, tick)
        else:
            with get_engine().begin() as conn:                 # UMA transação por tabela: tudo ou nada
                local = _fill(conn, reader, env, pid, suffix, meta, strategy, busy, False, tick)
    except JobCancelled:
        raise
    except Exception:  # noqa: BLE001 — a tabela falha INTEIRA e nada dela é gravado; o job segue
        logger.warning("[Portability] tabela %s falhou", label, exc_info=True)
        n = _count_rows(reader, pid, suffix)
        tally["error"] += n
        env.warn("plugin_data", label, "plugin_table_failed")
        if sink is not None:
            sink.add("plugin_data", None, "plugin_table_failed", {"key": label, "reason": "plugin_table_failed"})
        return "failed"
    for k, v in local.items():
        tally[k] += v
    if not dry_run:
        env.cache["plugin_data_changed"] = True
    return "done"

def has_plugin_data(reader: Any) -> bool:
    return any(reader.plugin_tables(pid) for pid in reader.plugin_ids())

def import_data(reader: Any, env: Env, strategy: str, dry_run: bool, tick: Callable[[int], None],
                sink: Any, only: set[str] | None = None) -> tuple[dict, list[str]]:
    """Importa as tabelas de plugin do pacote. Devolve ``(tally, pendentes)``; ``pendentes`` são
    ``"<plugin>/<tabela>"`` cuja tabela ainda não existe (``only`` restringe à 2ª etapa)."""
    tally = {o: 0 for o in OUTCOMES}
    pending: list[str] = []
    done_tables = 0
    for pid in reader.plugin_ids():
        for suffix in reader.plugin_tables(pid):
            label = f"{pid}/{suffix}"
            if only is not None and label not in only:
                continue
            if not _plugin_known(env, pid):
                env.warn("plugin_data", pid, "plugin_unknown")
                tally["skip"] += _count_rows(reader, pid, suffix)
                continue
            outcome = _import_table(reader, env, pid, suffix, strategy, dry_run, tally,
                                    lambda: tick(done_tables), sink)
            done_tables += 1
            tick(done_tables)
            if outcome == "pending":
                pending.append(label)
    if not dry_run and env.cache.get("plugin_data_changed"):
        _repair_sequences()
    return tally, sorted(set(pending))

def _repair_sequences() -> None:
    """Id EXPLÍCITO em coluna IDENTITY do Postgres não avança a sequence: sem isto o próximo INSERT do
    plugin (id implícito) bate numa linha importada. No SQLite o autoincremento já acompanha."""
    from db.engine import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return
    try:
        from db.migration_postgres import repair_postgres_sequences
        repair_postgres_sequences(engine)
    except Exception:  # noqa: BLE001 — a sequence é conforto; os dados já estão gravados
        logger.warning("[Portability] não consegui reancorar as sequences", exc_info=True)
