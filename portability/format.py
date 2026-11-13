"""Formato do pacote ``.zip`` de importar/exportar.

Layout::

    manifest.json
    data/<entidade>.ndjson            uma linha JSON por registro
    media/<arquivo>                   opcional
    avatars/<hash>.jpg                opcional (foto do contato; hash do telefone)
    plugins/<id>.zip                  código do plugin
    plugin_data/<id>/<tabela>.ndjson  dados do plugin
    secrets.enc                       opcional (ver :mod:`portability.crypto`)

Este módulo é PURO (sem banco, sem rede): escrita em streaming num ``zipfile``,
leitura segura (zip-slip, zip-bomb) e checksums SHA-256. O pacote é gravado em
disco (nunca em ``BytesIO``) para o uso de memória não crescer com o volume.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import IO, Any, Iterable, Iterator

SCHEMA_VERSION = 1
#: Identifica o PRODUTO que gerou o pacote. O formato do lite é próprio (não é o do whatsbot-pro:
#: não há conversa/caixa/canal separados), então um pacote sem esta marca — ou com outra — é
#: recusado em vez de ser lido pela metade.
PRODUCT = "whatsbot-lite"
MANIFEST_NAME = "manifest.json"
SECRETS_NAME = "secrets.enc"

# Teto de leitura (zip-bomb). Valores deliberadamente conservadores: o plano
# propõe ~20× de razão e ~10 GB descompactado. Sobrescreva no chamador se precisar.
MAX_ENTRIES = 200_000
MAX_UNCOMPRESSED_BYTES = 10 * 1024 ** 3
MAX_RATIO = 20.0
# NDJSON é texto com as MESMAS chaves em toda linha: comprime 30–50× legitimamente (medido com
# 100 mil mensagens). Um teto de 20× recusava o pacote que o próprio export gera. Zeros/lixo
# repetido passam de 1000×, então 200× ainda separa os dois — e o que protege a memória não é
# a razão, é o teto de LINHA (``MAX_LINE_BYTES``) mais o teto de bytes descompactados.
MAX_NDJSON_RATIO = 200.0
# Maior linha de NDJSON aceita. Uma mensagem de WhatsApp tem dezenas de KB; 16 MB é folga enorme
# e impede que um "arquivo de uma linha só" seja lido inteiro para a memória.
MAX_LINE_BYTES = 16 * 1024 * 1024
# Abaixo disto a razão não é medida: arquivo minúsculo comprime absurdamente bem
# (um NDJSON de 3 linhas) e não é ameaça.
_RATIO_FLOOR_BYTES = 1024 * 1024

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")

class PackageError(Exception):
    """Pacote inválido ou inseguro. A mensagem é segura para ir ao relatório."""

# ── caminhos ────────────────────────────────────────────────────────────────

def entity_path(entity: str) -> str:
    if not _NAME_RE.match(entity):
        raise PackageError(f"Nome de entidade inválido: {entity!r}")
    return f"data/{entity}.ndjson"

def plugin_code_path(plugin_id: str) -> str:
    if not re.match(r"^[a-z][a-z0-9_]{0,31}$", plugin_id):
        raise PackageError(f"Id de plugin inválido: {plugin_id!r}")
    return f"plugins/{plugin_id}.zip"

def plugin_data_path(plugin_id: str, table: str) -> str:
    plugin_code_path(plugin_id)  # valida o id
    if not _NAME_RE.match(table):
        raise PackageError(f"Nome de tabela inválido: {table!r}")
    return f"plugin_data/{plugin_id}/{table}.ndjson"

def media_path(filename: str) -> str:
    if not _NAME_RE.match(filename):
        raise PackageError(f"Nome de arquivo de mídia inválido: {filename!r}")
    return f"media/{filename}"

def avatar_path(filename: str) -> str:
    if not _NAME_RE.match(filename):
        raise PackageError(f"Nome de foto inválido: {filename!r}")
    return f"avatars/{filename}"

def is_safe_member_name(name: str) -> bool:
    """Falso para caminho absoluto, ``..``, barra invertida, byte nulo ou vazio.

    ``posixpath.normpath`` colapsa ``a/../b`` ⇒ ``b``, então comparamos o
    resultado com o original: qualquer diferença (``./``, ``//``, ``..``) é
    rejeitada em vez de "corrigida".
    """
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", name):  # C:\... mesmo com barra normal
        return False
    norm = posixpath.normpath(name.rstrip("/")) if name != "/" else "/"
    if norm != name.rstrip("/"):
        return False
    return not any(part in ("", ".", "..") for part in norm.split("/"))

# ── manifest ────────────────────────────────────────────────────────────────

@dataclass
class Manifest:
    schema_version: int = SCHEMA_VERSION
    product: str = PRODUCT
    app_version: str = ""
    alembic_head: str = ""
    created_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)  # path -> {sha256, bytes}
    plugins: list[dict[str, Any]] = field(default_factory=list)
    source_instance: str = ""
    secrets: dict[str, Any] = field(default_factory=lambda: {"present": False})

    def to_json(self) -> bytes:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2,
                          sort_keys=True).encode("utf-8")

    def aad(self) -> bytes:
        """Dados autenticados do ``secrets.enc`` (AAD do AES-GCM).

        NÃO é o manifest inteiro: ele lista o checksum do próprio ``secrets.enc`` em
        ``files``, e cifrar com um AAD que depende do cifrado seria circular. É o
        subconjunto estável que identifica ESTE pacote — adulterar qualquer um destes
        campos invalida a decifragem.
        """
        core = {"schema_version": self.schema_version, "product": self.product,
                "created_at": self.created_at,
                "source_instance": self.source_instance, "scopes": sorted(self.scopes)}
        return json.dumps(core, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "Manifest":
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise PackageError("manifest.json ilegível.") from None
        if not isinstance(data, dict):
            raise PackageError("manifest.json inválido.")
        version = data.get("schema_version")
        if not isinstance(version, int) or version < 1:
            raise PackageError("manifest.json sem schema_version válido.")
        if version > SCHEMA_VERSION:
            raise PackageError(
                f"Pacote de uma versão mais nova (schema {version}); "
                f"esta instalação entende até o schema {SCHEMA_VERSION}.")
        if data.get("product") != PRODUCT:
            raise PackageError(
                "Este pacote não foi gerado pelo WhatsBot-Lite. Pacotes de outros produtos "
                "(inclusive o WhatsBot Pro) têm outro formato e não podem ser importados aqui.")
        known = set(cls().__dict__)
        m = cls(**{k: v for k, v in data.items() if k in known})
        if not isinstance(m.files, dict):
            raise PackageError("manifest.json: 'files' inválido.")
        for path in m.files:
            if not is_safe_member_name(path):
                raise PackageError(f"manifest.json aponta para caminho inseguro: {path!r}")
        return m

# ── escrita ─────────────────────────────────────────────────────────────────

class PackageWriter:
    """Escreve o ``.zip`` em streaming e acumula SHA-256/tamanho por arquivo.

    Uso::

        with PackageWriter(path) as w:
            w.write_ndjson("contacts", rows)       # iterável de dicts
            w.write_bytes("media/x.jpg", data)     # ou w.copy_file(...)
            w.finish(manifest)                      # grava o manifest por ÚLTIMO

    O manifest vai por último de propósito: ele carrega os checksums de tudo que
    foi escrito antes. ``ZIP_DEFLATED`` com nível 6 — bom equilíbrio para NDJSON.
    """

    def __init__(self, path: str) -> None:
        self._zf = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                                   compresslevel=6, allowZip64=True)
        self.files: dict[str, dict[str, Any]] = {}
        self.counts: dict[str, int] = {}
        self._finished = False

    def __enter__(self) -> "PackageWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._zf.close()

    def _guard(self, name: str) -> None:
        if self._finished:
            raise PackageError("Pacote já finalizado.")
        if not is_safe_member_name(name) or name == MANIFEST_NAME:
            raise PackageError(f"Caminho inválido no pacote: {name!r}")
        if name in self.files:
            raise PackageError(f"Arquivo duplicado no pacote: {name!r}")

    def write_bytes(self, name: str, data: bytes) -> None:
        self._guard(name)
        self._zf.writestr(name, data)
        self.files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def write_ndjson(self, entity: str, rows: Iterable[dict]) -> int:
        """Grava ``data/<entity>.ndjson`` linha a linha. Devolve a contagem."""
        name = entity_path(entity)
        self._guard(name)
        h = hashlib.sha256()
        size = 0
        n = 0
        with self._zf.open(name, "w", force_zip64=True) as fh:
            for row in rows:
                line = (json.dumps(row, ensure_ascii=False, separators=(",", ":"),
                                   default=_json_default) + "\n").encode("utf-8")
                fh.write(line)
                h.update(line)
                size += len(line)
                n += 1
        self.files[name] = {"sha256": h.hexdigest(), "bytes": size}
        self.counts[entity] = n
        return n

    def write_plugin_table(self, plugin_id: str, table: str, rows: Iterable[dict]) -> int:
        """Grava ``plugin_data/<id>/<tabela>.ndjson``. Devolve a contagem."""
        name = plugin_data_path(plugin_id, table)
        self._guard(name)
        h = hashlib.sha256()
        size = n = 0
        with self._zf.open(name, "w", force_zip64=True) as fh:
            for row in rows:
                line = (json.dumps(row, ensure_ascii=False, separators=(",", ":"),
                                   default=_json_default) + "\n").encode("utf-8")
                fh.write(line)
                h.update(line)
                size += len(line)
                n += 1
        self.files[name] = {"sha256": h.hexdigest(), "bytes": size}
        return n

    def copy_file(self, name: str, src_path: str, chunk: int = 1024 * 1024,
                  *, stored: bool = False) -> None:
        """Copia um arquivo do disco para o pacote sem carregá-lo inteiro.

        ``stored=True`` grava SEM compressão: foto, áudio e vídeo já vêm comprimidos e o
        deflate só gastaria CPU sem ganho.
        """
        self._guard(name)
        h = hashlib.sha256()
        size = 0
        target: Any = name
        if stored:
            target = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
            target.compress_type = zipfile.ZIP_STORED
        with open(src_path, "rb") as src, self._zf.open(target, "w", force_zip64=True) as dst:
            while True:
                buf = src.read(chunk)
                if not buf:
                    break
                dst.write(buf)
                h.update(buf)
                size += len(buf)
        self.files[name] = {"sha256": h.hexdigest(), "bytes": size}

    def finish(self, manifest: Manifest) -> None:
        """Preenche ``files``/``counts`` no manifest e o grava (por último)."""
        manifest.files = dict(self.files)
        manifest.counts = {**manifest.counts, **self.counts}
        self._zf.writestr(MANIFEST_NAME, manifest.to_json())
        self._finished = True

def _json_default(o: Any) -> Any:
    # Decimal/datetime/UUID saem como string; bytes nunca devem chegar aqui.
    if isinstance(o, (bytes, bytearray, memoryview)):
        raise TypeError("bytes não são serializáveis em NDJSON")
    return str(o)

# ── leitura ─────────────────────────────────────────────────────────────────

def inspect_zip(path: str, *, max_entries: int = MAX_ENTRIES,
                max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
                max_ratio: float = MAX_RATIO,
                max_ndjson_ratio: float = MAX_NDJSON_RATIO) -> zipfile.ZipInfo | None:
    """Valida o ``.zip`` ANTES de ler qualquer conteúdo (zip-slip e zip-bomb).

    Confia no ``file_size`` do diretório central só como PRIMEIRA barreira — é
    declarado pelo próprio arquivo. A barreira real está em :func:`read_member`,
    que limita os bytes efetivamente descomprimidos.
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise PackageError("O arquivo não é um .zip válido.") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise PackageError(f"Pacote com entradas demais ({len(infos)}).")
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if not is_safe_member_name(name):
                raise PackageError(f"Caminho inseguro no pacote: {name!r}")
            # Bit 0xA000 no external_attr (>>16) = symlink unix.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise PackageError(f"Link simbólico no pacote: {name!r}")
            total += info.file_size
            if total > max_uncompressed:
                raise PackageError("Pacote descompactado excede o limite permitido.")
            limit = max_ndjson_ratio if name.endswith(".ndjson") else max_ratio
            if (info.file_size > _RATIO_FLOOR_BYTES
                    and info.compress_size > 0
                    and info.file_size / info.compress_size > limit):
                raise PackageError(f"Razão de compressão suspeita em {name!r}.")
        names = [i.filename for i in infos if not i.is_dir()]
        if len(names) != len(set(names)):
            raise PackageError("Pacote com nomes de arquivo duplicados.")
        if MANIFEST_NAME not in names:
            raise PackageError("Pacote sem manifest.json.")
    return None

class PackageReader:
    """Leitura de um pacote já validado por :func:`inspect_zip`."""

    def __init__(self, path: str) -> None:
        inspect_zip(path)
        self._zf = zipfile.ZipFile(path)
        self.manifest = Manifest.from_json(self.read_member(MANIFEST_NAME, limit=16 * 1024 * 1024))

    def __enter__(self) -> "PackageReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._zf.close()

    def names(self) -> list[str]:
        return [i.filename for i in self._zf.infolist() if not i.is_dir()]

    def has(self, name: str) -> bool:
        return name in set(self.names())

    def plugin_ids(self) -> list[str]:
        """Ids de plugin que o pacote traz (código OU dados), validados pelo formato do id."""
        out: list[str] = []
        for n in self.names():
            m = re.match(r"^plugins/([a-z][a-z0-9_]{0,31})\.zip$", n) or \
                re.match(r"^plugin_data/([a-z][a-z0-9_]{0,31})/", n)
            if m and m.group(1) not in out:
                out.append(m.group(1))
        return sorted(out)

    def plugin_tables(self, plugin_id: str) -> list[str]:
        """Nomes de tabela (sem o prefixo ``plugin_<id>_``) com dados no pacote."""
        pre = f"plugin_data/{plugin_id}/"
        return sorted(n[len(pre):-len(".ndjson")] for n in self.names()
                      if n.startswith(pre) and n.endswith(".ndjson")
                      and _NAME_RE.match(n[len(pre):-len(".ndjson")]))

    def read_member(self, name: str, *, limit: int = 512 * 1024 * 1024) -> bytes:
        """Lê um membro inteiro, abortando se passar de ``limit`` bytes REAIS."""
        out = bytearray()
        with self._zf.open(name) as fh:
            while True:
                buf = fh.read(1024 * 1024)
                if not buf:
                    break
                out += buf
                if len(out) > limit:
                    raise PackageError(f"{name!r} excede o limite de leitura.")
        return bytes(out)

    def open_member(self, name: str) -> IO[bytes]:
        """Fluxo de leitura de um membro (para copiar mídia para o disco sem carregá-la)."""
        return self._zf.open(name)

    def iter_ndjson(self, entity: str) -> Iterator[dict]:
        """Itera as linhas de ``data/<entity>.ndjson`` sem carregar o arquivo todo."""
        yield from self._iter_lines(entity_path(entity))

    def iter_plugin_table(self, plugin_id: str, table: str) -> Iterator[dict]:
        yield from self._iter_lines(plugin_data_path(plugin_id, table))

    def _iter_lines(self, name: str) -> Iterator[dict]:
        declared = self.manifest.files.get(name, {}).get("bytes")
        read = 0
        with self._zf.open(name) as fh:
            while True:
                raw = fh.readline(MAX_LINE_BYTES + 1)       # nunca uma linha inteira ilimitada
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES:
                    raise PackageError(f"Linha grande demais em {name!r}.")
                read += len(raw)
                if declared is not None and read > declared:
                    raise PackageError(f"{name!r} maior que o declarado no manifest.")
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise PackageError(f"Linha inválida em {name!r}.") from None
                if not isinstance(row, dict):
                    raise PackageError(f"Linha inválida em {name!r}.")
                yield row

    def verify_checksums(self) -> list[str]:
        """Confere SHA-256 e tamanho de tudo que o manifest declara.

        Devolve a lista de problemas (vazia = íntegro): arquivo declarado e
        ausente, arquivo presente e não declarado, hash ou tamanho divergente.
        """
        problems: list[str] = []
        present = set(self.names()) - {MANIFEST_NAME}
        declared = set(self.manifest.files)
        for missing in sorted(declared - present):
            problems.append(f"ausente: {missing}")
        for extra in sorted(present - declared):
            problems.append(f"não declarado: {extra}")
        for name in sorted(declared & present):
            meta = self.manifest.files[name]
            h = hashlib.sha256()
            size = 0
            with self._zf.open(name) as fh:
                while True:
                    buf = fh.read(1024 * 1024)
                    if not buf:
                        break
                    h.update(buf)
                    size += len(buf)
            if size != meta.get("bytes") or h.hexdigest() != meta.get("sha256"):
                problems.append(f"checksum divergente: {name}")
        return problems

def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    """SHA-256 de um arquivo em disco (hash do pacote, para detectar reimportação)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()
