"""
Окно удалённого терминала — отдельное окно с авторизацией
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
from src.ssh import SSHClient


class TerminalWindow:
    """Отдельное окно интерактивного терминала"""

    def __init__(self, hostname: str, port: int, username: str, password: str):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password

        self.root = tk.Tk()
        self.root.title(f"Терминал — {hostname}")
        self.root.geometry("900x600")

        self.ssh = SSHClient()
        self.command_history = []
        self.history_index = 0

        self._create_widgets()
        self._connect()

    def _create_widgets(self):
        """Создание элементов"""
        # Статус
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=10, pady=5)

        self.status_label = ttk.Label(status_frame, text="⚪ Подключение...", foreground="gray")
        self.status_label.pack(side=tk.LEFT)

        ttk.Button(status_frame, text="❌ Закрыть", command=self._close).pack(side=tk.RIGHT)

        # Терминал
        self.output_text = scrolledtext.ScrolledText(
            self.root, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font="Consolas 11"
        )
        self.output_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Цвета
        self.output_text.tag_configure("command", foreground="#4ec9b0")
        self.output_text.tag_configure("output", foreground="#d4d4d4")
        self.output_text.tag_configure("error", foreground="#f48771")
        self.output_text.tag_configure("success", foreground="#6a9955")
        self.output_text.tag_configure("info", foreground="#569cd6")

        # Ввод команд
        input_frame = ttk.Frame(self.root)
        input_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(input_frame, text="$", font="Consolas 11").pack(side=tk.LEFT, padx=5)
        self.command_entry = ttk.Entry(input_frame, font="Consolas 11", state=tk.DISABLED)
        self.command_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.command_entry.bind('<Return>', self._execute)
        self.command_entry.bind('<Up>', self._prev_command)
        self.command_entry.bind('<Down>', self._next_command)
        # Разрешаем вставку Ctrl+V
        self.command_entry.bind('<Control-v>', lambda e: None)
        self.command_entry.bind('<Control-V>', lambda e: None)

        ttk.Button(input_frame, text="▶", command=self._execute, width=3).pack(side=tk.LEFT, padx=5)

    def _log(self, message: str, tag: str = "output"):
        """Вывод в терминал"""
        self.output_text.insert(tk.END, message + "\n", tag)
        self.output_text.see(tk.END)

    def _connect(self):
        """Подключение"""
        def thread():
            success, message = self.ssh.connect(
                self.hostname, self.port, self.username, self.password
            )
            self.root.after(0, lambda: self._on_connect(success, message))

        threading.Thread(target=thread, daemon=True).start()

    def _on_connect(self, success: bool, message: str):
        """Результат подключения"""
        if success:
            self.status_label.config(text="🟢 Подключено", foreground="green")
            self.command_entry.config(state=tk.NORMAL)
            self.command_entry.focus()
            self._log(f"✅ Подключено к {self.hostname}", "success")
            self._log("Введите команду и нажмите Enter", "info")
            self._log("", "output")
        else:
            self.status_label.config(text="❌ Ошибка", foreground="red")
            self._log(f"❌ Ошибка подключения: {message}", "error")
            messagebox.showerror("Ошибка", f"Не удалось подключиться:\n{message}")

    def _execute(self, event=None):
        """Выполнение команды"""
        command = self.command_entry.get().strip()
        if not command:
            return

        if not self.ssh.is_connected:
            messagebox.showwarning("Внимание", "Нет подключения")
            return

        # Показываем команду
        self._log(f"$ {command}", "command")
        self.command_entry.delete(0, tk.END)

        # История
        self.command_history.append(command)
        self.history_index = len(self.command_history)

        # Выполнение
        def thread():
            success, output = self.ssh.execute_command(command)
            self.root.after(0, lambda: self._on_result(success, output))

        threading.Thread(target=thread, daemon=True).start()

    def _on_result(self, success: bool, output: str):
        """Результат команды"""
        if success:
            self._log(output, "output")
        else:
            self._log(output, "error")

    def _prev_command(self, event):
        """Предыдущая команда"""
        if self.history_index > 0:
            self.history_index -= 1
            self.command_entry.delete(0, tk.END)
            self.command_entry.insert(0, self.command_history[self.history_index])

    def _next_command(self, event):
        """Следующая команда"""
        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.command_entry.delete(0, tk.END)
            self.command_entry.insert(0, self.command_history[self.history_index])
        else:
            self.history_index = len(self.command_history)
            self.command_entry.delete(0, tk.END)

    def _close(self):
        """Закрытие"""
        self.ssh.disconnect()
        self.root.destroy()

    def run(self):
        """Запуск окна"""
        self.root.mainloop()


def open_terminal(hostname: str, port: int, username: str, password: str):
    """Открыть окно терминала"""
    window = TerminalWindow(hostname, port, username, password)
    window.run()
