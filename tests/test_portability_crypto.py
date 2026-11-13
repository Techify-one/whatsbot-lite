"""Importar/Exportar — cifra do ``secrets.enc`` (``portability.crypto``).

scrypt + AES-256-GCM. O que importa provar: senha errada e pacote adulterado dão o MESMO erro (sem
oráculo), o cabeçalho vindo de arquivo hostil não vira negação de serviço, e a ausência da biblioteca
``cryptography`` (o self-update não roda ``pip``) dá uma mensagem acionável em vez de derrubar o módulo.

Os testes que cifram de verdade pulam quando ``cryptography`` não está instalada; o teste da mensagem de
ausência roda SEMPRE (ele simula a falta da biblioteca).

Roda direto (``python tests/test_portability_crypto.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import builtins
import struct
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from portability import crypto  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - depende do ambiente
    HAVE_CRYPTO = False

needs_crypto = pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography não instalada")

PW = "uma senha bem comprida 123"
# N=2**10 em vez de 2**15: o teste não precisa dos 32 MiB e ~100 ms por derivação do valor real.
FAST = {"log2_n": 10}

# ── não precisa da biblioteca ───────────────────────────────────────────────

def test_password_minimum_length():
    crypto.check_password("x" * crypto.MIN_PASSWORD_LEN)
    for bad in ("", "curta", "x" * (crypto.MIN_PASSWORD_LEN - 1), None, 123):
        with pytest.raises(crypto.WeakPasswordError):
            crypto.check_password(bad)  # type: ignore[arg-type]

def test_module_imports_without_cryptography(monkeypatch):
    """Importar o módulo NUNCA exige a biblioteca (export/import sem segredos não a usa)."""
    for name in [m for m in sys.modules if m == "portability.crypto" or m.startswith("cryptography")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("simulado: sem cryptography")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import importlib
    mod = importlib.import_module("portability.crypto")           # não levanta
    with pytest.raises(mod.CryptoUnavailableError, match="não está instalada") as ei:
        mod.encrypt({"a": 1}, PW)
    assert "iniciador" in str(ei.value)                           # diz o que fazer
    with pytest.raises(mod.CryptoUnavailableError):
        mod.decrypt(b"WBSEC1" + b"\x01\x0a\x08\x01" + b"\0" * 64, PW)

def test_weak_password_is_reported_before_missing_library(monkeypatch):
    """A senha curta é o erro útil; não esconda atrás de 'biblioteca ausente'."""
    with pytest.raises(crypto.WeakPasswordError):
        crypto.encrypt({"a": 1}, "curta")

@pytest.mark.parametrize("blob", [b"", b"x" * 10, b"WBSEC1", b"NOTMAG" + b"\0" * 80])
def test_garbage_blob_is_a_format_error(blob):
    with pytest.raises(crypto.SecretsFormatError):
        crypto.decrypt(blob, PW)

def test_header_with_hostile_kdf_params_is_refused_before_deriving():
    """O cabeçalho vem de arquivo não confiável: N enorme viraria negação de serviço (128*N*r bytes)."""
    header = struct.pack(">6sBBBB", crypto.MAGIC, crypto.VERSION, 30, 8, 1)   # N = 2**30
    blob = header + b"\0" * crypto._SALT_LEN + b"\0" * crypto._NONCE_LEN + b"\0" * 32
    with pytest.raises(crypto.SecretsFormatError, match="fora do intervalo"):
        crypto.decrypt(blob, PW)

def test_unknown_version_is_a_format_error():
    header = struct.pack(">6sBBBB", crypto.MAGIC, 9, 10, 8, 1)
    blob = header + b"\0" * (crypto._SALT_LEN + crypto._NONCE_LEN + 32)
    with pytest.raises(crypto.SecretsFormatError, match="Versão"):
        crypto.decrypt(blob, PW)

# ── cifra de verdade ────────────────────────────────────────────────────────

@needs_crypto
def test_roundtrip_preserves_unicode():
    secrets = {"config": {"openrouter_api_key": {"value": "sk-ação-123"}}}
    blob = crypto.encrypt(secrets, PW, b"aad", **FAST)
    assert blob.startswith(crypto.MAGIC)
    assert crypto.decrypt(blob, PW, b"aad") == secrets

@needs_crypto
def test_ciphertext_is_randomized():
    a = crypto.encrypt({"k": "v"}, PW, **FAST)
    b = crypto.encrypt({"k": "v"}, PW, **FAST)
    assert a != b                                                  # salt e nonce novos a cada vez

@needs_crypto
def test_plaintext_never_appears_in_blob():
    blob = crypto.encrypt({"k": "SEGREDO-VISIVEL"}, PW, **FAST)
    assert b"SEGREDO-VISIVEL" not in blob

@needs_crypto
def test_wrong_password_and_tampering_give_the_same_error():
    """Sem oráculo: quem ataca não distingue 'senha errada' de 'pacote adulterado'."""
    blob = crypto.encrypt({"k": "v"}, PW, b"aad", **FAST)
    with pytest.raises(crypto.SecretsDecryptError) as wrong_pw:
        crypto.decrypt(blob, "outra senha completamente errada", b"aad")
    flipped = bytearray(blob)
    flipped[-1] ^= 0x01
    with pytest.raises(crypto.SecretsDecryptError) as tampered:
        crypto.decrypt(bytes(flipped), PW, b"aad")
    with pytest.raises(crypto.SecretsDecryptError) as bad_aad:
        crypto.decrypt(blob, PW, b"outro-manifest")
    assert str(wrong_pw.value) == str(tampered.value) == str(bad_aad.value)

@needs_crypto
def test_kdf_params_live_in_the_header_so_they_can_evolve():
    blob = crypto.encrypt({"k": "v"}, PW, log2_n=11, r=8, p=1)
    assert blob[7] == 11                                           # log2_n no cabeçalho
    assert crypto.decrypt(blob, PW) == {"k": "v"}                  # lê com o que o cabeçalho diz

@needs_crypto
def test_encrypt_refuses_params_outside_range():
    with pytest.raises(crypto.SecretsFormatError):
        crypto.encrypt({"k": "v"}, PW, log2_n=crypto.MAX_LOG2_N + 1)

@needs_crypto
def test_non_object_payload_is_not_accepted_on_decrypt():
    """O plaintext cifrado precisa ser um objeto JSON; outra coisa é tratada como adulteração."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os
    salt, nonce = os.urandom(16), os.urandom(12)
    key = crypto._derive_key(PW, salt, 10, 8, 1)
    ct = AESGCM(key).encrypt(nonce, b"[1,2,3]", b"")
    blob = struct.pack(">6sBBBB", crypto.MAGIC, crypto.VERSION, 10, 8, 1) + salt + nonce + ct
    with pytest.raises(crypto.SecretsDecryptError):
        crypto.decrypt(blob, PW)

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
