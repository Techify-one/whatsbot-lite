"""GOWA updater tests.

Runs entirely offline: a fake release zip is fabricated on disk and served
through ``file://``, so download, checksum verification, binary swap, health
check and rollback are all exercised without touching the network.

    python tests/test_gowa_update.py
"""

import hashlib
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gowa import binary as gowa_binary  # noqa: E402
from gowa import updater as gowa_updater  # noqa: E402

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" -> {detail}" if detail else ""))


def section(title: str) -> None:
    print("\n" + "-" * 60)
    print(f"  {title}")
    print("-" * 60)


# ── Fake project root + fake release ───────────────────────────────────

ROOT = Path(tempfile.mkdtemp(prefix="whatsbot-gowa-test-"))
RELEASE = Path(tempfile.mkdtemp(prefix="whatsbot-gowa-release-"))
BUNDLED_BYTES = b"#!/bin/sh\nexit 0\n"

REAL_ROOT = gowa_binary.repo_root()  # captured before the fake root replaces it

(ROOT / "bin").mkdir()
(ROOT / "storages").mkdir()
# GOWA_VERSION is what the release RECOMMENDS; the installed (bundled) version
# comes from the file next to the binary. They differ on purpose, as they do
# after a self-update (which rewrites GOWA_VERSION but preserves bin/).
(ROOT / "GOWA_VERSION").write_text("9.6.0\n")
(ROOT / "bin" / (gowa_binary.binary_name() + ".version")).write_text("8.8.0\n")
(ROOT / "bin" / gowa_binary.binary_name()).write_bytes(BUNDLED_BYTES)
(ROOT / "bin" / gowa_binary.binary_name()).chmod(0o755)
(ROOT / "storages" / "whatsapp.db").write_bytes(b"session")

gowa_binary.repo_root = lambda: ROOT  # type: ignore[assignment]
# Keep the rollback path fast; the real 45s budget is a production concern.
gowa_updater._HEALTH_TIMEOUT_SEC = 3

_combo = gowa_updater.detect_platform()
if _combo is None:
    print("Plataforma sem binário GOWA publicado; nada a testar aqui.")
    sys.exit(0)
OS_, ARCH = _combo


def publish(version: str) -> str:
    """Write a fake release zip + checksums entry. Returns the zip sha256."""
    asset = gowa_updater.asset_name(version, OS_, ARCH)
    zpath = RELEASE / asset
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("readme.md", "docs")
        zf.writestr(gowa_updater.inner_binary_name(OS_, ARCH), f"#!/bin/sh\nexit 0\n# {version}\n")
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    _checksums[asset] = digest
    _write_checksums()
    return digest


_checksums: dict[str, str] = {}


def _write_checksums() -> None:
    lines = [f"{sha}  {name}" for name, sha in _checksums.items()]
    (RELEASE / gowa_updater.checksums_asset(OS_)).write_text("\n".join(lines) + "\n")


gowa_updater.GOWA_RELEASE_DOWNLOAD = "file://" + str(RELEASE) + "/{asset}"

manager = MagicMock()
client = MagicMock()
client.health_check.return_value = True
client.get_app_info.return_value = None


# ═══════════════════════════════════════════════════════════════════
section("Version parsing & supported range")

for value, expected in [
    ("9.1.0", (9, 1, 0)),
    ("v9.1.0", (9, 1, 0)),
    ("9.1.0-rc1", (9, 1, 0)),
    ("8.8", (8, 8, 0)),
    ("lixo", (0, 0, 0)),
    ("", (0, 0, 0)),
]:
    check(f"parse_version({value!r})", gowa_binary.parse_version(value) == expected,
          str(gowa_binary.parse_version(value)))

check("version_cmp 9.1.0 > 8.8.0", gowa_binary.version_cmp("9.1.0", "8.8.0") == 1)
check("version_cmp 8.8.0 == 8.8.0", gowa_binary.version_cmp("8.8.0", "8.8.0") == 0)
check("version_cmp 8.7.0 < 8.8.0", gowa_binary.version_cmp("8.7.0", "8.8.0") == -1)

for value, expected in [
    ("8.7.9", False), ("8.8.0", True), ("9.1.0", True),
    ("9.99.99", True), ("10.0.0", False), ("", False),
]:
    check(f"is_supported({value!r}) -> {expected}", gowa_updater.is_supported(value) is expected)


# ═══════════════════════════════════════════════════════════════════
section("Asset naming")

check("asset_name", gowa_updater.asset_name("9.1.0", "linux", "amd64") == "whatsapp_9.1.0_linux_amd64.zip")
check("inner name (linux)", gowa_updater.inner_binary_name("linux", "amd64") == "linux-amd64")
check("inner name (windows) tem .exe", gowa_updater.inner_binary_name("windows", "amd64") == "windows-amd64.exe")
check("checksums darwin é arquivo separado", gowa_updater.checksums_asset("darwin") == "checksums-macos.txt")
check("checksums linux", gowa_updater.checksums_asset("linux") == "checksums.txt")


# ═══════════════════════════════════════════════════════════════════
section("Release interval")

_saved_release_list_cache = dict(gowa_updater._release_list_cache)
gowa_updater._release_list_cache.update({
    "fetched_at": time.monotonic(),
    "etag": "test",
    "data": [
        {"tag_name": "v9.2.0", "body": "latest", "draft": False, "prerelease": False},
        {"tag_name": "v8.9.0", "body": "first", "draft": False, "prerelease": False},
        {"tag_name": "v9.0.0-rc1", "body": "rc", "draft": False, "prerelease": True},
        {"tag_name": "v10.0.0", "body": "future", "draft": False, "prerelease": False},
        {"tag_name": "v8.8.0", "body": "installed", "draft": False, "prerelease": False},
    ],
})
interval = gowa_updater.fetch_release_interval("8.8.0", "9.2.0")
check("interval includes all newer stable releases",
      [item["version"] for item in interval] == ["8.9.0", "9.2.0"])
check("interval keeps release notes", interval[0]["notes"] == "first")
check("extract whatsmeow pseudo-version revision",
      gowa_updater._extract_whatsmeow_revision(
          "require (\n  go.mau.fi/whatsmeow v0.0.0-20260816113502-fb386f152837\n)"
      ) == "fb386f152837")
gowa_updater._release_list_cache.clear()
gowa_updater._release_list_cache.update(_saved_release_list_cache)


# ═══════════════════════════════════════════════════════════════════
section("Binary resolution")

check("sem override -> bundled", gowa_binary.resolve_binary()[1] == "bundled")
check("versão bundled vem do arquivo ao lado do binário, não do GOWA_VERSION",
      gowa_binary.read_bundled_version() == "8.8.0")
check("recomendada vem do GOWA_VERSION", gowa_binary.read_recommended_version() == "9.6.0")


# ═══════════════════════════════════════════════════════════════════
section("Install — happy path")

publish("9.1.0")
result = gowa_updater.install("9.1.0", manager=manager, client=client)
check("ok", result["ok"], result.get("message", ""))
check("binário gravado em storages/bin", gowa_binary.managed_binary_path().exists())
check("bin/ intocado", (ROOT / "bin" / gowa_binary.binary_name()).read_bytes() == BUNDLED_BYTES)
check("meta.version", gowa_binary.read_meta().get("version") == "9.1.0")
check("checksum verificado", gowa_binary.read_meta().get("checksum") == "verified")
check("resolve -> managed", gowa_binary.resolve_binary()[1] == "managed")
check("previous_source registrado", gowa_binary.read_meta().get("previous_source") == "bundled")
check("snapshot da sessão em salto de major", bool(gowa_binary.read_meta().get("session_backup")))
check("staging limpo", not gowa_binary.tmp_dir().exists())
check("GOWA parado e religado", manager.stop.called and manager.start.called)


# ═══════════════════════════════════════════════════════════════════
section("Install — guards")

result = gowa_updater.install("10.5.0", manager=manager, client=client)
check("fora da faixa pede confirmação",
      result.get("requires_confirmation") is True and not result["ok"])

result = gowa_updater.install("../../etc/passwd", manager=manager, client=client)
check("versão malformada rejeitada", not result["ok"] and "inválida" in result["message"])

result = gowa_updater.install("9.1.0.0", manager=manager, client=client)
check("versão de 4 partes rejeitada", not result["ok"])

before = gowa_binary.managed_binary_path().read_bytes()
publish("9.1.1")
_checksums[gowa_updater.asset_name("9.1.1", OS_, ARCH)] = "0" * 64
_write_checksums()
result = gowa_updater.install("9.1.1", manager=manager, client=client)
check("checksum divergente aborta", not result["ok"], result["message"])
check("binário intacto após checksum ruim", gowa_binary.managed_binary_path().read_bytes() == before)
check("versão instalada continua 9.1.0", gowa_binary.read_meta().get("version") == "9.1.0")

gowa_updater._UPDATE_LOCK.acquire()
try:
    gowa_updater.install("9.1.0", manager=manager, client=client)
    check("update concorrente é recusado", False, "não levantou GowaUpdateBusy")
except gowa_updater.GowaUpdateBusy:
    check("update concorrente é recusado", True)
finally:
    gowa_updater._UPDATE_LOCK.release()


# ═══════════════════════════════════════════════════════════════════
section("Install — health check falha e reverte sozinho")

publish("9.2.0")
client.health_check.return_value = False
result = gowa_updater.install("9.2.0", manager=manager, client=client)
client.health_check.return_value = True
check("install falha", not result["ok"], result["message"])
check("rolled_back", result["rolled_back"] is True)
check("meta voltou para 9.1.0", gowa_binary.read_meta().get("version") == "9.1.0")
check("ainda usa o managed", gowa_binary.resolve_binary()[1] == "managed")


# ═══════════════════════════════════════════════════════════════════
section("Rollback manual")

result = gowa_updater.rollback(manager=manager, client=client)
check("ok", result["ok"], result.get("message", ""))
check("volta para o bundled", gowa_binary.resolve_binary()[1] == "bundled")
check("versão volta a 8.8.0", gowa_binary.installed_version() == "8.8.0")

result = gowa_updater.rollback(manager=manager, client=client)
check("rollback sem backup falha limpo", not result["ok"] and "anterior" in result["message"])


# ═══════════════════════════════════════════════════════════════════
section("Managed mais antigo que o bundled perde")

gowa_binary.managed_dir(create=True)
gowa_binary.managed_binary_path().write_bytes(b"velho")
gowa_binary.write_meta({"version": "8.0.0"})
check("bundled 8.8.0 ganha de managed 8.0.0", gowa_binary.resolve_binary()[1] == "bundled")

gowa_binary.write_meta({"version": "9.9.9"})
check("managed 9.9.9 ganha de bundled 8.8.0", gowa_binary.resolve_binary()[1] == "managed")

(ROOT / "bin" / ".gowa_stamp").write_text("10.0.0\n")
check("stamp tem precedência sobre o arquivo .version",
      gowa_binary.read_bundled_version() == "10.0.0")
check("bundled 10.0.0 ganha de managed 9.9.9", gowa_binary.resolve_binary()[1] == "bundled")
(ROOT / "bin" / ".gowa_stamp").unlink()


# ═══════════════════════════════════════════════════════════════════
section("Versão instalada: ordem stamp → hash → arquivo → desconhecida")

BIN = ROOT / "bin" / gowa_binary.binary_name()
SIDECAR = ROOT / "bin" / (gowa_binary.binary_name() + ".version")
STAMP = ROOT / "bin" / ".gowa_stamp"
for leftover in (gowa_binary.managed_binary_path(), gowa_binary.meta_path()):
    leftover.unlink(missing_ok=True)


def identify() -> tuple[str, str]:
    return gowa_binary.identify_bundled_version()


# 2. Arquivo ao lado do binário vira a versão instalada.
check("arquivo .version -> instalada", identify() == ("8.8.0", "version_file"), str(identify()))

# 3. Stamp e arquivo presentes: o stamp vence.
STAMP.write_text("8.5.0\n")
check("stamp vence o arquivo .version", identify() == ("8.5.0", "stamp"), str(identify()))
STAMP.unlink()

# 1. Sem stamp, sem arquivo, GOWA_VERSION=9.6.0 e hash desconhecido: desconhecida
#    (nunca 9.6.0 nem o baseline). É o cenário A do plano.
SIDECAR.unlink()
check("GOWA_VERSION=9.6.0 mas binário sem identificação -> desconhecida",
      identify() == ("", "unknown"), str(identify()))
check("read_bundled_version não cai em GOWA_VERSION", gowa_binary.read_bundled_version() == "")
check("desconhecida não conta como versão", not gowa_binary.is_known_version(""))
check("resolve_binary devolve versão vazia", gowa_binary.resolve_binary()[2] == "")
check("desconhecida perde de qualquer versão", gowa_binary.version_cmp("", "0.0.1") < 0)

# 4. Hash na tabela (binário falso + tabela monkeypatchada) -> versão da tabela.
_orig_hashes = dict(gowa_binary.KNOWN_BINARY_HASHES)
gowa_binary.KNOWN_BINARY_HASHES[hashlib.sha256(BUNDLED_BYTES).hexdigest()] = "8.5.0"
check("hash conhecido -> versão da tabela", identify() == ("8.5.0", "hash"), str(identify()))

# O hash é objetivo: ganha de um .version velho que sobrou de uma troca manual do .exe.
SIDECAR.write_text("9.0.0\n")
check("hash vence arquivo .version divergente", identify() == ("8.5.0", "hash"), str(identify()))
SIDECAR.write_text("8.8.0\n")

# Cache por (tamanho, mtime_ns): trocar o conteúdo invalida o hash.
BIN.write_bytes(b"#!/bin/sh\nexit 0\n# trocado\n")
check("conteúdo trocado invalida o cache do hash",
      identify() == ("8.8.0", "version_file"), str(identify()))
BIN.write_bytes(BUNDLED_BYTES)
check("restaurar o conteúdo volta a achar o hash", identify() == ("8.5.0", "hash"), str(identify()))

gowa_binary.KNOWN_BINARY_HASHES.clear()
gowa_binary.KNOWN_BINARY_HASHES.update(_orig_hashes)

# O hash só é recalculado quando o arquivo muda (resolve_binary roda o tempo todo).
_reads = {"n": 0}
_orig_open = Path.open


def _counting_open(self, *args, **kwargs):
    if self == BIN:
        _reads["n"] += 1
    return _orig_open(self, *args, **kwargs)


Path.open = _counting_open  # type: ignore[method-assign]
try:
    SIDECAR.unlink()
    for _ in range(5):
        gowa_binary.resolve_binary()
finally:
    Path.open = _orig_open  # type: ignore[method-assign]
check("hash cacheado: no máximo 1 leitura em 5 resoluções", _reads["n"] <= 1, str(_reads))
SIDECAR.write_text("8.8.0\n")

# Arquivo de versão ilegível/lixo não vira versão.
SIDECAR.write_text("lixo\n")
check("arquivo .version com lixo é ignorado", identify() == ("", "unknown"), str(identify()))
SIDECAR.write_text("v8.8.0\r\n")
check("arquivo .version aceita 'v' e CRLF", identify() == ("8.8.0", "version_file"), str(identify()))


# ═══════════════════════════════════════════════════════════════════
section("Cenário B: gerenciado 9.2.2 + bundled desconhecido não é descartado")

SIDECAR.unlink()
gowa_binary.managed_dir(create=True)
gowa_binary.managed_binary_path().write_bytes(b"gerenciado")
gowa_binary.write_meta({"version": "9.2.2"})
_, _src, _ver = gowa_binary.resolve_binary()
check("bundled desconhecido + GOWA_VERSION=9.6.0 -> usa o gerenciado", _src == "managed", _src)
check("versão é a do gerenciado (9.2.2)", _ver == "9.2.2", _ver)

gowa_binary.write_meta({"version": ""})
check("gerenciado sem versão válida não vence do bundled",
      gowa_binary.resolve_binary()[1] == "bundled")
gowa_binary.managed_binary_path().unlink()
gowa_binary.meta_path().unlink()
SIDECAR.write_text("8.8.0\n")


# ═══════════════════════════════════════════════════════════════════
section("Recomendada x instalada (somente local, sem rede)")

_settings: dict = {}


def reco(installed: str, source: str = "bundled") -> dict:
    return gowa_updater.recommended_info(installed, source, _settings)


r = reco("8.8.0")
check("9.6.0 recomendada > 8.8.0 instalada -> disponível", r["recommended_update_available"] is True)
check("campos: recomendada", r["recommended_version"] == "9.6.0")
check("campos: homologada", r["recommended_supported"] is True)
check("campos: versão instalada conhecida", r["installed_version_known"] is True)
check("campos: não adiada", r["recommended_skipped"] is False)
check("link da release da recomendada", r["recommended_release_url"].endswith("/tag/v9.6.0"))

r = reco("9.6.0")
check("instalada == recomendada -> nada a recomendar", r["recommended_update_available"] is False)
r = reco("9.7.0")
check("instalada mais nova que a recomendada -> nada a recomendar",
      r["recommended_update_available"] is False)

r = reco("")
check("instalada desconhecida -> recomendação disponível", r["recommended_update_available"] is True)
check("instalada desconhecida -> installed_version_known falso", r["installed_version_known"] is False)

r = reco("8.8.0", source="env")
check("WHATSBOT_GOWA_BINARY (env) nunca recebe recomendação",
      r["recommended_update_available"] is False)

_settings["gowa_recommended_skipped_version"] = "9.6.0"
check("'Agora não' em 9.6.0 -> recommended_skipped", reco("8.8.0")["recommended_skipped"] is True)
(ROOT / "GOWA_VERSION").write_text("9.7.0\n")
r = reco("8.8.0")
check("GOWA_VERSION muda para 9.7.0 -> volta a perguntar",
      r["recommended_skipped"] is False and r["recommended_version"] == "9.7.0")
(ROOT / "GOWA_VERSION").write_text("9.6.0\n")
_settings.clear()

(ROOT / "GOWA_VERSION").write_text("10.0.0\n")
check("recomendada fora da faixa homologada -> recommended_supported falso",
      reco("8.8.0")["recommended_supported"] is False)
(ROOT / "GOWA_VERSION").write_text("9.6.0\n")

(ROOT / "GOWA_VERSION").unlink()
r = reco("8.8.0")
check("sem GOWA_VERSION -> sem recomendação",
      r["recommended_version"] == "" and r["recommended_update_available"] is False)
(ROOT / "GOWA_VERSION").write_text("9.6.0\n")

# check() sem versão instalada: não consulta intervalo/analisador com tag vazia.
_orig_latest = gowa_updater.fetch_latest_release
_orig_interval = gowa_updater.fetch_release_interval
_calls = {"interval": 0}
gowa_updater.fetch_latest_release = lambda force=False: {
    "version": "9.6.0", "url": "", "notes": "", "published_at": "",
    "rate_limited": False, "cached": False,
}


def _interval_spy(*_a, **_k):
    _calls["interval"] += 1
    return []


gowa_updater.fetch_release_interval = _interval_spy
SIDECAR.unlink()
state = gowa_updater.check({})
check("check() com versão desconhecida: update_available", state["update_available"] is True)
check("check() expõe installed_version_known=False", state["installed_version_known"] is False)
check("check() não monta intervalo de releases com versão desconhecida", _calls["interval"] == 0)
gowa_updater.fetch_latest_release = _orig_latest
gowa_updater.fetch_release_interval = _orig_interval
SIDECAR.write_text("8.8.0\n")


# ═══════════════════════════════════════════════════════════════════
section("Proxy com versão desconhecida")

from gowa import proxy as gowa_proxy  # noqa: E402

check("supports_proxy('') é falso", gowa_proxy.supports_proxy("") is False)
SIDECAR.unlink()
check("supports_proxy() com binário não identificado é falso", gowa_proxy.supports_proxy() is False)
check("supports_proxy(8.11.0) continua verdadeiro", gowa_proxy.supports_proxy("8.11.0") is True)
SIDECAR.write_text("8.8.0\n")


# ═══════════════════════════════════════════════════════════════════
section("Cenário A: após o self-update, recomendada disponível e install() funciona")

# Estado pós-self-update: GOWA_VERSION novo, bin/ intocado e sem como se
# identificar (instalação antiga de Windows, sem .version e com hash fora da tabela).
SIDECAR.unlink()
(ROOT / "GOWA_VERSION").write_text("9.6.0\n")
r = reco(gowa_binary.installed_version())
check("recomendada disponível mesmo sem identificar o binário",
      r["recommended_update_available"] is True and r["installed_version_known"] is False)

publish("9.6.0")
result = gowa_updater.install(r["recommended_version"], manager=manager, client=client)
check("install(recomendada) funciona", result["ok"], result.get("message", ""))
check("gerenciado passa a valer", gowa_binary.resolve_binary()[1:] == ("managed", "9.6.0"),
      str(gowa_binary.resolve_binary()))
check("bin/ continua intacto", BIN.read_bytes() == BUNDLED_BYTES)
r = reco(gowa_binary.installed_version())
check("depois de instalar, nada mais a recomendar", r["recommended_update_available"] is False)
check("previous_version desconhecida não quebra o meta", "previous_version" in gowa_binary.read_meta())

# Rollback com bundled desconhecido: volta ao bin/, versão desconhecida.
result = gowa_updater.rollback(manager=manager, client=client)
check("rollback com bundled desconhecido funciona", result["ok"], result.get("message", ""))
check("após o rollback a versão volta a ser desconhecida", gowa_binary.installed_version() == "")
SIDECAR.write_text("8.8.0\n")


# ═══════════════════════════════════════════════════════════════════
section("Guardas de release (arquivos reais do repositório)")

REAL_EXE = REAL_ROOT / "bin" / "gowa.exe"
REAL_EXE_VERSION = REAL_ROOT / "bin" / "gowa.exe.version"
if REAL_EXE.is_file():
    real_digest = hashlib.sha256(REAL_EXE.read_bytes()).hexdigest()
    check("sha256 do bin/gowa.exe está na tabela",
          real_digest in gowa_binary.KNOWN_BINARY_HASHES, real_digest)
    check("bin/gowa.exe.version existe", REAL_EXE_VERSION.is_file())
    if REAL_EXE_VERSION.is_file():
        check("tabela[sha256] == bin/gowa.exe.version",
              gowa_binary.KNOWN_BINARY_HASHES.get(real_digest)
              == REAL_EXE_VERSION.read_text(encoding="utf-8").strip(),
              f"{gowa_binary.KNOWN_BINARY_HASHES.get(real_digest)} vs "
              f"{REAL_EXE_VERSION.read_text(encoding='utf-8').strip()}")
else:
    print("  (bin/gowa.exe ausente neste checkout; guarda do hash ignorada)")

_real_recommended = (REAL_ROOT / "GOWA_VERSION").read_text(encoding="utf-8").strip()
check("GOWA_VERSION está dentro de GOWA_SUPPORTED_RANGE",
      gowa_updater.is_supported(_real_recommended), _real_recommended)
check("tabela de hashes só tem versões x.y.z",
      all(gowa_updater._VERSION_RE.match(v) for v in gowa_binary.KNOWN_BINARY_HASHES.values()))
check("tabela de hashes só tem sha256 minúsculo de 64 hex",
      all(len(h) == 64 and h == h.lower() and set(h) <= set("0123456789abcdef")
          for h in gowa_binary.KNOWN_BINARY_HASHES))


# ═══════════════════════════════════════════════════════════════════
shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(RELEASE, ignore_errors=True)

print("\n" + "=" * 60)
print(f"  RESULTS: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
