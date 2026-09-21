"""
SSH Client — главный файл запуска
"""

import os
import tkinter as tk
from src.gui import SSHApp


def main():
    """Точка входа приложения"""
    root = tk.Tk()

    # Иконка окна
    icon_path = os.path.join(os.path.dirname(__file__), "favicon.ico")
    if os.path.exists(icon_path):
        root.iconbitmap(icon_path)

    app = SSHApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
