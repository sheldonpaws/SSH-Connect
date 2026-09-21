"""
SSH Client для проекта "Собачья мудрость. Секрет счастья"
Простой и удобный SSH-клиент для подключения к VPS

Этот файл сохранён для обратной совместимости.
Основной код перемещён в src/
"""

import tkinter as tk
from src.gui import SSHApp


# === Запуск приложения ===
if __name__ == "__main__":
    root = tk.Tk()
    app = SSHApp(root)
    root.mainloop()
