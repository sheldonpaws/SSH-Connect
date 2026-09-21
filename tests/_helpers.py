# -*- coding: utf-8 -*-
"""Фейки для offline-тестов (SSH, SFTP, hostkeys, exec-каналы)."""

import stat


class FakeKey:
    def __init__(self, name="ssh-rsa"):
        self._name = name

    def get_name(self):
        return self._name


class _FakeHostKeys:
    def __init__(self):
        self.data = {}

    def add(self, hostname, keytype, key):
        self.data.setdefault(hostname, {})[keytype] = key


class FakeParamikoClient:
    """Подмена paramiko.SSHClient для hostkeys-тестов."""

    def __init__(self):
        self._host_keys = _FakeHostKeys()
        self.loaded = []
        self.policy = None
        self.saved = []

    def load_host_keys(self, path):
        self.loaded.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def save_host_keys(self, path):
        self.saved.append(path)


class FakeShellClient:
    """Подмена paramiko-клиента: только exec_command (для джоб hermes)."""

    def __init__(self):
        self.exec_calls = []

    def exec_command(self, command, timeout=None):
        self.exec_calls.append((command, timeout))
        return None, None, None


class FakeSSH:
    """Подмена src.ssh.SSHClient: сценарий ответов на execute_command."""

    def __init__(self, is_connected=True, responses=None):
        self.is_connected = is_connected
        self.commands = []
        self._responses = list(responses or [])
        self.client = FakeShellClient()

    def execute_command(self, command):
        self.commands.append(command)
        if self._responses:
            return self._responses.pop(0)
        return True, ""


class FakeAttr:
    def __init__(self, filename, mode, size, mtime=0):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class FakeSFTPFile:
    def __init__(self, sftp, path, mode):
        self._sftp = sftp
        self._path = path
        self._mode = mode
        self._pos = 0

    def prefetch(self):
        pass

    def read(self, n=-1):
        raw = self._sftp.files.get(self._path, b"")
        if n is None or n < 0:
            chunk = raw[self._pos:]
            self._pos = len(raw)
        else:
            chunk = raw[self._pos:self._pos + n]
            self._pos += len(chunk)
        if "b" in self._mode:
            return chunk
        return chunk.decode("utf-8")

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        # open() уже обнулил файл для режимов 'w'/'wb', поэтому дописываем
        existing = self._sftp.files.get(self._path, b"")
        self._sftp.files[self._path] = existing + data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSFTP:
    """In-memory SFTP: files {path: bytes}, dirs {path}."""

    def __init__(self):
        self.files = {}
        self.dirs = {"/"}

    def listdir_attr(self, path):
        p = path.rstrip("/") or "/"
        if p not in self.dirs:
            raise IOError("no such directory: " + path)
        base = "" if p == "/" else p
        out = []
        for fp in list(self.files) + list(self.dirs):
            if fp == "/":
                continue
            if base:
                if not fp.startswith(base + "/"):
                    continue
                rel = fp[len(base) + 1:]
            else:
                rel = fp.lstrip("/")
            if not rel or "/" in rel:
                continue
            if fp in self.dirs:
                out.append(FakeAttr(rel, stat.S_IFDIR, 0))
            else:
                out.append(FakeAttr(rel, stat.S_IFREG, len(self.files[fp]), 1234))
        return out

    def stat(self, path):
        p = path.rstrip("/") or "/"
        if p in self.dirs:
            return FakeAttr(p, stat.S_IFDIR, 0)
        if p in self.files:
            return FakeAttr(p, stat.S_IFREG, len(self.files[p]), 1234)
        raise IOError("no such file: " + path)

    def open(self, path, mode="rb"):
        if "w" in mode:
            self.files[path] = b""
        elif path not in self.files:
            self.files.setdefault(path, b"")
        return FakeSFTPFile(self, path, mode)

    def remove(self, path):
        if path not in self.files:
            raise IOError("no such file: " + path)
        del self.files[path]

    def rename(self, old, new):
        if old not in self.files:
            raise IOError("no such file: " + old)
        self.files[new] = self.files.pop(old)

    def mkdir(self, path):
        if path in self.dirs:
            raise IOError("already exists: " + path)
        self.dirs.add(path)

    def rmdir(self, path):
        if path not in self.dirs:
            raise IOError("no such dir: " + path)
        self.dirs.discard(path)


class _SFTPContext:
    def __init__(self, sftp):
        self.sftp = sftp

    def __enter__(self):
        return self.sftp

    def __exit__(self, *exc):
        return False


class _FakeStream:
    def __init__(self, data=b""):
        self._data = data
        self._pos = 0
        self.closed = False

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + n]
            self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class _FakeChannel:
    def close(self):
        pass


class FakeStreamStdout(_FakeStream):
    def __init__(self, data=b""):
        super().__init__(data)
        self.channel = _FakeChannel()


class FakeSFTPClient:
    """Подмена paramiko client для FileManager."""

    def __init__(self, sftp=None, tar_data=b"TARDATA"):
        self._sftp = sftp or FakeSFTP()
        self._tar_data = tar_data
        self.exec_calls = []

    def open_sftp(self):
        return _SFTPContext(self._sftp)

    def exec_command(self, command, timeout=None):
        self.exec_calls.append((command, timeout))
        return _FakeStream(), FakeStreamStdout(self._tar_data), _FakeStream()


class FakeSession:
    """Подмена SSHSession (у неё есть .client)."""

    def __init__(self, client):
        self.client = client
