"""Fotos de perfil dos contatos: o que entra no pacote e como volta para o disco.

A foto não está no banco: é o arquivo ``statics/avatars/<phone>.jpg`` da instância de origem
(``server/avatars.py``). Segue as mesmas regras da mídia (``portability.media``):

* **Nenhum nome do pacote vira nome de disco.** No pacote o arquivo é ``avatars/<sha1(telefone)[:20]>.jpg``;
  no destino o nome é montado a partir do telefone do contato JÁ RESOLVIDO no destino (variante BR
  incluída), nunca do que veio na linha.
* **Origem validada de novo na hora de copiar**: link simbólico, arquivo oculto, diretório ou arquivo
  que sumiu ⇒ não entra, sem derrubar o job.
* **Só JPEG** (cabeçalho ``FF D8 FF``), com teto por foto e no total. Gravação atômica (``.part`` +
  ``os.replace``); o dry-run nunca grava.
* Foto ausente no destino é PREENCHIDA em qualquer estratégia; só ``overwrite`` troca uma existente.

⚠️ Ao contrário do WhatsBot Pro, aqui a foto vem DESLIGADA por padrão: o lite refaz o cache sozinho
(``server/avatars.py`` busca no GOWA ao abrir a conversa e a cada 30 min), então levar a foto só incha o
pacote. Fica como opção para quem quer a tela idêntica no primeiro acesso.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as _stat
from typing import Any, Callable

from portability.format import _NAME_RE, avatar_path as member_path

#: Uma foto de perfil do WhatsApp tem dezenas de KB.
MAX_AVATAR_FILE = 1024 * 1024
MAX_AVATAR_TOTAL = 256 * 1024 ** 2

# '@' entra de propósito: o "telefone" de um GRUPO é o JID ("1203…@g.us") e a foto dele mora em
# ``<jid>.jpg``. Continua sem '/', '\\' nem espaço, então não escapa da pasta.
_PHONE_FILE_RE = re.compile(r"^[A-Za-z0-9_.:+@\-]{3,100}$")
_JPEG_MAGIC = b"\xff\xd8\xff"

def avatars_dir() -> str:
    from portability.media import statics_dir
    return os.path.join(statics_dir(), "avatars")

def member_name(phone: str) -> str:
    """Nome do arquivo DENTRO do pacote: hash do telefone, nunca o telefone."""
    return hashlib.sha1(phone.encode(), usedforsecurity=False).hexdigest()[:20] + ".jpg"  # noqa: S324

def _safe_phone(phone: Any) -> bool:
    return (isinstance(phone, str) and bool(_PHONE_FILE_RE.match(phone))
            and not phone.startswith(".") and ".." not in phone)

def source_of(phone: Any) -> tuple[str, int] | None:
    """``(caminho_absoluto, bytes)`` da foto de ``phone`` ou ``None``.

    ``None`` para tudo que não seja arquivo regular direto em ``statics/avatars``: telefone com
    caractere de caminho, arquivo oculto, link simbólico (do arquivo OU da pasta), diretório, ausente."""
    if not _safe_phone(phone):
        return None
    base = os.path.realpath(avatars_dir())
    path = os.path.join(base, f"{phone}.jpg")
    try:
        if os.path.islink(path) or os.path.realpath(path) != path:
            return None
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return None
    return path, st.st_size

# ── parâmetros do export ────────────────────────────────────────────────────

def parse_params(raw: dict, scopes: list[str]) -> bool:
    """``include_avatars``: DESLIGADO por padrão e só vale com o escopo de conversas (a foto pertence
    ao contato). ``"false"``/``"0"`` em texto não ligam."""
    if "conversations" not in scopes:
        return False
    v = raw.get("include_avatars", False)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return v is True

# ── export ──────────────────────────────────────────────────────────────────

def plan_export(env, phone: str) -> str | None:
    """Nome (no pacote) da foto deste contato, ou ``None``. Marca o arquivo para a cópia final."""
    if not getattr(env, "avatars", False):
        return None
    src = source_of(phone)
    if src is None:
        return None
    _abs, size = src
    if size > MAX_AVATAR_FILE or size < len(_JPEG_MAGIC):
        env.warn("contacts", "*", "avatar_too_large" if size > MAX_AVATAR_FILE else "avatar_failed")
        return None
    member = member_name(phone)
    if member not in env.avatar_wanted:
        if env.avatar_bytes + size > MAX_AVATAR_TOTAL:
            env.warn("contacts", "*", "avatar_too_large")
            return None
        env.avatar_wanted[member] = phone
        env.avatar_bytes += size
    return member

def copy_wanted(writer, env, tick: Callable[[int], None] | None = None) -> int:
    """Copia as fotos marcadas para o pacote, SEM compressão. Devolve quantas foram."""
    copied = 0
    for i, (member, phone) in enumerate(env.avatar_wanted.items()):
        if tick:
            tick(i)
        src = source_of(phone)                      # valida DE NOVO (TOCTOU)
        if src is None or src[1] > MAX_AVATAR_FILE:
            continue
        try:
            with open(src[0], "rb") as fh:
                if fh.read(3) != _JPEG_MAGIC:
                    env.warn("contacts", "*", "avatar_failed")
                    continue
            writer.copy_file(member_path(member), src[0], stored=True)
        except OSError:
            continue
        copied += 1
    return copied

# ── import ──────────────────────────────────────────────────────────────────

def restore(env, member: Any, dest_phone: str, *, replace: bool, dry_run: bool) -> bool:
    """Traz a foto do pacote para ``statics/avatars/<dest_phone>.jpg``.

    ``True`` se criou/trocou (ou, no dry-run, se criaria/trocaria). ``False`` se não havia o que fazer
    ou se falhou (o aviso ``avatar_failed`` já foi registrado, exceto quando o pacote simplesmente não
    traz a foto). ``replace`` = ``overwrite``: troca a existente; sem ele só preenche a ausente."""
    if (not isinstance(member, str) or not _NAME_RE.match(member)
            or member not in env.avatar_members or not _safe_phone(dest_phone)):
        return False
    final = os.path.join(avatars_dir(), f"{dest_phone}.jpg")
    present = os.path.lexists(final)
    if present and not replace:
        return False
    if dry_run:
        return True
    entry = env.media_reader.manifest.files.get(member_path(member)) or {}
    size = entry.get("bytes")
    if not isinstance(size, int) or size > MAX_AVATAR_FILE or size < len(_JPEG_MAGIC):
        env.warn("contacts", "*", "avatar_failed")
        return False
    part = final + ".part"
    try:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        if present and (os.path.islink(final) or not os.path.isfile(final)):
            raise OSError("destino não é arquivo regular")
        got = bytearray()
        with env.media_reader.open_member(member_path(member)) as src:
            while True:
                buf = src.read(64 * 1024)
                if not buf:
                    break
                got += buf
                if len(got) > size:
                    raise OSError("arquivo maior que o declarado")
        if bytes(got[:3]) != _JPEG_MAGIC:
            raise OSError("não é JPEG")
        if present:
            with open(final, "rb") as cur:
                if cur.read(MAX_AVATAR_FILE + 1) == bytes(got):
                    return False                    # idêntica: nada a trocar
        with open(part, "xb") as dst:
            dst.write(got)
        os.replace(part, final)
    except OSError:
        try:
            os.remove(part)
        except OSError:
            pass
        env.warn("contacts", "*", "avatar_failed")
        return False
    return True
