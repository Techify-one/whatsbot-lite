"""Mídia das mensagens: o que entra no pacote e como volta para o disco.

Só dois diretórios existem para este módulo — ``statics/media`` (recebida) e ``statics/operator``
(enviada pelo painel) — e só arquivo REGULAR direto dentro deles. O resto do disco (banco, sessão do
WhatsApp, código, ``.env``) é inalcançável por construção. As raízes vêm do ``file_manager`` (uma só
fonte): se a tela Arquivos ganhar uma pasta, este módulo a vê junto.

Regras:

* **Nenhum caminho do pacote vira caminho de disco.** No pacote o arquivo tem um nome derivado do hash
  do caminho de origem (``media/<sha1[:20]><ext>``); no destino ganha um nome NOVO e único, e a extensão
  passa por uma ALLOWLIST: o ``/statics`` é um ``StaticFiles`` puro, então um ``.html``/``.svg``/``.xml``
  importado seria servido na MESMA origem do painel (script rodando com a sessão do operador). Fora da
  lista vira ``.bin``.
* **Origem validada de novo na hora de copiar** (não só ao planejar): link simbólico, diretório,
  arquivo que sumiu ou que saiu do diretório ⇒ não entra, sem derrubar o job.
* **Tetos**: por arquivo e total. Passou ⇒ a mensagem vai SEM o arquivo, com aviso — o texto nunca se perde.
* Disco cheio no destino ⇒ para de gravar mídia e mantém as mensagens (``media_disk_full``).
* Funções recebem o diretório de dados por ``statics_dir()`` (uma costura só, trocável em teste).
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat as _stat
import uuid
from typing import Any, Callable

from portability.format import _NAME_RE, media_path as member_path

try:                                    # a mesma fonte de verdade da tela Arquivos
    from server.file_manager import (AUDIO_EXT, IMAGE_EXT, ROOTS, VIDEO_EXT, _MIME_BY_EXT)
except Exception:  # noqa: BLE001 — nunca impede o módulo de carregar (ex.: ambiente de teste mínimo)
    ROOTS = {"received": "media", "sent": "operator"}
    IMAGE_EXT = AUDIO_EXT = VIDEO_EXT = frozenset()
    _MIME_BY_EXT = {}

_ROOT_BY_DIR = {v: k for k, v in ROOTS.items()}
_REL_RE = re.compile(r"^statics/(" + "|".join(re.escape(d) for d in ROOTS.values())
                     + r")/([^/\\\x00]+)$")
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")

#: Extensões que o navegador exibe/executa como PÁGINA. Nunca entram, mesmo que alguém as ponha na allowlist.
ACTIVE_EXT = frozenset({".html", ".htm", ".xhtml", ".xht", ".shtml", ".svg", ".svgz", ".xml", ".xsl",
                        ".xslt", ".js", ".mjs", ".css", ".swf", ".hta", ".mht", ".mhtml"})
#: Documentos e arquivos comuns de WhatsApp, além do que o file_manager já reconhece.
_EXTRA_SAFE_EXT = frozenset({
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".zip", ".rar",
    ".7z", ".gz", ".tar", ".vcf", ".ics", ".apk", ".exe", ".msi", ".dmp", ".bin", ".dat", ".flac",
    ".wma", ".wmv", ".mpeg", ".mpg", ".ts", ".opus"})
SAFE_EXT = ((IMAGE_EXT | AUDIO_EXT | VIDEO_EXT | frozenset(_MIME_BY_EXT) | _EXTRA_SAFE_EXT) - ACTIVE_EXT)

MAX_MEDIA_FILE = 512 * 1024 ** 2
#: Abaixo do teto de upload (5 GB): mídia não comprime, então o pacote ≈ soma dos arquivos.
MAX_MEDIA_TOTAL = 4 * 1024 ** 3

def statics_dir() -> str:
    from config.settings import get_data_dir
    return str(get_data_dir() / "statics")

def safe_extension(name: str) -> str:
    """Extensão que o arquivo importado terá no disco: a original (minúscula) só se estiver na allowlist."""
    ext = os.path.splitext(str(name))[1].lower()
    return ext if _EXT_RE.match(ext) and ext in SAFE_EXT else ".bin"

def unique_media_name(member: str) -> str:
    """Nome NOVO e único no destino: nunca o do pacote, nunca colide (uuid), extensão filtrada."""
    return f"{uuid.uuid4().hex}{safe_extension(member)}"

# ── parâmetros do export ────────────────────────────────────────────────────

def _epoch(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("Período de mídia inválido.")
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")) or f < 0 or f > 1e11:
        raise ValueError("Período de mídia inválido.")
    return f

def parse_params(raw: dict, scopes: list[str]) -> dict:
    """``{}`` (sem mídia) ou ``{include_media, media_from, media_to}``. ``ValueError`` com mensagem
    segura para ir ao usuário."""
    v = raw.get("include_media")
    on = v is True or (isinstance(v, str) and v.lower() in ("1", "true", "on", "yes"))
    if not on:
        return {}
    if "conversations" not in scopes:
        raise ValueError("A mídia pertence às conversas: inclua “Contatos e conversas”.")
    lo, hi = _epoch(raw.get("media_from")), _epoch(raw.get("media_to"))
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("O período de mídia está invertido.")
    return {"include_media": True, "media_from": lo, "media_to": hi}

# ── export ──────────────────────────────────────────────────────────────────

def source_of(media_path: Any) -> tuple[str, str, str, int] | None:
    """``(raiz, nome, caminho_absoluto, bytes)`` de um arquivo servível, ou ``None``.

    ``None`` para tudo que não seja arquivo regular direto em ``statics/media|operator``: caminho com
    ``..``/subpasta/barra invertida/NUL, link simbólico (do arquivo OU de um diretório do caminho),
    diretório, ausente."""
    if not isinstance(media_path, str):
        return None
    m = _REL_RE.match(media_path.lstrip("/"))
    if not m:
        return None
    dirname, name = m.group(1), m.group(2)
    if name in (".", "..") or name.startswith("."):
        return None
    base = os.path.realpath(os.path.join(statics_dir(), dirname))
    path = os.path.join(base, name)
    try:
        if os.path.islink(path) or os.path.realpath(path) != path:
            return None
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return None
    return _ROOT_BY_DIR[dirname], name, path, st.st_size

def member_name(root: str, name: str) -> str:
    """Nome do arquivo DENTRO do pacote: hash do caminho de origem + extensão segura."""
    ext = os.path.splitext(name)[1].lower()
    ext = ext if _EXT_RE.match(ext) else ""
    return hashlib.sha1(f"{root}/{name}".encode(), usedforsecurity=False).hexdigest()[:20] + ext  # noqa: S324

def plan_export(env, media_path: Any, ts: float) -> dict | None:
    """Decide se o arquivo desta mensagem vai no pacote. Devolve os campos que a LINHA da mensagem leva
    (``media_file``/``media_root``) ou ``None`` (a mensagem vai só com o texto)."""
    cfg = env.media
    if not cfg.get("enabled") or not media_path:
        return None
    lo, hi = cfg.get("from"), cfg.get("to")
    if (lo is not None and ts < lo) or (hi is not None and ts > hi):
        env.warn("messages", "*", "media_out_of_period")
        return None
    src = source_of(media_path)
    if src is None:
        env.warn("messages", "*", "media_missing")
        return None
    root, name, _abs, size = src
    if size > MAX_MEDIA_FILE:
        env.warn("messages", "*", "media_too_large")
        return None
    member = member_name(root, name)
    if member not in env.media_wanted:
        if env.media_bytes + size > MAX_MEDIA_TOTAL:
            env.warn("messages", "*", "media_limit_reached")
            return None
        env.media_wanted[member] = (root, name)
        env.media_bytes += size
    return {"media_file": member, "media_root": root}

def copy_wanted(writer, env, tick: Callable[[int], None]) -> int:
    """Copia para o pacote os arquivos marcados, SEM compressão. Devolve quantos foram."""
    copied = 0
    for i, (member, (root, name)) in enumerate(env.media_wanted.items()):
        tick(i)
        src = source_of(f"statics/{ROOTS[root]}/{name}")   # valida DE NOVO (TOCTOU)
        if src is None:
            env.warn("messages", "*", "media_missing")
            continue
        try:
            writer.copy_file(member_path(member), src[2], stored=True)
        except OSError:
            env.warn("messages", "*", "media_missing")
            continue
        copied += 1
    return copied

# ── import ──────────────────────────────────────────────────────────────────

def restore(env, member: Any, root: Any, dry_run: bool) -> tuple[str, str | None]:
    """Traz um arquivo do pacote para o disco do destino.

    ``("ok", caminho_relativo)`` · ``("absent", None)`` (o pacote não traz) · ``("failed", None)``.
    No dry-run devolve ``("ok", None)`` sem escrever nada. O mesmo arquivo referenciado por várias
    mensagens é gravado UMA vez."""
    if (not isinstance(member, str) or not _NAME_RE.match(member)
            or not isinstance(root, str) or root not in ROOTS
            or member not in env.media_members):
        return "absent", None
    done = env.cache.setdefault("media_done", {})
    key = (root, member)
    if key in done:
        return done[key]
    if dry_run:
        done[key] = ("ok", None)
        return done[key]
    if env.cache.get("media_disk_full"):
        return "failed", None
    entry = env.media_reader.manifest.files.get(member_path(member)) or {}
    size = entry.get("bytes")
    if not isinstance(size, int) or size > MAX_MEDIA_FILE:
        env.warn("messages", "*", "media_too_large")
        done[key] = ("failed", None)
        return done[key]
    dest_dir = os.path.join(statics_dir(), ROOTS[root])
    fname = unique_media_name(member)
    final = os.path.join(dest_dir, fname)
    part = final + ".part"
    try:
        os.makedirs(dest_dir, exist_ok=True)
        got = 0
        with env.media_reader.open_member(member_path(member)) as src, open(part, "xb") as dst:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                got += len(buf)
                if got > size:
                    raise OSError("arquivo maior que o declarado")
                dst.write(buf)
        os.replace(part, final)
    except OSError as exc:
        try:
            os.remove(part)
        except OSError:
            pass
        if getattr(exc, "errno", None) == errno.ENOSPC:
            env.cache["media_disk_full"] = True
            env.warn("messages", "*", "media_disk_full")
        else:
            env.warn("messages", "*", "media_failed")
        return "failed", None
    env.cache["media_restored"] = True
    done[key] = ("ok", f"statics/{ROOTS[root]}/{fname}")
    return done[key]

def media_path_is_referenced(media_path: str) -> bool:
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import messages
    with get_engine().connect() as conn:
        return conn.execute(select(messages.c.id).where(
            messages.c.media_path == media_path).limit(1)).first() is not None

def discard_if_unreferenced(media_path: str | None) -> None:
    """A mensagem que usava este arquivo não foi gravada. Se nenhuma outra o referencia, apaga — senão
    sobraria arquivo órfão no disco. Só mexe em arquivo que ``source_of`` aceita (dentro das raízes)
    e que nenhuma mensagem aponta."""
    src = source_of(media_path)
    if src is None or media_path_is_referenced(media_path):
        return
    try:
        os.remove(src[2])
    except OSError:
        pass
