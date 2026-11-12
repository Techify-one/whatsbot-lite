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
section("GOWA 9.8+: sobe em loopback (sem APP_HOST ele aborta sem senha)")

import os  # noqa: E402
from unittest import mock  # noqa: E402
from gowa import manager as gowa_manager_mod  # noqa: E402


def _spawn_env(preset: dict[str, str] | None = None, drop: tuple[str, ...] = ("APP_HOST",)) -> dict:
    """Run GOWAManager.start() with Popen faked and return the env it got."""
    base = {k: v for k, v in os.environ.items() if k not in drop}
    base.update(preset or {})
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1234
        return proc

    with tempfile.TemporaryDirectory() as tmp:
        mgr = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tmp))
        fake_bin = Path(tmp) / "gowa"
        fake_bin.write_text("x")
        with mock.patch.dict(os.environ, base, clear=True), \
             mock.patch.object(gowa_manager_mod, "_get_gowa_binary", return_value=fake_bin), \
             mock.patch.object(mgr, "_ensure_runs_here", side_effect=lambda b: b), \
             mock.patch.object(gowa_manager_mod.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(gowa_manager_mod.threading, "Thread"):
            mgr.start()
    return seen.get("env", {})


check("sem APP_HOST do operador, o GOWA recebe 127.0.0.1",
      _spawn_env().get("APP_HOST") == "127.0.0.1")
check("APP_HOST do operador (ex.: Docker) prevalece",
      _spawn_env({"APP_HOST": "10.0.0.5"}).get("APP_HOST") == "10.0.0.5")
check("APP_HOST vazio conta como não definido",
      _spawn_env({"APP_HOST": "  "}).get("APP_HOST") == "127.0.0.1")
check("o cliente fala com o GOWA só por 127.0.0.1 (o bind combina)",
      "127.0.0.1" in gowa_client_mod.GOWAClient(port=3999).base_url
      if (gowa_client_mod := __import__("gowa.client", fromlist=["GOWAClient"])) else False)

# ═══════════════════════════════════════════════════════════════════
section("Binário novo que morre ao iniciar: rollback imediato e com motivo")

# Estado limpo: GOWA gerenciado 9.6.0 já instalado, tentando ir para 9.9.0.
shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
publish("9.9.0")


class DyingManager:
    """A 1a start() (a do binário novo) 'sobe' e morre, como o GOWA 9.8+ sem APP_HOST.

    O start() do rollback não morre.
    """

    def __init__(self):
        self.exit_count = 0
        self.last_exit_code: int | None = None
        self.starts = 0

    def stop(self):
        pass

    def start(self):
        self.starts += 1
        if self.starts == 1:
            self.last_exit_code = 1
            self.exit_count += 1


class DeadClient:
    """Sem resposta enquanto só o binário novo foi iniciado; responde após o rollback."""

    def __init__(self, mgr):
        self.mgr = mgr

    def health_check(self):
        return self.mgr.starts >= 2

    def get_app_info(self):
        return None


dying = DyingManager()
t0 = time.monotonic()
with mock.patch.object(gowa_updater, "_HEALTH_TIMEOUT_SEC", 45):
    res = gowa_updater.install("9.9.0", manager=dying, client=DeadClient(dying))
elapsed = time.monotonic() - t0
check("install falha e reverte quando o processo morre", res["ok"] is False and res["rolled_back"] is True, str(res))
check("o rollback religou o GOWA (2 starts)", dying.starts == 2, str(dying.starts))
check("não espera os 45s do health check", elapsed < 15, f"{elapsed:.1f}s")
check("a mensagem ao cliente é neutra, sem detalhe técnico",
      res.get("message") == gowa_updater._GENERIC_UPDATE_FAILURE
      and "gowa.log" not in res.get("message", "")
      and "DEBUG" not in res.get("message", ""),
      res.get("message", ""))

# Sem sinal de morte (MagicMock/atributos ausentes) o comportamento antigo se mantém.
check("_process_died ignora manager sem contador", gowa_updater._process_died(MagicMock(), 0) is False)
check("_process_died ignora exits_before desconhecido", gowa_updater._process_died(dying, None) is False)
check("_process_died detecta saída nova", gowa_updater._process_died(dying, dying.exit_count - 1) is True)

# ═══════════════════════════════════════════════════════════════════
section("Teste de fumaça: CPU antiga não executa o binário novo")

import subprocess as _sp  # noqa: E402


def _fake_run(returncode, stderr=b"", stdout=b""):
    return mock.patch.object(
        gowa_updater.subprocess, "run",
        return_value=_sp.CompletedProcess([], returncode, stdout=stdout, stderr=stderr),
    )


def _smoke(returncode, stderr=b""):
    try:
        with _fake_run(returncode, stderr):
            gowa_updater._smoke_test(Path("/x/gowa"))
        return None
    except gowa_updater.GowaUpdateError as exc:
        return str(exc)


msg = _smoke(127, b"./linux-amd64: CPU ISA level is lower than required\n")
check("CPU sem AVX2 é recusada antes de trocar o binário, com o aviso de processador antigo",
      msg == gowa_updater.CPU_UNSUPPORTED_MESSAGE, str(msg))
check("--help com código 1 e texto normal passa", _smoke(1, b"unknown flag") is None)
check("--help com código 0 passa", _smoke(0) is None)
check("loader 127 com mensagem é recusado, sem vazar o texto do sistema",
      _smoke(127, b"error while loading shared libraries") == gowa_updater._GENERIC_UPDATE_FAILURE)
check("127 sem texto não é recusado (comportamento antigo)", _smoke(127, b"") is None)
check("Linux/Mac: morto por SIGILL (-4) = CPU incompatível",
      _smoke(-4) == gowa_updater.CPU_UNSUPPORTED_MESSAGE, str(_smoke(-4)))
check("Windows: 0xC000001D (3221225501) = CPU incompatível",
      _smoke(3221225501) == gowa_updater.CPU_UNSUPPORTED_MESSAGE)
check("Windows: 0xC000001D como int negativo (-1073741795) = CPU incompatível",
      _smoke(-1073741795) == gowa_updater.CPU_UNSUPPORTED_MESSAGE)
check("SIGSEGV (-11) NÃO é tratado como CPU incompatível", _smoke(-11) is None)
check("a mensagem de CPU é em português e não técnica",
      "processador antigo" in gowa_updater.CPU_UNSUPPORTED_MESSAGE
      and "AVX" not in gowa_updater.CPU_UNSUPPORTED_MESSAGE)

# ═══════════════════════════════════════════════════════════════════
section("Plano B: oficial sem AVX2 -> binário da imagem oficial no GHCR (qualquer Linux amd64)")

import gzip as _gzip  # noqa: E402
import http.server as _http  # noqa: E402
import io as _io  # noqa: E402
import json as _json  # noqa: E402
import tarfile as _tarfile  # noqa: E402
import threading as _threading  # noqa: E402


def _tgz(members: dict) -> bytes:
    """tar.gz em memória. value bytes = arquivo regular; ("sym", alvo) = link simbólico."""
    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, value in members.items():
            info = _tarfile.TarInfo(name)
            if isinstance(value, tuple):
                info.type = _tarfile.SYMTYPE
                info.linkname = value[1]
                tf.addfile(info)
            else:
                info.size = len(value)
                info.mode = 0o755
                tf.addfile(info, _io.BytesIO(value))
    return buf.getvalue()


class FakeRegistry:
    """Registry OCI falso: /token, /v2/<repo>/manifests/<ref>, /v2/<repo>/blobs/<digest>.

    Os blobs respondem 307 para um segundo servidor (como o GHCR faz com
    pkg-containers.githubusercontent.com), que registra o Authorization recebido.
    """

    def __init__(self):
        self.images: dict[str, list[bytes]] = {}
        self.blobs: dict[str, bytes] = {}
        self.status: int | None = None   # força um código (404/429) em tudo
        self.fail_first: dict[str, int] = {}   # {trecho do caminho: nº de respostas 503 antes de funcionar}
        self.stall = False                      # aceita a conexão e nunca responde (timeout de leitura)
        self.requests: list[str] = []
        self.auth_seen_on_redirect: list = []
        self.tamper: set[str] = set()
        reg = self

        class Blob(_http.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                reg.auth_seen_on_redirect.append(self.headers.get("Authorization"))
                digest = self.path.rsplit("/", 1)[-1]
                body = reg.blobs.get(digest, b"")
                if digest in reg.tamper:
                    body = body[:-1] + bytes([body[-1] ^ 1])
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Api(_http.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, ctype="application/json"):
                data = _json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                reg.requests.append(self.path)
                for part, left in list(reg.fail_first.items()):
                    if part in self.path and left > 0:
                        reg.fail_first[part] = left - 1
                        self.send_response(503)
                        self.end_headers()
                        return
                if reg.stall:
                    import time as _t
                    _t.sleep(3)
                    return
                if reg.status:
                    self.send_response(reg.status)
                    self.end_headers()
                    return
                if self.path.startswith("/token"):
                    return self._json({"token": "tok-123"})
                if self.headers.get("Authorization") != "Bearer tok-123":
                    self.send_response(401)
                    self.end_headers()
                    return
                ref = self.path.rsplit("/", 1)[-1]
                if "/manifests/" in self.path and ref.startswith("v"):
                    if ref[1:] not in reg.images:
                        self.send_response(404)
                        self.end_headers()
                        return
                    return self._json({
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [
                            {"digest": "sha256:" + "0" * 64,
                             "platform": {"os": "unknown", "architecture": "unknown"}},
                            {"digest": reg._manifest_digest(ref[1:]),
                             "platform": {"os": "linux", "architecture": "amd64"}},
                        ],
                    }, "application/vnd.oci.image.index.v1+json")
                if "/manifests/sha256:" in self.path:
                    for version in reg.images:
                        if reg._manifest_digest(version) == ref:
                            return self._json({
                                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                "layers": [
                                    {"digest": d, "size": len(reg.blobs[d]),
                                     "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
                                    for d in reg._layer_digests(version)
                                ],
                            }, "application/vnd.oci.image.manifest.v1+json")
                    self.send_response(404)
                    self.end_headers()
                    return
                if "/blobs/" in self.path:
                    self.send_response(307)
                    self.send_header("Location", f"http://127.0.0.1:{reg.blob_port}/b/{ref}")
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

        self._blob_srv = _http.ThreadingHTTPServer(("127.0.0.1", 0), Blob)
        self._api_srv = _http.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        self.blob_port = self._blob_srv.server_port
        self.url = f"http://127.0.0.1:{self._api_srv.server_port}"
        for srv in (self._blob_srv, self._api_srv):
            _threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _manifest_digest(self, version):
        return "sha256:" + hashlib.sha256(f"manifest-{version}".encode()).hexdigest()

    def _layer_digests(self, version):
        return ["sha256:" + hashlib.sha256(b).hexdigest() for b in self.images[version]]

    def publish(self, version, layers: list[dict]):
        blobs = [_tgz(m) for m in layers]
        self.images[version] = blobs
        for b in blobs:
            self.blobs["sha256:" + hashlib.sha256(b).hexdigest()] = b

    def blob_requests(self):
        return [p for p in self.requests if "/blobs/" in p]

    def close(self):
        for srv in (self._blob_srv, self._api_srv):
            srv.shutdown()


LOADER_BYTES = b"#!/bin/sh\n# fake musl loader\nexec \"$@\"\n"


def ghcr_image(version, *, binary=True, loader=True, extra=None):
    """Camadas parecidas com as reais: alpine+loader, ffmpeg grande, binário (2x)."""
    bin_bytes = f"#!/bin/sh\nexit 0\n# ghcr {version}\n".encode()
    extra = os.urandom(200000) if extra is None else extra  # não comprime: é a maior camada
    layers = [{"lib/ld-musl-x86_64.so.1": LOADER_BYTES} if loader else {"etc/alpine": b"a"}]
    layers.append({"usr/bin/ffmpeg": extra})
    layers.append({"app/whatsapp": bin_bytes} if binary else {"app/other": b"o"})
    if binary:
        layers.append({"app/whatsapp": bin_bytes, "app/entrypoint.sh": b"#!/bin/sh\n"})
    return layers


REG = FakeRegistry()
gowa_updater._GHCR_RETRY_WAIT_SEC = (0.05, 0.05)
gowa_updater._GHCR_READ_TIMEOUT_SEC = 1
gowa_updater._GHCR_JSON_TIMEOUT_SEC = 1
REG_ENV = {"WHATSBOT_GOWA_REGISTRY": REG.url}


def _first_fails(rc=127, stderr=b"./linux-amd64: CPU ISA level is lower than required\n"):
    """1a chamada (oficial) = CPU incompatível; as seguintes (GHCR, pelo carregador) = ok."""
    calls = {"n": 0, "cmds": []}

    def run(cmd, *_a, **_k):
        calls["n"] += 1
        calls["cmds"].append([str(c) for c in cmd])
        if calls["n"] == 1:
            return _sp.CompletedProcess([], rc, stdout=b"", stderr=stderr)
        return _sp.CompletedProcess([], 0, stdout=b"usage", stderr=b"")
    return mock.patch.object(gowa_updater.subprocess, "run", side_effect=run), calls


# --- registry: padrão e override -------------------------------------
with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop("WHATSBOT_GOWA_REGISTRY", None)
    check("sem env: registry é o GHCR (nada a configurar)", gowa_updater.registry_base() == "https://ghcr.io")
for bad in ["ftp://x/y", "file:///opt/x", "/opt/x", "https://x/y?a=1", "javascript:x", "https://"]:
    with mock.patch.dict(os.environ, {"WHATSBOT_GOWA_REGISTRY": bad}):
        check(f"override inválido ignorado: {bad!r}", gowa_updater.registry_base() == "https://ghcr.io")
with mock.patch.dict(os.environ, {"WHATSBOT_GOWA_REGISTRY": "https://espelho.example/"}):
    check("override http(s) aceito", gowa_updater.registry_base() == "https://espelho.example")
check("a env antiga do compat não existe mais", not hasattr(gowa_updater, "compat_base_url"))

if (OS_, ARCH) == ("linux", "amd64"):
    REG.publish("9.9.0", ghcr_image("9.9.0"))
    publish("9.9.0")

    with mock.patch.dict(os.environ, REG_ENV):
        # --- caminho feliz ------------------------------------------
        shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
        REG.requests.clear()
        REG.auth_seen_on_redirect.clear()
        patcher, calls = _first_fails()
        with patcher:
            res = gowa_updater.install("9.9.0", manager=MagicMock(), client=client)
        meta = gowa_binary.read_meta()
        check("oficial falha por CPU -> GHCR instala", res["ok"] is True, str(res))
        check("meta grava flavor=ghcr", meta.get("flavor") == "ghcr", str(meta))
        check("meta grava checksum verificado (digest das camadas)", meta.get("checksum") == "verified", str(meta))
        check("meta grava a imagem de origem", meta.get("asset") == "ghcr.io/aldinokemal/go-whatsapp-web-multidevice:v9.9.0", str(meta))
        check("binário gerenciado é o da imagem", b"ghcr 9.9.0" in gowa_binary.managed_binary_path().read_bytes())
        check("carregador gravado em storages/bin", gowa_binary.loader_path().read_bytes() == LOADER_BYTES)
        check("smoke test do GHCR roda pelo carregador",
              len(calls["cmds"]) == 2 and calls["cmds"][1][0].endswith("ld-musl-x86_64.so.1")
              and calls["cmds"][1][-1] == "--help", str(calls["cmds"]))
        _ffmpeg = max(REG._layer_digests("9.9.0"), key=lambda d: len(REG.blobs[d]))
        check("a camada grande (ffmpeg) não foi baixada",
              REG.blob_requests() and not any(_ffmpeg in p for p in REG.blob_requests()),
              str(REG.blob_requests()))
        check("o token NÃO vai para o host do redirect dos blobs",
              REG.auth_seen_on_redirect and all(a is None for a in REG.auth_seen_on_redirect),
              str(REG.auth_seen_on_redirect))
        check("nada sobra em storages/bin/.tmp", not gowa_binary.tmp_dir().exists())

        # --- o manager inicia pelo carregador -----------------------
        musl_elf = Path(tempfile.mkdtemp()) / "gowa"
        with mock.patch.object(gowa_binary, "elf_interpreter", return_value="/lib/ld-musl-x86_64.so.1"):
            prefix = gowa_binary.launch_prefix(gowa_binary.managed_binary_path())
        check("binário musl + carregador presente -> inicia pelo carregador",
              prefix == [str(gowa_binary.loader_path())], str(prefix))
        with mock.patch.object(gowa_binary, "elf_interpreter", return_value="/lib64/ld-linux-x86-64.so.2"):
            check("binário glibc (oficial / compat antigo) -> inicia direto",
                  gowa_binary.launch_prefix(gowa_binary.managed_binary_path()) == [])

        # --- rollback depois de uma instalação ghcr -----------------
        publish("9.10.0")
        REG.publish("9.10.0", ghcr_image("9.10.0"))
        patcher, calls = _first_fails()
        with patcher:
            res = gowa_updater.install("9.10.0", manager=MagicMock(), client=client)
        check("segunda instalação ghcr (9.9.0 -> 9.10.0)", res["ok"] is True, str(res))
        res = gowa_updater.rollback(manager=MagicMock(), client=client)
        check("rollback depois de ghcr volta para 9.9.0",
              res["ok"] is True and gowa_binary.installed_version() == "9.9.0", str(res))
        check("rollback mantém o carregador (não entra no .bak)",
              gowa_binary.loader_path().read_bytes() == LOADER_BYTES)
        check("rollback restaurou o binário ghcr 9.9.0",
              b"ghcr 9.9.0" in gowa_binary.managed_binary_path().read_bytes())

        before = gowa_binary.managed_binary_path().read_bytes()

        def _net_fails(version, title):
            """Falha de rede: mensagem própria, SEM aviso de processador antigo, nada trocado."""
            patcher, _ = _first_fails()
            with patcher:
                r = gowa_updater.install(version, manager=MagicMock(), client=client)
            check(f"{title} -> aviso de rede (registry_unavailable), nada trocado",
                  r["ok"] is False and r["rolled_back"] is False
                  and r.get("error_code") == "registry_unavailable", str(r))
            check(f"{title} -> binário atual intacto",
                  gowa_binary.managed_binary_path().read_bytes() == before)
            return r

        def _fails_keeps(version, title):
            patcher, _ = _first_fails()
            with patcher:
                r = gowa_updater.install(version, manager=MagicMock(), client=client)
            check(f"{title} -> aviso de processador antigo, nada trocado",
                  r["ok"] is False and r["rolled_back"] is False
                  and r.get("error_code") == "cpu_unsupported", str(r))
            check(f"{title} -> binário atual intacto",
                  gowa_binary.managed_binary_path().read_bytes() == before)
            return r

        # --- camada adulterada --------------------------------------
        publish("9.11.0")
        REG.publish("9.11.0", ghcr_image("9.11.0"))
        REG.tamper = set(REG._layer_digests("9.11.0"))
        patcher, _ = _first_fails()
        with patcher:
            r = gowa_updater.install("9.11.0", manager=MagicMock(), client=client)
        REG.tamper = set()
        check("camada adulterada é recusada (mensagem de integridade)",
              r["ok"] is False and "checksum" in r["message"], str(r))
        check("camada adulterada -> binário atual intacto",
              gowa_binary.managed_binary_path().read_bytes() == before)

        # --- imagem sem o binário / sem o carregador ----------------
        publish("9.12.0")
        REG.publish("9.12.0", ghcr_image("9.12.0", binary=False))
        _fails_keeps("9.12.0", "imagem sem app/whatsapp")
        publish("9.13.0")
        REG.publish("9.13.0", ghcr_image("9.13.0", loader=False))
        _fails_keeps("9.13.0", "imagem sem o carregador musl")

        # --- tag inexistente, limite --------------------------------
        publish("9.14.0")
        _fails_keeps("9.14.0", "tag inexistente no GHCR (404)")
        # --- falhas de REDE: nunca viram "processador antigo" -------
        REG.status = 429
        REG.requests.clear()
        r = _net_fails("9.9.0", "limite do registry (429)")
        check("429: tentou 3 vezes antes de desistir", REG.requests.count("/token?scope=repository:aldinokemal/go-whatsapp-web-multidevice:pull") == 3, str(REG.requests))
        REG.status = None
        REG.stall = True
        _net_fails("9.9.0", "registry que não responde (timeout)")
        REG.stall = False
        _saved = gowa_updater.registry_base
        gowa_updater.registry_base = lambda: "http://127.0.0.1:9"
        try:
            _net_fails("9.9.0", "registry inacessível (conexão recusada)")
        finally:
            gowa_updater.registry_base = _saved
        check("falha de rede não grava a marca de CPU antiga (o código da resposta é outro)",
              r.get("error_code") == "registry_unavailable"
              and r["message"] == gowa_updater.REGISTRY_UNAVAILABLE_MESSAGE
              and "processador" not in r["message"], str(r))

        # --- uma falha passageira é recuperada pelas tentativas -----
        REG.fail_first = {"/blobs/": 2}
        REG.requests.clear()
        patcher, _ = _first_fails()
        with patcher:
            r = gowa_updater.install("9.9.0", manager=MagicMock(), client=client)
        REG.fail_first = {}
        check("503 passageiro nas camadas: as tentativas recuperam e a instalação conclui",
              r["ok"] is True and REG.requests.count(REG.requests[0]) >= 1, str(r))
        check("503 passageiro: refez o download (mais de 3 blobs pedidos)",
              len(REG.blob_requests()) > 3, str(REG.blob_requests()))
        before = gowa_binary.managed_binary_path().read_bytes()

        # --- membros perigosos --------------------------------------
        for title, layer in [
            ("app/whatsapp como link simbólico", {"app/whatsapp": ("sym", "/etc/passwd")}),
            ("carregador como link simbólico", {"lib/ld-musl-x86_64.so.1": ("sym", "../../x")}),
        ]:
            publish("9.15.0")
            REG.publish("9.15.0", [layer, {"app/whatsapp": b"#!/bin/sh\n", "lib/ld-musl-x86_64.so.1": b"x"}])
            _fails_keeps("9.15.0", title)
        publish("9.16.0")
        REG.publish("9.16.0", [{"../../evil": b"x", "/abs/evil": b"x"}] + ghcr_image("9.16.0"))
        patcher, _ = _first_fails()
        with patcher:
            r = gowa_updater.install("9.16.0", manager=MagicMock(), client=client)
        check("membros com '..' e caminho absoluto são ignorados (nunca extraídos)",
              r["ok"] is True and not (ROOT / "evil").exists()
              and not Path("/abs/evil").exists() and not (gowa_binary.managed_dir().parent.parent / "evil").exists(),
              str(r))

        # --- CPU nova: GHCR nunca consultado ------------------------
        shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
        REG.requests.clear()
        with _fake_run(0):
            res = gowa_updater.install("9.9.0", manager=MagicMock(), client=client)
        check("CPU nova usa o oficial (flavor=official)",
              res["ok"] and gowa_binary.read_meta().get("flavor") == "official", str(res))
        check("CPU nova: GHCR nunca consultado", REG.requests == [], str(REG.requests))
        check("CPU nova: nenhum carregador gravado", not gowa_binary.loader_path().exists())

        # --- cliente antigo com flavor=compat continua direto -------
        shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
        gowa_binary.managed_dir(create=True)
        gowa_binary.managed_binary_path().write_bytes(b"#!/bin/sh\nexit 0\n# compat\n")
        gowa_binary.write_meta({"version": "9.9.0", "flavor": "compat", "installed_at": time.time()})
        with mock.patch.object(gowa_binary, "elf_interpreter", return_value="/lib64/ld-linux-x86-64.so.2"):
            check("flavor=compat (glibc) inicia direto, sem carregador",
                  gowa_binary.launch_prefix(gowa_binary.managed_binary_path()) == [])
        check("flavor=compat continua sendo o binário resolvido",
              gowa_binary.resolve_binary()[1] == "managed" and gowa_binary.installed_version() == "9.9.0")

# Windows / Mac / Linux arm64 nunca consultam o GHCR
for plat in [("windows", "amd64"), ("darwin", "amd64"), ("darwin", "arm64"), ("linux", "arm64")]:
    shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
    asset = gowa_updater.asset_name("9.9.0", *plat)
    zpath = RELEASE / asset
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(gowa_updater.inner_binary_name(*plat), "#!/bin/sh\nexit 0\n")
    _checksums[asset] = hashlib.sha256(zpath.read_bytes()).hexdigest()
    rc = 3221225501 if plat[0] == "windows" else -4
    (RELEASE / gowa_updater.checksums_asset(plat[0])).write_text(
        "\n".join(f"{v}  {k}" for k, v in _checksums.items()) + "\n")
    patcher, calls = _first_fails(rc=rc, stderr=b"")
    REG.requests.clear()
    with mock.patch.dict(os.environ, REG_ENV), patcher, \
         mock.patch.object(gowa_updater, "detect_platform", return_value=plat):
        res = gowa_updater.install("9.9.0", manager=MagicMock(), client=client)
    check(f"{'/'.join(plat)}: CPU incompatível vira aviso (rc={rc})",
          res["ok"] is False and res.get("error_code") == "cpu_unsupported", str(res))
    check(f"{'/'.join(plat)}: GHCR nunca consultado", REG.requests == [], str(REG.requests))

# ═══════════════════════════════════════════════════════════════════
section("Boot: binário empacotado que não abre na CPU é corrigido antes de iniciar")

if (OS_, ARCH) == ("linux", "amd64"):
    with mock.patch.dict(os.environ, REG_ENV):
        os.environ.pop("WHATSBOT_GOWA_BINARY", None)
        shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
        REG.publish("8.8.0", ghcr_image("8.8.0"))  # bundled do teste = 8.8.0 (bin/gowa.version)
        bundled = gowa_binary.bundled_binary_path()

        def _boot_run(first_rc=127):
            """Oficial empacotado: CPU incompatível; qualquer outro: ok."""
            calls = {"cmds": []}

            def run(cmd, *_a, **_k):
                calls["cmds"].append([str(c) for c in cmd])
                if str(cmd[0]) == str(bundled):
                    return _sp.CompletedProcess([], first_rc, stdout=b"",
                                                stderr=b"CPU ISA level is lower than required\n")
                return _sp.CompletedProcess([], 0, stdout=b"usage", stderr=b"")
            return mock.patch.object(gowa_updater.subprocess, "run", side_effect=run), calls

        def _boot_start(mgr):
            spawned = {}

            def fake_popen(cmd, **kwargs):
                spawned["cmd"] = [str(c) for c in cmd]
                proc = MagicMock()
                proc.poll.return_value = None
                proc.pid = 4321
                return proc
            # threading do módulo trocado só ali: o registry falso precisa das threads reais
            with mock.patch.object(gowa_manager_mod.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(gowa_manager_mod, "threading"), \
                 mock.patch.object(gowa_binary, "elf_interpreter",
                                   side_effect=lambda p: "/lib/ld-musl-x86_64.so.1"
                                   if Path(p) == gowa_binary.managed_binary_path()
                                   else "/lib64/ld-linux-x86-64.so.2"):
                mgr.start()
            mgr._process = None
            return spawned.get("cmd", [])

        mgr = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
        REG.requests.clear()
        patcher, calls = _boot_run()
        with patcher:
            cmd = _boot_start(mgr)
        meta = gowa_binary.read_meta()
        check("boot: empacotado falha por CPU -> GHCR instalado em storages/bin",
              meta.get("flavor") == "ghcr" and meta.get("version") == "8.8.0"
              and meta.get("installed_by") == "boot", str(meta))
        check("boot: inicia pelo carregador o binário corrigido",
              cmd[:2] == [str(gowa_binary.loader_path()), str(gowa_binary.managed_binary_path())]
              and cmd[2] == "rest", str(cmd))
        n_blobs = len(REG.blob_requests())
        check("boot: baixou do GHCR uma vez", n_blobs > 0, str(REG.requests))

        # restart do watchdog / segundo start no mesmo processo: nada de novo
        patcher, calls2 = _boot_run()
        with patcher:
            cmd2 = _boot_start(mgr)
        check("boot: restart do watchdog não baixa de novo", len(REG.blob_requests()) == n_blobs)
        check("boot: restart nem repete o teste", calls2["cmds"] == [], str(calls2["cmds"]))

        # reiniciar o WhatsBot (manager novo): testa (1-2 s), não baixa
        mgr2 = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
        patcher, calls3 = _boot_run()
        with patcher:
            cmd3 = _boot_start(mgr2)
        check("boot: segundo start do WhatsBot NÃO baixa de novo", len(REG.blob_requests()) == n_blobs,
              str(REG.blob_requests()))
        check("boot: segundo start testa o binário corrigido pelo carregador",
              calls3["cmds"] and calls3["cmds"][0][0] == str(gowa_binary.loader_path()), str(calls3["cmds"]))
        check("boot: segundo start usa o mesmo binário corrigido", cmd3[:2] == cmd[:2], str(cmd3))

        # CPU nova: nada é baixado
        shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
        REG.requests.clear()
        mgr3 = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
        with _fake_run(0):
            cmd4 = _boot_start(mgr3)
        check("boot com CPU nova: GHCR nunca consultado", REG.requests == [], str(REG.requests))
        check("boot com CPU nova: inicia o empacotado direto", cmd4[:2] == [str(bundled), "rest"], str(cmd4))

        # sem plano B possível: aviso e nada iniciado (sem loop de restart)
        shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
        REG.status = 404
        mgr4 = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
        patcher, _ = _boot_run()
        raised = None
        with patcher:
            try:
                _boot_start(mgr4)
            except gowa_updater.GowaCpuIsaError as exc:
                raised = exc
        REG.status = None
        check("boot com tag inexistente (404): GowaCpuIsaError (sem plano B)", raised is not None)
        check("boot sem GHCR: nada iniciado, nada instalado",
              mgr4._process is None and not gowa_binary.managed_binary_path().exists())

        # rede fora do ar no boot: NÃO é processador antigo; nada iniciado; próximo start tenta de novo
        REG.status = 503
        mgr6 = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
        patcher, _ = _boot_run()
        raised = None
        with patcher:
            try:
                _boot_start(mgr6)
            except gowa_updater.GowaUpdateError as exc:
                raised = exc
        REG.status = None
        check("boot sem rede: GowaRegistryUnavailableError (NÃO é CPU antiga)",
              isinstance(raised, gowa_updater.GowaRegistryUnavailableError)
              and not isinstance(raised, gowa_updater.GowaCpuIsaError), repr(raised))
        check("boot sem rede: nada iniciado e o binário não é dado como checado",
              mgr6._process is None and not mgr6._cpu_checked)

        # versão desconhecida: não há tag para baixar
        sidecar = gowa_binary.bundled_version_file_path()
        saved = sidecar.read_text()
        sidecar.unlink()
        REG.requests.clear()
        mgr5 = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
        patcher, _ = _boot_run()
        raised = None
        with patcher:
            try:
                _boot_start(mgr5)
            except gowa_updater.GowaCpuIsaError as exc:
                raised = exc
        sidecar.write_text(saved)
        check("boot com versão desconhecida: aviso, GHCR nunca consultado",
              raised is not None and REG.requests == [], str(REG.requests))

        # durante uma instalação (lock ocupado) o boot não testa nem baixa
        gowa_updater._UPDATE_LOCK.acquire()
        try:
            REG.requests.clear()
            with _fake_run(127, b"CPU ISA level is lower than required"):
                check("boot com instalação em andamento: não interfere",
                      gowa_updater.repair_at_boot(bundled) is False and REG.requests == [])
        finally:
            gowa_updater._UPDATE_LOCK.release()

        # Windows/Mac: nunca
        with mock.patch.object(gowa_updater, "detect_platform", return_value=("windows", "amd64")), \
             _fake_run(3221225501):
            check("boot no Windows: nada a fazer", gowa_updater.repair_at_boot(bundled) is False)

REG.close()

# ═══════════════════════════════════════════════════════════════════
section("elf_interpreter: lê o carregador pedido pelo binário")

_tmp_elf = Path(tempfile.mkdtemp())
for real in [Path("/bin/ls"), Path(sys.executable)]:
    if real.exists() and sys.platform == "linux":
        interp = gowa_binary.elf_interpreter(real)
        check(f"{real.name}: interpretador glibc ou vazio (estático)",
              interp == "" or interp.startswith("/lib"), interp)
        break
(_tmp_elf / "txt").write_bytes(b"#!/bin/sh\n")
check("arquivo que não é ELF -> ''", gowa_binary.elf_interpreter(_tmp_elf / "txt") == "")
check("arquivo inexistente -> ''", gowa_binary.elf_interpreter(_tmp_elf / "nada") == "")
(_tmp_elf / "trunc").write_bytes(b"\x7fELF\x02\x01" + b"\0" * 10)
check("ELF truncado -> ''", gowa_binary.elf_interpreter(_tmp_elf / "trunc") == "")
shutil.rmtree(_tmp_elf, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════
section("GOWA novo que morre por instrução ilegal logo após subir: rollback + aviso de CPU")

shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
publish("9.9.0")


class SigillManager(DyingManager):
    def start(self):
        self.starts += 1
        if self.starts == 1:
            self.last_exit_code = -4
            self.exit_count += 1


sig_mgr = SigillManager()
with _fake_run(0), mock.patch.object(gowa_updater, "_HEALTH_TIMEOUT_SEC", 45):
    res = gowa_updater.install("9.9.0", manager=sig_mgr, client=DeadClient(sig_mgr))
check("SIGILL depois de subir: rollback feito", res["ok"] is False and res["rolled_back"] is True, str(res))
check("SIGILL depois de subir: aviso de processador antigo",
      res.get("error_code") == "cpu_unsupported"
      and res["message"] == gowa_updater.CPU_UNSUPPORTED_MESSAGE, str(res))

# ═══════════════════════════════════════════════════════════════════
section("Processador antigo constatado: para de oferecer atualização + botão da última compatível")


class FakeSettings(dict):
    pass


def _managed_at(version: str):
    gowa_binary.managed_dir().mkdir(parents=True, exist_ok=True)
    gowa_binary.managed_binary_path().write_bytes(b"#!/bin/sh\nexit 0\n")
    gowa_binary.write_meta({"version": version, "installed_at": time.time()})


st = FakeSettings()
check("sem marca: cpu_unsupported=False", gowa_updater.cpu_unsupported_info("9.6.0", "managed", st)["cpu_unsupported"] is False)
check("a última versão compatível é a 9.7.0", gowa_updater.LAST_CPU_COMPATIBLE_VERSION == "9.7.0")

gowa_updater.mark_cpu_unsupported(st, "9.9.0")
info = gowa_updater.cpu_unsupported_info("9.6.0", "managed", st)
check("com marca: cpu_unsupported=True e guarda a versão que falhou",
      info["cpu_unsupported"] is True and info["cpu_unsupported_version"] == "9.9.0", str(info))
check("instalada 9.6.0 (<9.7.0): oferece o botão da 9.7.0", info["cpu_fallback_version"] == "9.7.0", str(info))
check("a mensagem do aviso é a do texto novo (equipe Techify)",
      "contato com a equipe Techify" in info["cpu_message"] and "Veja os requisitos" not in info["cpu_message"])
check("instalada 9.7.0: NÃO oferece botão (já está na última compatível)",
      gowa_updater.cpu_unsupported_info("9.7.0", "managed", st)["cpu_fallback_version"] == "")
check("instalada 9.8.0 (de uma máquina que funcionou): NÃO oferece a 9.7.0 (seria downgrade)",
      gowa_updater.cpu_unsupported_info("9.8.0", "managed", st)["cpu_fallback_version"] == "")
check("versão instalada desconhecida: NÃO oferece botão",
      gowa_updater.cpu_unsupported_info("", "bundled", st)["cpu_fallback_version"] == "")
check("binário fixado por env: NÃO oferece botão",
      gowa_updater.cpu_unsupported_info("9.6.0", "env", st)["cpu_fallback_version"] == "")

# check(): nenhuma consulta ao GitHub, nenhum aviso de versão nova
_calls = {"n": 0}
_orig_fetch_latest = gowa_updater.fetch_latest_release


def _boom(*a, **k):
    _calls["n"] += 1
    raise AssertionError("não deveria consultar o GitHub com a CPU marcada")


gowa_updater.fetch_latest_release = _boom
try:
    res = gowa_updater.check(st, True)
finally:
    gowa_updater.fetch_latest_release = _orig_fetch_latest
check("check() com a CPU marcada não consulta o GitHub", _calls["n"] == 0)
check("check() com a CPU marcada: update_available=False e sem 'whatsapp_update_recommended'",
      res["update_available"] is False and res["whatsapp_update_recommended"] is False and res["latest_version"] == "", str(res))
check("check() com a CPU marcada devolve cpu_unsupported=True", res["cpu_unsupported"] is True)

# recomendação local (modal pós-atualização) também some
_rec_ok = gowa_updater.recommended_info("8.8.0", "managed", FakeSettings())
check("sem marca: recomendação local continua funcionando (sanidade)", _rec_ok["recommended_update_available"] in (True, False))
rec = gowa_updater.recommended_info("8.8.0", "managed", st)
check("com a marca: recomendação local nunca fica disponível", rec["recommended_update_available"] is False, str(rec))

# "Testar de novo" limpa a marca e volta a consultar
gowa_updater.clear_cpu_unsupported(st)
check("Testar de novo: marca limpa", gowa_updater.cpu_unsupported_info("9.6.0", "managed", st)["cpu_unsupported"] is False)
gowa_updater.fetch_latest_release = lambda force=False: {
    "version": "9.9.0", "url": "", "notes": "", "published_at": "", "rate_limited": False}
try:
    res = gowa_updater.check(st, True)
finally:
    gowa_updater.fetch_latest_release = _orig_fetch_latest
check("depois de Testar de novo: volta a consultar o GitHub", res["latest_version"] == "9.9.0", str(res))

# a 9.7.0 pode ser instalada normalmente quando a CPU é antiga (sem plano B)
shutil.rmtree(gowa_binary.managed_dir(), ignore_errors=True)
publish("9.7.0")
with _fake_run(0):
    res = gowa_updater.install("9.7.0", manager=MagicMock(), client=client)
check("o botão da 9.7.0 instala (binário baseline passa no smoke test)", res["ok"] is True, str(res))

# ═══════════════════════════════════════════════════════════════════
section("manager.stop(): TimeoutExpired depois do SIGKILL não vaza")


class _SlowProc:
    """terminate() ignorado e wait() sempre estoura, como um processo não reapado."""

    pid = 4242

    def __init__(self):
        self.killed = False
        self.waits = []

    def terminate(self):
        pass

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        raise _sp.TimeoutExpired(["gowa"], timeout)

    def poll(self):
        return None


mgr = gowa_manager_mod.GOWAManager(port=3999, data_dir=Path(tempfile.mkdtemp()))
slow = _SlowProc()
mgr._process = slow
mgr._running = True
try:
    mgr.stop()
    leaked = None
except Exception as exc:  # noqa: BLE001
    leaked = exc
check("stop() não levanta TimeoutExpired", leaked is None, repr(leaked))
check("stop() mandou SIGKILL depois do SIGTERM", slow.killed is True)
check("stop() usa os prazos novos (10s + 5s)",
      slow.waits == [gowa_manager_mod.GOWA_STOP_TIMEOUT_SEC, gowa_manager_mod.GOWA_KILL_TIMEOUT_SEC]
      and slow.waits == [10, 5], str(slow.waits))
check("stop() libera o processo", mgr._process is None and mgr._running is False)

# ═══════════════════════════════════════════════════════════════════
shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(RELEASE, ignore_errors=True)

print("\n" + "=" * 60)
print(f"  RESULTS: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
