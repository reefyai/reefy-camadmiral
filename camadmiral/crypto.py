from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import SecretConfigurationError, read_secret_file, settings


def _decode_key(value: bytes) -> bytes:
    if len(value) == 32:
        return value
    text = value.decode("ascii", errors="strict")
    candidates = [text]
    if len(text) % 4:
        candidates.append(text + "=" * (4 - len(text) % 4))
    for candidate in candidates:
        try:
            decoded = base64.urlsafe_b64decode(candidate)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) == 32:
            return decoded
    try:
        decoded = bytes.fromhex(text)
    except ValueError:
        decoded = b""
    if len(decoded) == 32:
        return decoded
    raise SecretConfigurationError("CamAdmiral master key must contain exactly 32 random bytes")


def _decoded_key(value: bytes) -> bytes:
    try:
        return _decode_key(value)
    except UnicodeDecodeError as exc:
        raise SecretConfigurationError("CamAdmiral master key has an invalid format") from exc


def _generated_key_path(database: Path) -> Path:
    return database.parent / "secrets" / "master-key"


def _generate_master_key(path: Path) -> bytes:
    key = os.urandom(32)
    encoded = base64.urlsafe_b64encode(key) + b"\n"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = read_secret_file(path, required=True)
        assert value is not None
        return _decoded_key(value)
    except OSError as exc:
        raise SecretConfigurationError("Cannot create persistent CamAdmiral master key") from exc
    try:
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(encoded)
            key_file.flush()
            os.fsync(key_file.fileno())
    except OSError as exc:
        raise SecretConfigurationError("Cannot write persistent CamAdmiral master key") from exc
    return key


def load_master_key() -> bytes:
    configuration = settings()
    mounted = read_secret_file(
        configuration.secrets.master_key_file,
        required=configuration.secrets.master_key_file_explicit,
    )
    if mounted is not None:
        return _decoded_key(mounted)

    generated_path = _generated_key_path(configuration.storage.database)
    generated = read_secret_file(generated_path)
    if generated is not None:
        return _decoded_key(generated)
    if configuration.storage.database.exists():
        raise SecretConfigurationError(
            "CamAdmiral master key is missing for the existing database"
        )
    return _generate_master_key(generated_path)


def encrypt_password(password: str, credential_uuid: str, key: bytes) -> bytes:
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, password.encode("utf-8"), credential_uuid.encode("utf-8"))
    return b"\x01" + nonce + ciphertext


def decrypt_password(payload: bytes, credential_uuid: str, key: bytes) -> str:
    if len(payload) < 29 or payload[0] != 1:
        raise ValueError("Unsupported encrypted credential format")
    plaintext = AESGCM(key).decrypt(
        payload[1:13],
        payload[13:],
        credential_uuid.encode("utf-8"),
    )
    return plaintext.decode("utf-8")
