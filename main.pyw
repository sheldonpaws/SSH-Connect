"""
SSH Client — без консольного окна
"""
import os
import sys

# Перезапускаем через pythonw.exe, если запущено через python.exe
if sys.executable.endswith('python.exe'):
    pythonw = sys.executable.replace('python.exe', 'pythonw.exe')
    if os.path.exists(pythonw):
        import subprocess
        subprocess.Popen(
            [pythonw, __file__],
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        sys.exit()

import tkinter as tk
from src.gui import SSHApp

root = tk.Tk()

icon_path = os.path.join(os.path.dirname(__file__), "favicon.ico")
if os.path.exists(icon_path):
    root.iconbitmap(icon_path)

app = SSHApp(root)
root.mainloop()
