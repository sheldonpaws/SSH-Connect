"""
Утилиты — вспомогательные функции
"""

import json
import os
from typing import Dict
from datetime import datetime

from src.encryption import encrypt_field, decrypt_field


def get_timestamp() -> str:
    """Получить текущее время в формате ЧЧ:ММ:СС"""
    return datetime.now().strftime("%H:%M:%S")


def load_connections(filepath: str) -> Dict:
    """Загрузка сохранённых подключений из JSON (с расшифровкой)"""
    if not os.path.exists(filepath):
        return {}
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Расшифровываем пароли и ключи
        decrypted_data = {}
        for profile, connection in data.items():
            decrypted_data[profile] = {
                "host": connection.get("host", ""),
                "port": connection.get("port", "22"),
                "username": connection.get("username", "root"),
                "password": decrypt_field(connection.get("password", "")),
                "key_file": decrypt_field(connection.get("key_file", ""))
            }
        
        return decrypted_data
    except Exception as e:
        print(f"Ошибка загрузки подключений: {e}")
        return {}


def save_connections(filepath: str, connections: Dict) -> bool:
    """Сохранение подключений в JSON (с шифрованием)"""
    try:
        # Шифруем пароли и ключи перед сохранением
        encrypted_data = {}
        for profile, connection in connections.items():
            encrypted_data[profile] = {
                "host": connection.get("host", ""),
                "port": connection.get("port", "22"),
                "username": connection.get("username", "root"),
                "password": encrypt_field(connection.get("password", "")),
                "key_file": encrypt_field(connection.get("key_file", ""))
            }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(encrypted_data, f, indent=2)
        return True
    except Exception as e:
        print(f"Ошибка сохранения подключений: {e}")
        return False


def format_remote_path(home_dir: str, filename: str) -> str:
    """Формирование полного пути на сервере"""
    return f"{home_dir}/{filename}"
