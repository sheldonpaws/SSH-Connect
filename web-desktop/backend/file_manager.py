"""
Файловый менеджер для веб-десктопа
Операции с файлами через SFTP
"""

import os
import shlex
import stat
from typing import Iterator, Optional


class FileManager:
    """Управление файлами на сервере через SFTP"""

    def __init__(self, ssh_session):
        """Принимает подключённый paramiko SSH client"""
        self.ssh_client = ssh_session.client

    def list_directory(self, path: str = "/") -> tuple[bool, list[dict], str]:
        """Список файлов и папок"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                entries = []

                for entry in sftp.listdir_attr(path):
                    is_dir = self._is_dir(entry)
                    entries.append({
                        "name": entry.filename,
                        "is_dir": is_dir,
                        "size": entry.st_size if not is_dir else 0,
                        "modified": entry.st_mtime,
                        "permissions": entry.st_mode
                    })

                # Сортировка: папки primero, затем по имени
                entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

            return True, entries, ""

        except Exception as e:
            return False, [], str(e)

    def read_file(self, path: str, max_size: int = 1024 * 1024) -> tuple[bool, str, str]:
        """Чтение текстового файла"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                # Проверяем размер
                file_attr = sftp.stat(path)
                if file_attr.st_size > max_size:
                    return False, "", f"Файл слишком большой ({file_attr.st_size / 1024:.0f} KB)"

                with sftp.open(path, 'r') as f:
                    content = f.read()

            return True, content, ""

        except Exception as e:
            return False, "", str(e)

    def save_file(self, path: str, content: str) -> tuple[bool, str]:
        """Сохранение файла"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                with sftp.open(path, 'w') as f:
                    f.write(content)
            return True, "Сохранено"

        except Exception as e:
            return False, str(e)

    def delete(self, path: str) -> tuple[bool, str]:
        """Удаление файла или папки"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                # Проверяем, папка или файл
                try:
                    sftp.stat(path + "/")
                    # Это папка — удаляем рекурсивно
                    self._rmtree(sftp, path)
                except IOError:
                    # Это файл
                    sftp.remove(path)
            return True, "Удалено"

        except Exception as e:
            return False, str(e)

    def rename(self, old_path: str, new_path: str) -> tuple[bool, str]:
        """Переименование"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                sftp.rename(old_path, new_path)
            return True, "Переименовано"
        except Exception as e:
            return False, str(e)

    def mkdir(self, path: str) -> tuple[bool, str]:
        """Создание папки"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                sftp.mkdir(path)
            return True, "Папка созданана"
        except Exception as e:
            return False, str(e)

    def download_info(self, path: str) -> tuple[bool, dict, str]:
        """Информация о файле"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                attr = sftp.stat(path)
                info = {
                    "name": os.path.basename(path),
                    "size": attr.st_size,
                    "modified": attr.st_mtime,
                    "permissions": attr.st_mode,
                    "is_dir": self._is_dir(attr)
                }
            return True, info, ""
        except Exception as e:
            return False, {}, str(e)

    def stat_path(self, path: str) -> tuple[bool, bool, int]:
        """(существует, это папка, размер) для пути на сервере"""
        try:
            with self.ssh_client.open_sftp() as sftp:
                attr = sftp.stat(path)
            return True, stat.S_ISDIR(attr.st_mode), attr.st_size
        except Exception:
            return False, False, 0

    def iter_download(self, path: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Потоковое бинарное чтение файла с сервера (без лимита размера).

        Генератор держит SFTP-канал открытым всё время стриминга,
        поэтому используется только внутри StreamingResponse.
        """
        with self.ssh_client.open_sftp() as sftp:
            with sftp.open(path, 'rb') as f:
                f.prefetch()
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

    def iter_tar(self, parent: str, name: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Потоковое tar.gz содержимое папки (`tar -czf -` на сервере).

        Архив собирается на стороне сервера и идёт в браузер потоком —
        большие папки не занимают память ни сервера API, ни клиента.
        """
        command = f"tar -czf - -C {shlex.quote(parent)} {shlex.quote(name)}"
        stdin, stdout, stderr = self.ssh_client.exec_command(command, timeout=600)
        try:
            stdin.close()
            while True:
                chunk = stdout.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                stdout.channel.close()
            except Exception:
                pass

    def upload_file(self, dest_path: str, source, chunk_size: int = 64 * 1024) -> tuple[bool, str]:
        """Записать файл на сервер из файлового объекта (чанки → SFTP).

        Промежуточные папки создаются автоматически (_mkdirs),
        так что фронт может заливать деревья каталогов пофайлово.
        """
        try:
            parent = dest_path.rsplit('/', 1)[0] or '/'
            with self.ssh_client.open_sftp() as sftp:
                self._mkdirs(sftp, parent)
                with sftp.open(dest_path, 'wb') as f:
                    while True:
                        chunk = source.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
            return True, "Загружено"
        except Exception as e:
            return False, str(e)

    def _mkdirs(self, sftp, path: str):
        """Аналог mkdir -p поверх SFTP (устойчив к гонкам)"""
        if not path or path == '/':
            return
        cur = ''
        for part in [p for p in path.split('/') if p]:
            cur += '/' + part
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass  # уже создан параллельной загрузкой

    def _is_dir(self, attr) -> bool:
        """Проверка, является ли запись директорией"""
        return stat.S_ISDIR(attr.st_mode)

    def _rmtree(self, sftp, path: str):
        """Рекурсивное удаление папки"""
        for entry in sftp.listdir_attr(path):
            entry_path = f"{path}/{entry.filename}"
            if self._is_dir(entry):
                self._rmtree(sftp, entry_path)
            else:
                sftp.remove(entry_path)
        sftp.rmdir(path)
