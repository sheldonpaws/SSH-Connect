"""
SSH Client — главный файл запуска
"""

import os
import tkinter as tk
from src.gui import SSHApp
from src.paths import project_root


def main():
    """Точка входа приложения"""
    root = tk.Tk()

    # Иконка окна (в собранном exe favicon.ico лежит рядом с _internal)
    icon_path = os.path.join(project_root(), "favicon.ico")
    if os.path.exists(icon_path):
        root.iconbitmap(icon_path)

    app = SSHApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
