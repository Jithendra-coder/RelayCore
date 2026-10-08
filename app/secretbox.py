import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class SecretStorageError(Exception):
    pass


def encrypt_secret(value: str, key: bytes) -> str:
    return Fernet(key).encrypt(value.encode()).decode()


def decrypt_secret(value: str, key: bytes) -> str:
    try:
        return Fernet(key).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise SecretStorageError from exc


def credential_fingerprint(key: bytes, provider: str, name: str, allowed_host: str | None, secret: str) -> str:
    fingerprint_key = hashlib.sha256(key).digest()
    payload = f"{provider}\0{name}\0{allowed_host or ''}\0{secret}".encode()
    return hmac.new(fingerprint_key, payload, hashlib.sha256).hexdigest()
