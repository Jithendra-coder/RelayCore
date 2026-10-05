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
