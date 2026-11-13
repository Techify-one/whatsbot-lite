"""Importar/Exportar — formato do pacote (``portability.format``).

Módulo PURO: sem banco, sem rede. Cobre o que protege o servidor de um pacote hostil (zip-slip,
zip-bomb, linha gigante, link simbólico) e o que identifica o produto (um pacote de OUTRO produto,
inclusive o do WhatsBot Pro, é recusado em vez de lido pela metade).

Roda direto (``python tests/test_portability_format.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from portability import format as fmt  # noqa: E402
from portability.format import (  # noqa: E402
    Manifest, PackageError, PackageReader, PackageWriter, inspect_zip, is_safe_member_name,
)

def _tmp() -> str:
    return tempfile.mkdtemp(prefix="wb_port_fmt_")

def _build(rows: dict[str, list[dict]] | None = None, **manifest_kw) -> str:
    path = str(Path(_tmp()) / "p.zip")
    with PackageWriter(path) as w:
        for ent, items in (rows or {"tags": [{"name": "vip", "color": "#fff"}]}).items():
            w.write_ndjson(ent, items)
        w.finish(Manifest(scopes=["catalog"], **manifest_kw))
    return path

def _raw_zip(members: dict[str, bytes], *, manifest: dict | None = None) -> str:
    """Zip montado à mão (para fabricar o que o PackageWriter nunca geraria)."""
    path = str(Path(_tmp()) / "raw.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        if manifest is not None:
            zf.writestr("manifest.json", json.dumps(manifest))
    return path

# ── nomes de membro ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "data/tags.ndjson", "media/a1b2.jpg", "avatars/abc.jpg", "manifest.json"])
def test_safe_member_names_ok(name):
    assert is_safe_member_name(name)

@pytest.mark.parametrize("name", [
    "", "/etc/passwd", "../x", "a/../b", "a/./b", "a//b", "a\\b", "C:\\x", "C:/x",
    "data/\x00x", "./data/x"])
def test_unsafe_member_names_rejected(name):
    assert not is_safe_member_name(name)

# ── ida e volta ─────────────────────────────────────────────────────────────

def test_roundtrip_and_checksums():
    rows = [{"name": "vip", "color": "#fff"}, {"name": "ação", "color": "red"}]
    path = _build({"tags": rows})
    with PackageReader(path) as r:
        assert r.manifest.product == fmt.PRODUCT == "whatsbot-lite"
        assert list(r.iter_ndjson("tags")) == rows            # acento e ordem preservados
        assert r.manifest.counts["tags"] == 2
        assert r.verify_checksums() == []

def test_manifest_is_written_last_and_declares_every_file():
    path = _build({"tags": [{"name": "a", "color": "x"}], "contacts": []})
    names = zipfile.ZipFile(path).namelist()
    assert names[-1] == "manifest.json"
    with PackageReader(path) as r:
        assert set(r.manifest.files) == {"data/tags.ndjson", "data/contacts.ndjson"}

def test_tampered_member_fails_checksum():
    path = _build()
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    members["data/tags.ndjson"] = b'{"name":"hack","color":"x"}\n'
    bad = _raw_zip({k: v for k, v in members.items() if k != "manifest.json"},
                   manifest=json.loads(members["manifest.json"]))
    with PackageReader(bad) as r:
        problems = r.verify_checksums()
    assert any("checksum divergente" in p for p in problems)

def test_undeclared_and_missing_members_are_reported():
    path = _build()
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    extra = _raw_zip({"data/tags.ndjson": b"", "data/extra.ndjson": b"{}\n"}, manifest=manifest)
    with PackageReader(extra) as r:
        problems = r.verify_checksums()
    assert any(p.startswith("não declarado") for p in problems)

# ── produto e versão ────────────────────────────────────────────────────────

def test_package_without_product_is_refused():
    """O pacote do WhatsBot Pro não tem ``product``: tem de ser recusado com mensagem clara."""
    path = _raw_zip({}, manifest={"schema_version": 1, "files": {}})
    with pytest.raises(PackageError, match="não foi gerado pelo WhatsBot-Lite"):
        PackageReader(path)

def test_package_from_other_product_is_refused():
    path = _raw_zip({}, manifest={"schema_version": 1, "product": "whatsbot-pro", "files": {}})
    with pytest.raises(PackageError, match="outros produtos"):
        PackageReader(path)

def test_newer_schema_is_refused():
    path = _raw_zip({}, manifest={"schema_version": 99, "product": fmt.PRODUCT, "files": {}})
    with pytest.raises(PackageError, match="mais nova"):
        PackageReader(path)

def test_manifest_pointing_to_unsafe_path_is_refused():
    path = _raw_zip({}, manifest={"schema_version": 1, "product": fmt.PRODUCT,
                                  "files": {"../evil": {"sha256": "x", "bytes": 1}}})
    with pytest.raises(PackageError, match="inseguro"):
        PackageReader(path)

def test_aad_changes_when_product_or_scopes_change():
    a = Manifest(scopes=["catalog"], created_at=1.0, source_instance="x")
    b = Manifest(scopes=["settings"], created_at=1.0, source_instance="x")
    c = Manifest(scopes=["catalog"], created_at=1.0, source_instance="x")
    c.product = "outro"
    assert a.aad() != b.aad() and a.aad() != c.aad()

# ── zip hostil ──────────────────────────────────────────────────────────────

def test_not_a_zip_is_refused():
    p = Path(_tmp()) / "x.zip"
    p.write_bytes(b"isto nao e um zip")
    with pytest.raises(PackageError, match="não é um .zip válido"):
        inspect_zip(str(p))

def test_zip_slip_is_refused():
    path = _raw_zip({"data/../../etc/x": b"1"}, manifest={"schema_version": 1, "product": fmt.PRODUCT})
    with pytest.raises(PackageError, match="Caminho inseguro"):
        inspect_zip(path)

def test_symlink_member_is_refused():
    path = str(Path(_tmp()) / "ln.zip")
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("data/link")
        info.external_attr = (0o120777 << 16)             # S_IFLNK
        zf.writestr(info, "/etc/passwd")
        zf.writestr("manifest.json", "{}")
    with pytest.raises(PackageError, match="Link simbólico"):
        inspect_zip(path)

def test_missing_manifest_is_refused():
    with pytest.raises(PackageError, match="sem manifest"):
        inspect_zip(_raw_zip({"data/tags.ndjson": b""}))

def test_duplicate_members_are_refused():
    path = str(Path(_tmp()) / "dup.zip")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data/a.ndjson", "1")
        with pytest.warns(UserWarning):
            zf.writestr("data/a.ndjson", "2")
        zf.writestr("manifest.json", "{}")
    with pytest.raises(PackageError, match="duplicados"):
        inspect_zip(path)

def test_too_many_entries_is_refused():
    path = _raw_zip({f"data/f{i}.ndjson": b"" for i in range(5)}, manifest={})
    with pytest.raises(PackageError, match="entradas demais"):
        inspect_zip(path, max_entries=3)

def test_uncompressed_total_limit():
    path = _raw_zip({"data/a.ndjson": b"x" * 4096}, manifest={})
    with pytest.raises(PackageError, match="excede o limite"):
        inspect_zip(path, max_uncompressed=1024)

def test_compression_ratio_bomb_is_refused():
    """Zeros comprimem >1000x: acima do piso de 1 MB e da razão máxima, é bomba."""
    path = _raw_zip({"media/bomb.bin": b"\0" * (8 * 1024 * 1024)}, manifest={})
    with pytest.raises(PackageError, match="Razão de compressão"):
        inspect_zip(path)

def test_legit_ndjson_passes_higher_ratio_cap():
    """NDJSON repetitivo comprime dezenas de vezes legitimamente e não pode ser recusado.

    Medido: 50 mil avisos de sistema (texto fixo, ``ts`` sequencial) dão ~39x, acima do teto de 20x
    dos binários e longe do de 200x do NDJSON. Texto livre real comprime bem menos (~8x)."""
    lines = "".join(
        json.dumps({"role": "system_notice", "content": "Conversa transferida",
                    "ts": 1_700_000_000 + i, "status": None, "msg_id": None},
                   ensure_ascii=False) + "\n" for i in range(50_000))
    path = _raw_zip({"data/messages.ndjson": lines.encode()}, manifest={})
    info = zipfile.ZipFile(path).getinfo("data/messages.ndjson")
    ratio = info.file_size / info.compress_size
    # a premissa do teste: passa do teto dos binários e fica longe do do NDJSON
    assert fmt.MAX_RATIO < ratio < fmt.MAX_NDJSON_RATIO, ratio
    assert info.file_size > fmt._RATIO_FLOOR_BYTES
    inspect_zip(path)                                      # não levanta

def test_perfectly_repetitive_ndjson_is_still_flagged():
    """Linhas idênticas comprimem >200x: o guard continua pegando isso (é o que protege a memória)."""
    line = json.dumps({"role": "user", "content": "olá", "ts": 1.0}) + "\n"
    path = _raw_zip({"data/messages.ndjson": (line * 200_000).encode()}, manifest={})
    with pytest.raises(PackageError, match="Razão de compressão"):
        inspect_zip(path)

def test_huge_single_line_is_refused(monkeypatch):
    """O teto é por LINHA: um 'arquivo de uma linha só' não pode ser lido inteiro para a memória."""
    monkeypatch.setattr(fmt, "MAX_LINE_BYTES", 1024)
    path = _build({"tags": [{"name": "a" * 5000, "color": "x"}]})
    with PackageReader(path) as r:
        with pytest.raises(PackageError, match="Linha grande demais"):
            list(r.iter_ndjson("tags"))

def test_ndjson_longer_than_declared_is_refused():
    path = _build()
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest["files"]["data/tags.ndjson"]["bytes"] = 3        # mente: declara menos do que tem
    forged = _raw_zip({"data/tags.ndjson": b'{"name":"a","color":"b"}\n' * 5}, manifest=manifest)
    with PackageReader(forged) as r:
        with pytest.raises(PackageError, match="maior que o declarado"):
            list(r.iter_ndjson("tags"))

def test_invalid_json_line_is_refused():
    path = _raw_zip({"data/tags.ndjson": b"isto nao e json\n"},
                    manifest={"schema_version": 1, "product": fmt.PRODUCT, "files": {}})
    with PackageReader(path) as r:
        with pytest.raises(PackageError, match="Linha inválida"):
            list(r.iter_ndjson("tags"))

def test_read_member_enforces_real_byte_limit():
    path = _build()
    with PackageReader(path) as r:
        with pytest.raises(PackageError, match="excede o limite de leitura"):
            r.read_member("data/tags.ndjson", limit=2)

def test_entity_name_is_validated():
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(PackageError):
            fmt.entity_path(bad)
    assert fmt.entity_path("tags") == "data/tags.ndjson"

def test_writer_refuses_duplicate_and_manifest_overwrite():
    path = str(Path(_tmp()) / "w.zip")
    with PackageWriter(path) as w:
        w.write_bytes("media/a.bin", b"1")
        with pytest.raises(PackageError, match="duplicado"):
            w.write_bytes("media/a.bin", b"2")
        with pytest.raises(PackageError, match="inválido"):
            w.write_bytes("manifest.json", b"{}")

def test_copy_file_streams_and_records_hash(tmp_path):
    src = tmp_path / "foto.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"z" * 5000)
    path = str(tmp_path / "c.zip")
    with PackageWriter(path) as w:
        w.copy_file("media/foto.jpg", str(src), stored=True)
        w.finish(Manifest())
    with PackageReader(path) as r:
        assert r.read_member("media/foto.jpg") == src.read_bytes()
        assert r.verify_checksums() == []
        assert zipfile.ZipFile(path).getinfo("media/foto.jpg").compress_type == zipfile.ZIP_STORED

def test_sha256_file(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(b"abc")
    assert fmt.sha256_file(str(p)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
