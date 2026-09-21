"""
Модуль шифрования для безопасного хранения паролей
"""

import base64
import os
from cryptography.fernet import Fernet


# Ключ хранится вне проекта — в профиле пользователя (AppData),
# чтобы не лежать рядом с зашифрованными подключениями.
KEY_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SSH-Connect")
KEY_FILE = os.path.join(KEY_DIR, ".encryption_key")

# Старое расположение (для миграции)
_OLD_KEY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".encryption_key"
)


def _migrate_old_key() -> None:
    """Перенести старый ключ из директории проекта в AppData."""
    if os.path.exists(_OLD_KEY_FILE) and not os.path.exists(KEY_FILE):
        try:
            os.makedirs(KEY_DIR, exist_ok=True)
            with open(_OLD_KEY_FILE, 'rb') as src, open(KEY_FILE, 'wb') as dst:
                dst.write(src.read())
            os.remove(_OLD_KEY_FILE)
        except Exception:
            pass


def get_or_create_key() -> bytes:
    """Получить или создать ключ шифрования"""
    _migrate_old_key()
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            return f.read()

    os.makedirs(KEY_DIR, exist_ok=True)
    key = Fernet.generate_key()
    with open(KEY_FILE, 'wb') as f:
        f.write(key)
    return key


def encrypt_password(password: str) -> str:
    """Шифрование пароля"""
    if not password:
        return ""

    key = get_or_create_key()
    f = Fernet(key)
    encrypted = f.encrypt(password.encode('utf-8'))
    return base64.urlsafe_b64encode(encrypted).decode('utf-8')


def decrypt_password(encrypted_password: str) -> str:
    """Расшифровка пароля"""
    if not encrypted_password:
        return ""

    try:
        key = get_or_create_key()
        f = Fernet(key)
        encrypted = base64.urlsafe_b64decode(encrypted_password.encode('utf-8'))
        decrypted = f.decrypt(encrypted)
        return decrypted.decode('utf-8')
    except Exception as e:
        print(f"Ошибка расшифровки: {e}")
        return ""


def encrypt_field(data: str) -> str:
    """Шифрование поля (пароль или ключ)"""
    return encrypt_password(data)


def decrypt_field(encrypted_data: str) -> str:
    """Расшифровка поля"""
    return decrypt_password(encrypted_data)
