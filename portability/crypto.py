"""Cifra do ``secrets.enc`` — scrypt + AES-256-GCM.

Layout do arquivo (tudo big-endian)::

    magic   6 bytes  b"WBSEC1"
    version 1 byte   1
    log2_n  1 byte   parâmetro N do scrypt (N = 2**log2_n)
    r       1 byte
    p       1 byte
    salt    16 bytes
    nonce   12 bytes
    ct      resto    AES-GCM(chave, nonce, plaintext, aad)

Os parâmetros do KDF ficam NO cabeçalho para poder evoluir sem quebrar pacote
antigo. O ``aad`` (o manifest.json, em bytes) entra na autenticação: adulterar o
manifest invalida a decifragem.

⚠️ Nunca grave senha ou chave em log, relatório, ``params`` de job ou auditoria.
⚠️ Senha errada e arquivo adulterado produzem o MESMO erro genérico
(:class:`SecretsDecryptError`) — não dê ao atacante um oráculo.
"""

from __future__ import annotations

import json
import os
import struct

MAGIC = b"WBSEC1"
VERSION = 1
MIN_PASSWORD_LEN = 12

# N=2**15, r=8, p=1 ⇒ ~32 MiB de memória, ~100 ms. Mesmo piso do OWASP.
DEFAULT_LOG2_N = 15
DEFAULT_R = 8
DEFAULT_P = 1
# Teto aceito ao LER: o cabeçalho vem de um arquivo não confiável, e um N enorme
# viraria negação de serviço (memória = 128 * N * r bytes).
MAX_LOG2_N = 20
MAX_R = 16
MAX_P = 4

_SALT_LEN = 16
_NONCE_LEN = 12
_HEADER = struct.Struct(">6sBBBB")  # magic, version, log2_n, r, p
_HEADER_LEN = _HEADER.size + _SALT_LEN + _NONCE_LEN

class SecretsError(Exception):
    """Base dos erros de cifra."""

class WeakPasswordError(SecretsError):
    """Senha curta demais (``MIN_PASSWORD_LEN``)."""

class SecretsFormatError(SecretsError):
    """Arquivo que não é um ``secrets.enc`` válido (magic/versão/parâmetros)."""

class CryptoUnavailableError(SecretsError):
    """A biblioteca ``cryptography`` não está instalada nesta instalação."""

def _backend():
    """Importa ``cryptography`` só quando preciso.

    O self-update do WhatsBot-Lite sobrescreve o código mas NÃO roda ``pip``: só os launchers e o
    Dockerfile instalam o ``requirements.txt``. Uma instalação recém-atualizada pode, portanto, ainda
    não ter a biblioteca. Importar no topo derrubaria o módulo inteiro (e o export/import SEM segredos,
    que não precisa dela). Aqui só quem cifra ou decifra paga o erro, e ele diz o que fazer."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise CryptoUnavailableError(
            "A biblioteca de criptografia (cryptography) não está instalada. Reinicie o WhatsBot-Lite "
            "pelo iniciador (linux_start.sh, macos_start.command ou windows_start.bat) para instalar as "
            "dependências, ou exporte sem incluir segredos.") from None
    return InvalidTag, AESGCM, Scrypt

class SecretsDecryptError(SecretsError):
    """Senha errada OU conteúdo/manifest adulterado — de propósito indistinguíveis."""

    def __init__(self) -> None:
        super().__init__("Não foi possível decifrar: senha incorreta ou pacote adulterado.")

def check_password(password: str) -> None:
    """Valida o tamanho mínimo. Levanta :class:`WeakPasswordError`."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise WeakPasswordError(
            f"A senha do pacote precisa ter pelo menos {MIN_PASSWORD_LEN} caracteres.")

def _derive_key(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    _, _, Scrypt = _backend()
    kdf = Scrypt(salt=salt, length=32, n=1 << log2_n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))

def encrypt(secrets: dict, password: str, aad: bytes = b"", *,
            log2_n: int = DEFAULT_LOG2_N, r: int = DEFAULT_R, p: int = DEFAULT_P) -> bytes:
    """Cifra ``secrets`` (JSON-serializável) e devolve o conteúdo do ``secrets.enc``."""
    check_password(password)
    if not (1 <= log2_n <= MAX_LOG2_N and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        raise SecretsFormatError("Parâmetros do scrypt fora do intervalo permitido.")
    _, AESGCM, _ = _backend()
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(password, salt, log2_n, r, p)
    plaintext = json.dumps(secrets, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return _HEADER.pack(MAGIC, VERSION, log2_n, r, p) + salt + nonce + ct

def decrypt(blob: bytes, password: str, aad: bytes = b"") -> dict:
    """Decifra um ``secrets.enc``. Erros: formato (:class:`SecretsFormatError`) ou
    senha/adulteração (:class:`SecretsDecryptError`, genérico)."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < _HEADER_LEN + 16:
        raise SecretsFormatError("Arquivo de segredos inválido.")
    magic, version, log2_n, r, p = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise SecretsFormatError("Arquivo de segredos inválido.")
    if version != VERSION:
        raise SecretsFormatError(f"Versão de segredos não suportada: {version}.")
    if not (1 <= log2_n <= MAX_LOG2_N and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        raise SecretsFormatError("Parâmetros do scrypt fora do intervalo permitido.")
    off = _HEADER.size
    salt = bytes(blob[off:off + _SALT_LEN])
    nonce = bytes(blob[off + _SALT_LEN:off + _SALT_LEN + _NONCE_LEN])
    ct = bytes(blob[_HEADER_LEN:])
    InvalidTag, AESGCM, _ = _backend()
    try:
        key = _derive_key(password or "", salt, log2_n, r, p)
        plaintext = AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag:
        raise SecretsDecryptError() from None
    try:
        data = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SecretsDecryptError() from None
    if not isinstance(data, dict):
        raise SecretsDecryptError()
    return data
