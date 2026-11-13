"""Pacote de código de um plugin (``.zip``): validar, extrair e instalar/trocar a pasta.

Fonte ÚNICA das barreiras que valem para QUALQUER caminho que leve código de plugin para o disco: o
"Importar (.zip)" e o "Atualizar" da tela de plugins e o Importar/Exportar (``portability/``).

O zip é dado NÃO CONFIÁVEL e o que ele carrega passa a rodar neste servidor, então:

* **zip-slip**: caminho absoluto, ``..``, barra invertida, letra de unidade e NUL são recusados antes de
  extrair qualquer byte (``reject_unsafe_zip_paths``);
* **link simbólico** no zip é recusado (o ``zipfile`` os extrairia como arquivo comum, mas um plugin
  empacotado com link só serve para enganar quem lê);
* **zip-bomb**: o ``file_size`` do diretório central é declarado pelo próprio arquivo, então só filtra o
  óbvio. A barreira real é ``safe_extract``, que descomprime em blocos e CONTA os bytes efetivos, parando
  no teto — por arquivo e no total. Antes só o "Atualizar" tinha o teto declarado e o "Importar" extraía
  sem guarda nenhuma;
* a extração acontece numa pasta de rascunho (com ``.`` na frente, que o loader ignora) e só então é
  movida para o lugar: uma falha no meio nunca deixa um plugin pela metade visível.

Nada aqui toca no banco, nas ``plugin.<id>.*`` da config nem nas tabelas ``plugin_<id>_*``.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Teto do total DESCOMPRIMIDO de um plugin (anti zip-bomb). Código de plugin é pequeno.
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
#: Teto de um único arquivo dentro do zip.
MAX_MEMBER_BYTES = 128 * 1024 * 1024
#: Teto do próprio ``.zip`` enviado (compactado), lido em memória pela tela de plugins.
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 20_000

_CHUNK = 1024 * 1024
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# ── leitura e validação ─────────────────────────────────────────────────────

def read_zip_manifest(contents: bytes) -> tuple[zipfile.ZipFile, dict, str]:
    """Abre o zip de um plugin e devolve ``(zipfile, manifest, id)``.

    ``ValueError(<mensagem amigável>)`` em qualquer entrada malformada, para o chamador devolver ao
    usuário. Só LÊ o manifest (pequeno e limitado); não extrai nada."""
    if len(contents) > MAX_ZIP_BYTES:
        raise ValueError("arquivo .zip grande demais")
    try:
        zf = zipfile.ZipFile(io.BytesIO(contents))
    except zipfile.BadZipFile:
        raise ValueError("arquivo .zip inválido")

    names = zf.namelist()
    manifest_name = next((c for c in ("plugin.yaml", "plugin.yml", "plugin.json") if c in names), None)
    if manifest_name is None:
        raise ValueError("manifest (plugin.yaml/plugin.json) ausente na raiz do zip")
    info = zf.getinfo(manifest_name)
    if info.file_size > 1024 * 1024:
        raise ValueError("manifest grande demais")
    try:
        manifest_text = zf.read(manifest_name).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"falha ao ler manifest: {e}")

    if manifest_name.endswith(".json"):
        try:
            meta = json.loads(manifest_text)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"manifest JSON inválido: {e}")
    else:
        from plugins.manifest import _parse_yaml  # type: ignore
        try:
            meta = _parse_yaml(manifest_text) or {}
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"manifest YAML inválido: {e}")

    pid = meta.get("id") if isinstance(meta, dict) else None
    if not isinstance(pid, str) or not PLUGIN_ID_RE.match(pid):
        raise ValueError("manifest sem id válido")
    return zf, meta, pid

def reject_unsafe_zip_paths(zf: zipfile.ZipFile) -> None:
    """Recusa o zip INTEIRO se qualquer entrada for perigosa, antes de extrair um byte."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ValueError("zip com entradas demais")
    for member in infos:
        name = member.filename
        norm = name.replace("\\", "/")
        if (name.startswith("/") or "\\" in name or "\x00" in name or _DRIVE_RE.match(name)
                or ".." in norm.split("/")):
            raise ValueError(f"caminho perigoso no zip: {name}")
        # Bits 0o170000 do external_attr (>>16): 0o120000 = link simbólico unix.
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"link simbólico no zip: {name}")

def check_declared_size(zf: zipfile.ZipFile) -> int:
    """Soma o tamanho DECLARADO e recusa o que passa do teto. É só a primeira barreira (o arquivo
    declara o próprio tamanho): a real é ``safe_extract``."""
    total = 0
    for i in zf.infolist():
        size = getattr(i, "file_size", 0)
        if size > MAX_MEMBER_BYTES:
            raise ValueError(f"arquivo grande demais no zip: {i.filename} ({size} bytes)")
        total += size
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"zip grande demais: {total} bytes descomprimidos (limite {MAX_UNCOMPRESSED_BYTES})")
    return total

def version_key(version: str) -> tuple[int, ...]:
    """Chave numérica para comparar versões ``1.2.3`` (só os componentes inteiros do começo).

    Serve para apontar um downgrade, não é um gate de semver estrito."""
    raw = str(version or "0").strip()
    if raw[:1] in ("v", "V"):          # tolera prefixo "v" (ex.: "v1.2.3")
        raw = raw[1:]
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", raw):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (0,)

# ── extração ────────────────────────────────────────────────────────────────

class _Sink:
    """Destino que descarta (ensaio de extração)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, buf: bytes) -> int:
        return len(buf)

def safe_extract(zf: zipfile.ZipFile, dest: Path | None, *,
                 max_total: int | None = None,
                 max_member: int | None = None) -> int:
    """Extrai para ``dest`` contando os bytes REAIS descomprimidos. Devolve o total gravado.

    ``dest=None`` é o ensaio: percorre e CONTA do mesmo jeito, sem gravar nada. É o que o dry-run do
    Importar/Exportar usa, para a prévia recusar a mesma bomba que o import recusaria.

    ``ValueError`` ao passar do teto (por arquivo ou no total) — quem chama apaga ``dest``. Nunca usa
    ``extractall``: o nome de cada entrada é validado de novo e o destino final é conferido contra
    ``dest`` (defesa em profundidade além do ``reject_unsafe_zip_paths``)."""
    # Lidos AQUI, em tempo de chamada (não como valor padrão do argumento, que ficaria congelado na
    # definição da função): quem ajusta o teto em tempo de execução precisa ser obedecido.
    max_total = MAX_UNCOMPRESSED_BYTES if max_total is None else max_total
    max_member = MAX_MEMBER_BYTES if max_member is None else max_member
    reject_unsafe_zip_paths(zf)
    check_declared_size(zf)
    root = os.path.realpath(dest) if dest is not None else os.path.realpath(os.sep)
    total = 0
    for info in zf.infolist():
        if dest is not None:
            target = os.path.realpath(os.path.join(root, info.filename))
            if os.path.commonpath([root, target]) != root:
                raise ValueError(f"caminho perigoso no zip: {info.filename}")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
        elif info.is_dir():
            continue
        written = 0
        try:
            with zf.open(info) as src, (open(target, "wb") if dest is not None else _Sink()) as out:
                while True:
                    buf = src.read(_CHUNK)
                    if not buf:
                        break
                    written += len(buf)
                    total += len(buf)
                    if written > max_member:
                        raise ValueError(f"arquivo grande demais no zip: {info.filename}")
                    if total > max_total:
                        raise ValueError(
                            f"zip grande demais: mais de {max_total} bytes descomprimidos")
                    out.write(buf)
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            raise ValueError(f"falha ao extrair {info.filename}: {e}")
    return total

# ── instalar / trocar ───────────────────────────────────────────────────────

def install_plugin_dir(zf: zipfile.ZipFile, plugins_dir: Path, pid: str) -> Path:
    """Instala um plugin NOVO em ``plugins_dir/pid`` (que não pode existir).

    Extrai num rascunho e move para o lugar numa renomeação só: o loader nunca vê a pasta pela metade.
    ``ValueError`` (mensagem amigável) se já existe ou se o zip é inseguro/grande demais."""
    target = plugins_dir / pid
    if target.exists():
        raise ValueError(f"plugin '{pid}' já instalado — desinstale antes")
    staging = plugins_dir / f".{pid}.install-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        safe_extract(zf, staging)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target

def swap_plugin_dir(
    zf: zipfile.ZipFile, plugins_dir: Path, pid: str, *, keep_backup: bool = False,
) -> Path | None:
    """Troca ``plugins_dir/pid`` pelo conteúdo do zip, de forma quase atômica.

    Extrai num rascunho com ``.`` na frente (ignorado pelo loader), tira a pasta atual do caminho e
    põe o rascunho no lugar; se falhar, restaura a original. NUNCA toca nas tabelas, na config nem no
    histórico de migrations do plugin — só no código em disco."""
    target = plugins_dir / pid
    staging = plugins_dir / f".{pid}.update-staging"
    backup = plugins_dir / f".{pid}.update-backup"
    for tmp in (staging, backup):
        shutil.rmtree(tmp, ignore_errors=True)

    staging.mkdir(parents=True)
    try:
        safe_extract(zf, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Compatibilidade com plugins que guardavam arquivos do usuário DENTRO da pasta de código, antes de
    # ``plugins.context.plugin_data_dir`` existir. Plugin novo deve usar aquela pasta externa.
    for name in ("data", "uploads", "media", "storage", ".data"):
        source = target / name
        destination = staging / name
        if source.exists() and not destination.exists():
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)

    os.replace(target, backup)          # o código atual para o lado (mesmo fs → atômico)
    try:
        os.replace(staging, target)     # o novo no lugar
    except Exception:
        os.replace(backup, target)      # restaura a original se falhar
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if keep_backup:
        return backup
    shutil.rmtree(backup, ignore_errors=True)
    return None
