"""Peças compartilhadas por todas as entidades: veredito, ``Env``, validações.

``Env`` carrega o estado de UMA execução (export, dry-run ou import):

* ``virtual`` — o que passaria a EXISTIR depois do import. No dry-run nada é gravado, então uma
  referência a algo que o próprio pacote cria só se resolve por aqui. É assim que o relatório do
  dry-run casa com o import real.
* ``contact_map`` — ``{id do contato na origem: id aqui}``: liga a mensagem do pacote ao contato
  certo no destino. Só existe em memória (contatos e mensagens sempre viajam juntos).
* ``flush_hooks`` — passos que rodam ao FIM das entidades (também quando o job é cancelado).

O lite tem uma instalação, uma senha e um canal: não há usuários, times, caixas nem conversas
separadas do contato. Por isso este módulo é bem menor que o do WhatsBot Pro.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# ── veredito ────────────────────────────────────────────────────────────────

CREATE, UPDATE, SKIP, ERROR = "create", "update", "skip", "error"
OUTCOMES = (CREATE, UPDATE, SKIP, ERROR)

MAX_TEXT_BYTES = 256 * 1024
MAX_PROMPT_BYTES = 1024 * 1024

# Cor de tag. A UI a injeta em ``style="color:${cor}"``: um valor como ``red;background:url(//x)``
# viraria uma requisição para fora, então só entram hex, nome de cor simples e ``rgb()/hsl()`` —
# nunca ``;`` ``:`` ``url(`` nem aspas.
_COLOR_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|(?:rgb|hsl)a?\([0-9 ,.%/]{3,40}\))$")

@dataclass
class Env:
    """Contexto de uma execução de export OU import."""
    include_secrets: bool = False
    #: ``{entidade: {chave_natural: {campo: valor}}}`` — no export é preenchido; no import é o
    #: conteúdo já decifrado (vazio quando o pacote não tem segredos / sem senha).
    secrets: dict = field(default_factory=dict)
    overwrite_secrets: bool = False
    #: chaves de config alteradas pelo import (para o pós-processo).
    config_changed: list[str] = field(default_factory=list)
    #: ``{tipo: {chave}}`` do que existiria após o import (ver docstring do módulo).
    virtual: dict[str, set[str]] = field(default_factory=dict)
    finalizers: list = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    #: ``{id_do_contato_na_origem: id_aqui}``; no dry-run guarda ids negativos fictícios.
    contact_map: dict = field(default_factory=dict)
    #: caches efêmeros desta execução (ids de contato por telefone, tags existentes…).
    cache: dict = field(default_factory=dict)
    #: fila de INSERT em lote de mensagens (ver ``entities_contacts``).
    msg_buffer: list = field(default_factory=list)
    #: linhas que o lote não conseguiu gravar DEPOIS de já terem sido contadas como criadas.
    late_errors: dict[str, int] = field(default_factory=dict)
    #: rodam ao fim das entidades (também quando o job é cancelado no meio).
    flush_hooks: list = field(default_factory=list)
    #: último ``(entidade, chave, código)`` avisado — o laço o usa como motivo do ERRO da linha.
    last_warn: tuple | None = None
    #: destino dos erros por linha (``jobs.ErrorSink``); ``None`` = não grava (export, testes).
    error_sink: Any = None
    _wmap: dict = field(default_factory=dict)
    _finalizer_keys: set[str] = field(default_factory=set)

    # ── avisos / marcas ─────────────────────────────────────────────────────

    def warn(self, entity: str, key: str, code: str, n: int = 1) -> None:
        """Registra um aviso. Repetição do MESMO (entidade, chave, código) só soma ``n`` — um import
        de milhões de mensagens não pode crescer a lista na memória."""
        k = (entity, str(key)[:80], code)
        self.last_warn = (entity, k[1], code)
        hit = self._wmap.get(k)
        if hit is not None:
            hit["n"] += n
            return
        entry = {"entity": entity, "key": k[1], "code": code, "n": n}
        self._wmap[k] = entry
        self.warnings.append(entry)

    def mark(self, kind: str, key: str) -> None:
        """Registra que ``key`` existe (ou passará a existir) — ver ``virtual``."""
        self.virtual.setdefault(kind, set()).add(key)

    def known(self, kind: str, key: str) -> bool:
        return key in self.virtual.get(kind, ())

    def add_finalizer(self, key: str, fn: Callable[["Env", bool], None]) -> None:
        if key not in self._finalizer_keys:
            self._finalizer_keys.add(key)
            self.finalizers.append(fn)

    def finalize(self, dry_run: bool) -> None:
        for fn in self.finalizers:
            fn(self, dry_run)

@dataclass(frozen=True)
class Entity:
    name: str
    export: Callable[[Env], Iterator[dict]]
    apply: Callable[[dict, str, Env, bool], str]

# ── helpers ─────────────────────────────────────────────────────────────────

def _is_text(v: Any, limit: int = MAX_TEXT_BYTES) -> bool:
    return isinstance(v, str) and len(v.encode("utf-8")) <= limit

def _str_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, list) and all(isinstance(i, str) for i in v):
        return v
    raise ValueError("lista inválida")

def valid_color(v: Any) -> bool:
    return isinstance(v, str) and bool(_COLOR_RE.match(v.strip()))

def mask_phone(phone: str) -> str:
    """``***1234`` — o relatório vai para a tela e telefone é dado pessoal."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"
