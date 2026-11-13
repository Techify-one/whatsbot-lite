"""``plugins/package.py``: as barreiras de zip de TODO caminho que leva código de plugin para o disco.

Cobre o que o ``server/routes/plugins.py`` fazia sozinho (e mal): o "Importar (.zip)" extraía com
``extractall`` e SEM teto de tamanho — um ``.zip`` de 300 KB virava 300 MB em disco — enquanto o
"Atualizar" só conferia o tamanho DECLARADO pelo próprio arquivo. Agora os dois usam ``safe_extract``,
que conta os bytes efetivamente descomprimidos.

Roda direto (``python tests/test_plugin_package.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from plugins import package  # noqa: E402

MANIFEST = "id: demo\nname: Demo\nversion: 1.0.0\n"

def _zip(files: dict, *, symlink: str | None = None) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))

@pytest.fixture()
def plugins_dir(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    return d

# ── zip-slip ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "../evil.py", "a/../../evil.py", "/etc/evil.py", "a\\..\\evil.py", "C:/evil.py", "c:evil.py",
    "a/b/../../../evil.py", "./../evil.py",
])
def test_unsafe_paths_are_refused_before_anything_is_written(plugins_dir, name):
    zf = _zip({"plugin.yaml": MANIFEST, name: "x"})
    with pytest.raises(ValueError):
        package.install_plugin_dir(zf, plugins_dir, "demo")
    assert list(plugins_dir.iterdir()) == []                  # nem a pasta de rascunho sobra
    assert not (plugins_dir.parent / "evil.py").exists()

def test_a_nul_in_a_name_never_reaches_the_disk(plugins_dir):
    """O ``zipfile`` já corta o nome no NUL ao ler (``x\\0.py`` vira ``x``): o nome que chega ao disco é
    o cortado, nunca um com NUL. A checagem de NUL em ``reject_unsafe_zip_paths`` fica como defesa."""
    zf = _zip({"plugin.yaml": MANIFEST, "x\x00.py": "1"})
    assert all("\x00" not in i.filename for i in zf.infolist())
    target = package.install_plugin_dir(zf, plugins_dir, "demo")
    assert all("\x00" not in p.name for p in target.rglob("*"))

def test_a_symlink_entry_is_refused(plugins_dir):
    zf = _zip({"plugin.yaml": MANIFEST}, symlink="link")
    with pytest.raises(ValueError, match="simbólico"):
        package.install_plugin_dir(zf, plugins_dir, "demo")
    assert list(plugins_dir.iterdir()) == []

def test_too_many_entries_are_refused(plugins_dir, monkeypatch):
    monkeypatch.setattr(package, "MAX_ENTRIES", 3)
    zf = _zip({"plugin.yaml": MANIFEST, "a": "1", "b": "2", "c": "3"})
    with pytest.raises(ValueError, match="entradas"):
        package.install_plugin_dir(zf, plugins_dir, "demo")

# ── zip-bomb ────────────────────────────────────────────────────────────────

def test_a_bomb_is_refused_and_leaves_nothing_on_disk(plugins_dir):
    """300 MB de zeros compactam para ~300 KB. Antes, o import extraía tudo."""
    zf = _zip({"plugin.yaml": MANIFEST, "lixo.bin": b"\0" * (300 * 1024 * 1024)})
    assert sum(i.compress_size for i in zf.infolist()) < 2 * 1024 * 1024        # é mesmo uma bomba
    with pytest.raises(ValueError, match="grande demais"):
        package.install_plugin_dir(zf, plugins_dir, "demo")
    assert list(plugins_dir.iterdir()) == []

def test_the_real_byte_count_stops_the_extraction_not_only_the_declared_size(plugins_dir, monkeypatch):
    """A barreira de verdade conta o que foi descomprimido: pára no teto mesmo sem olhar o declarado."""
    monkeypatch.setattr(package, "check_declared_size", lambda zf: 0)            # finge um zip que mente
    zf = _zip({"plugin.yaml": MANIFEST, "grande.bin": b"\0" * (8 * 1024 * 1024)})
    dest = plugins_dir / "x"
    dest.mkdir()
    with pytest.raises(ValueError, match="grande demais"):
        package.safe_extract(zf, dest, max_total=2 * 1024 * 1024, max_member=2 * 1024 * 1024)
    written = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    assert written <= 2 * 1024 * 1024 + package._CHUNK                           # parou no teto

def test_the_total_cap_counts_across_files(plugins_dir, monkeypatch):
    monkeypatch.setattr(package, "check_declared_size", lambda zf: 0)
    zf = _zip({"plugin.yaml": MANIFEST, **{f"f{i}.bin": b"\0" * (600 * 1024) for i in range(10)}})
    dest = plugins_dir / "x"
    dest.mkdir()
    with pytest.raises(ValueError, match="grande demais"):
        package.safe_extract(zf, dest, max_total=2 * 1024 * 1024, max_member=1024 * 1024)

def test_the_per_file_cap(plugins_dir):
    zf = _zip({"plugin.yaml": MANIFEST, "um.bin": b"\0" * (3 * 1024 * 1024)})
    dest = plugins_dir / "x"
    dest.mkdir()
    with pytest.raises(ValueError, match="grande demais"):
        package.safe_extract(zf, dest, max_total=50 * 1024 * 1024, max_member=1024 * 1024)

def test_an_oversized_zip_upload_is_refused_before_it_is_opened(monkeypatch):
    monkeypatch.setattr(package, "MAX_ZIP_BYTES", 10)
    with pytest.raises(ValueError, match="grande demais"):
        package.read_zip_manifest(b"x" * 11)

# ── instalar / trocar ───────────────────────────────────────────────────────

def test_install_puts_the_code_in_place_in_one_step(plugins_dir):
    zf = _zip({"plugin.yaml": MANIFEST, "tools.py": "X = 1\n", "migrations/001_a.sql": "SELECT 1;"})
    target = package.install_plugin_dir(zf, plugins_dir, "demo")
    assert (target / "tools.py").read_text() == "X = 1\n"
    assert (target / "migrations" / "001_a.sql").is_file()
    assert sorted(p.name for p in plugins_dir.iterdir()) == ["demo"]        # sem rascunho

def test_install_refuses_an_existing_plugin(plugins_dir):
    (plugins_dir / "demo").mkdir()
    with pytest.raises(ValueError, match="já instalado"):
        package.install_plugin_dir(_zip({"plugin.yaml": MANIFEST}), plugins_dir, "demo")

def test_a_failed_install_does_not_leave_a_half_plugin(plugins_dir):
    zf = _zip({"plugin.yaml": MANIFEST, "../x": "1"})
    with pytest.raises(ValueError):
        package.install_plugin_dir(zf, plugins_dir, "demo")
    assert not (plugins_dir / "demo").exists()

def test_swap_keeps_user_dirs_and_can_keep_a_backup(plugins_dir):
    old = plugins_dir / "demo"
    (old / "uploads").mkdir(parents=True)
    (old / "uploads" / "u.txt").write_text("meu")
    (old / "tools.py").write_text("V = 1\n")
    zf = _zip({"plugin.yaml": MANIFEST, "tools.py": "V = 2\n"})
    backup = package.swap_plugin_dir(zf, plugins_dir, "demo", keep_backup=True)
    assert (plugins_dir / "demo" / "tools.py").read_text() == "V = 2\n"
    assert (plugins_dir / "demo" / "uploads" / "u.txt").read_text() == "meu"
    assert backup is not None and (backup / "tools.py").read_text() == "V = 1\n"

def test_swap_with_a_bomb_leaves_the_old_code_untouched(plugins_dir):
    old = plugins_dir / "demo"
    old.mkdir()
    (old / "tools.py").write_text("V = 1\n")
    zf = _zip({"plugin.yaml": MANIFEST, "lixo.bin": b"\0" * (300 * 1024 * 1024)})
    with pytest.raises(ValueError):
        package.swap_plugin_dir(zf, plugins_dir, "demo")
    assert (old / "tools.py").read_text() == "V = 1\n"
    assert sorted(p.name for p in plugins_dir.iterdir()) == ["demo"]

# ── a rota "Importar (.zip)" usa as mesmas barreiras ────────────────────────

def test_the_import_route_no_longer_extracts_without_a_cap():
    src = (_ROOT / "server" / "routes" / "plugins.py").read_text(encoding="utf-8")
    assert "extractall" not in src, "a rota voltou a extrair sem o guarda de zip-bomb"
    assert "install_plugin_dir" in src and "swap_plugin_dir" in src

def test_version_key_orders_versions():
    k = package.version_key
    assert k("2.0.0") > k("1.9.9") and k("1.0.10") > k("1.0.2") and k("v2.0.0") == k("2.0.0")
    assert k("1.2.0-rc1") == k("1.2.0") and k("lixo") == (0,)

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
