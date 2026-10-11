"""SSH Connect Web — веб-интерфейс для сервера:
REST API файлов + меню установленных программ (запуск в KiTTY)
+ Docker-браузер.
"""
import os
import secrets
import sys as _sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from ssh_manager import SSHSession
from file_manager import FileManager
import docker_manager

sessions: dict[str, SSHSession] = {}
auth_tokens: dict[str, str] = {}

SERVER_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.server_token')


def _get_or_create_server_token() -> str:
    if os.path.exists(SERVER_TOKEN_FILE):
        with open(SERVER_TOKEN_FILE, 'r', encoding='utf-8') as f:
            token = f.read().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    with open(SERVER_TOKEN_FILE, 'w', encoding='utf-8') as f:
        f.write(token)
    return token


SERVER_TOKEN = _get_or_create_server_token()


def _is_valid_session_token(token: str) -> bool:
    return token in auth_tokens.values()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запуск и остановка сервера"""
    print('SSH Connect Web server started!')
    yield
    for session in sessions.values():
        session.close()
    sessions.clear()
    auth_tokens.clear()


app = FastAPI(title='SSH Connect Web', lifespan=lifespan)


@app.middleware('http')
async def auth_middleware(request, call_next):
    """Защита API: требуется серверный или сессионный токен в заголовке.

    Токен принимается ТОЛЬКО из заголовка X-Auth-Token (не из query-строки:
    иначе токены светятся в логах/URL). Host обязан быть 127.0.0.1:8000 —
    защита от DNS-rebinding.

    /api/connect — создание сессии, требует серверный токен.
    Остальные /api/* — требуют сессионный (или серверный) токен.
    """
    host = request.headers.get('host') or ''
    if host.split(':')[0] not in ('127.0.0.1', 'localhost'):
        return JSONResponse(status_code=403, content={'detail': 'Доступ запрещён'})
    path = request.url.path
    token = request.headers.get('x-auth-token') or ''
    if path == '/api/connect':
        if token != SERVER_TOKEN:
            return JSONResponse(status_code=403, content={'detail': 'Доступ запрещён'})
    elif path.startswith('/api'):
        if token != SERVER_TOKEN and not _is_valid_session_token(token):
            return JSONResponse(status_code=403, content={'detail': 'Доступ запрещён'})
    return await call_next(request)


@app.middleware('http')
async def no_cache_static(request, call_next):
    """Статика не кэшируется — иначе старая разметка/скрипты после обновления.

    Страницы и CSS/JS отдаются с no-store, чтобы правки веб-интерфейса были
    видны сразу при следующем открытии.
    """
    response = await call_next(request)
    if request.url.path.startswith(('/static', '/')) and request.method == 'GET':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


def _resource_path(relative_path):
    """Путь к относительному пути от корня сборки (приложение в .exe, иначе — с .).

    В собранном .exe фронтенд лежит рядом с exe (build.bat копирует
    `web-desktop/` NEXT к `_internal`), поэтому `base_path/..` — это и есть
    каталог программы.
    """
    base_path = getattr(_sys, '_MEIPASS', None) or os.path.abspath('.')
    paths_to_try = [
        os.path.join(base_path, relative_path),
        os.path.join(base_path, '..', relative_path),
        os.path.join(os.path.dirname(__file__), '..', relative_path),
    ]
    for path in paths_to_try:
        if os.path.isdir(path) or os.path.isfile(path):
            return os.path.abspath(path)
    return os.path.join(base_path, relative_path)


_frontend_dir = _resource_path('frontend')
if os.path.isdir(_frontend_dir):
    app.mount('/static', StaticFiles(directory=_frontend_dir), name='static')


def _require_session(session_id: str) -> SSHSession:
    """Сессия по id или 404 (единая точка проверки для всех /api/*)."""
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    return session


def _exec_on_session(session_id: str, command: str, timeout: int = 60):
    """Выполнить команду в SSH-сессии, вернуть (ok, текст)."""
    session = sessions[session_id]
    try:
        stdin, stdout, stderr = session.client.exec_command(command, timeout=timeout)
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        code = stdout.channel.recv_exit_status()
        if code != 0 and error and not output:
            return False, error
        return True, output or error
    except Exception as e:
        return False, str(e)


class ConnectRequest(BaseModel):
    hostname: str
    port: int = 22
    username: str
    password: Optional[str] = None
    key_file: Optional[str] = None


class FileListRequest(BaseModel):
    session_id: str
    path: str = '/'


class FileReadRequest(BaseModel):
    session_id: str
    path: str


class FileSaveRequest(BaseModel):
    session_id: str
    path: str
    content: str


class FileActionRequest(BaseModel):
    session_id: str
    path: str
    new_path: Optional[str] = None


class FileDownloadRequest(BaseModel):
    session_id: str
    path: str


class LaunchRequest(BaseModel):
    session_id: str
    command: str


class DockerRequest(BaseModel):
    session_id: str


class KittyLogRequest(BaseModel):
    session_id: str
    offset: int = 0
    tail: bool = False


@app.get('/')
async def index():
    """Главная страница — веб-интерфейс"""
    return FileResponse(_resource_path('frontend/index.html'))


COMMON_PROGRAMS = [
    'htop', 'top', 'mc', 'nano', 'vim', 'neofetch', 'df', 'du', 'systemctl',
    'journalctl', 'pm2', 'screen', 'tmux', 'git', 'python3', 'node', 'npm', 'tree', 'free'
]


@app.post('/api/connect')
async def connect(req: ConnectRequest):
    """Открытие SSH-подключения"""
    session_id = str(uuid.uuid4())[:8]
    session = SSHSession(session_id)
    success, message = session.connect(
        hostname=req.hostname,
        port=req.port,
        username=req.username,
        password=req.password,
        key_file=req.key_file,
    )
    if success:
        sessions[session_id] = session
        auth_tokens[session_id] = secrets.token_urlsafe(32)
        return {'success': True, 'session_id': session_id, 'auth_token': auth_tokens[session_id], 'message': message}
    return {'success': False, 'session_id': None, 'message': message}


@app.post('/api/disconnect')
async def disconnect(session_id: str):
    """Закрытие SSH-подключения"""
    if session_id in sessions:
        sessions[session_id].close()
        del sessions[session_id]
        auth_tokens.pop(session_id, None)
        return {'success': True, 'message': 'Отключено'}
    return {'success': False, 'message': 'Сессия не найдена'}


@app.get('/api/status')
async def session_status(session_id: str):
    """Проверка живости сессии (страница отдаёт этот токен в заголовке)."""
    return {'active': session_id in sessions}


@app.post('/api/files/list')
async def file_list(req: FileListRequest):
    """Список файлов"""
    fm = FileManager(_require_session(req.session_id))
    success, entries, error = fm.list_directory(req.path)
    if success:
        return {'success': True, 'entries': entries, 'path': req.path}
    return {'success': False, 'error': error}


@app.post('/api/files/read')
async def file_read(req: FileReadRequest):
    """Чтение файла"""
    fm = FileManager(_require_session(req.session_id))
    success, content, error = fm.read_file(req.path)
    if success:
        return {'success': True, 'content': content}
    return {'success': False, 'error': error}


@app.post('/api/files/save')
async def file_save(req: FileSaveRequest):
    """Сохранение файла"""
    fm = FileManager(_require_session(req.session_id))
    success, message = fm.save_file(req.path, req.content)
    return {'success': success, 'message': message}


@app.post('/api/files/delete')
async def file_delete(req: FileActionRequest):
    """Удаление файла/папки"""
    fm = FileManager(_require_session(req.session_id))
    success, message = fm.delete(req.path)
    return {'success': success, 'message': message}


@app.post('/api/files/rename')
async def file_rename(req: FileActionRequest):
    """Переименование"""
    if not req.new_path:
        raise HTTPException(status_code=400, detail='Нужен new_path')
    fm = FileManager(_require_session(req.session_id))
    success, message = fm.rename(req.path, req.new_path)
    return {'success': success, 'message': message}


@app.post('/api/files/mkdir')
async def file_mkdir(req: FileActionRequest):
    """Создание папки"""
    fm = FileManager(_require_session(req.session_id))
    success, message = fm.mkdir(req.path)
    return {'success': success, 'message': message}


def _download_response(session_id: str, path: str):
    """Отдача файла/папки: файл — бинарный поток, папка — tar.gz с сервера.

    Скачивание идёт только в заголовке X-Auth-Token, поэтому фронт тянет
    ответ через fetch -> blob (прямой <a>-линк не умеет передавать заголовок).
    POST-вариант удобен: у него нет query-параметров, поэтому токен и путь
    POST+JSON остаются вне логов uvicorn.
    """
    if not path.startswith('/') or '\x00' in path:
        raise HTTPException(status_code=400, detail='Некорректный путь')
    fm = FileManager(_require_session(session_id))
    exists, is_dir, _size = fm.stat_path(path)
    if not exists:
        raise HTTPException(status_code=404, detail='Файл не найден')
    clean = path.rstrip('/')
    filename = clean.rsplit('/', 1)[-1] or 'download'
    if is_dir:
        parent = clean.rsplit('/', 1)[0] or '/'
        media_type = 'application/gzip'
        download_name = filename + '.tar.gz'
        stream = fm.iter_tar(parent, filename)
    else:
        media_type = 'application/octet-stream'
        download_name = filename
        stream = fm.iter_download(clean)
    ascii_name = download_name.encode('ascii', 'ignore').decode('ascii') or 'download'
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={
            'Content-Disposition': f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(download_name)}'
        }
    )


@app.post('/api/files/download')
async def download_file_post(req: FileDownloadRequest):
    """Скачивание файла/папки (удобный вариант: POST + JSON в теле)."""
    return _download_response(req.session_id, req.path)


@app.get('/api/files/download')
async def download_file_get(path: str, session_id: Optional[str] = None, session: Optional[str] = None):
    """Скачивание GET-вариантом (session — устаревший или session_id)."""
    return _download_response(session_id or session or '', path)


@app.post('/api/files/upload')
async def file_upload(session_id: str = Form(...), path: str = Form(...), file: UploadFile = File(...)):
    """Загрузка ОДНОГО файла на сервер.

    `path` — полный конечный путь на сервере (промежуточные папки создаются
    автоматически), поэтому фронт выгружает деревья папок пофайлово с
    индивидуальным прогрессом. Большие файлы не проходят через память API.
    """
    _require_session(session_id)
    if not path.startswith('/') or '\x00' in path or path.endswith('/'):
        return {'success': False, 'message': 'Некорректный путь'}
    fm = FileManager(sessions[session_id])
    ok, message = fm.upload_file(path, file.file)
    return {'success': ok, 'message': message}


@app.post('/api/programs')
async def programs(req: FileListRequest):
    """Какие из установленных программ доступны на сервере"""
    session = _require_session(req.session_id)
    try:
        command = 'command -v ' + ' '.join(COMMON_PROGRAMS) + ' 2>/dev/null'
        stdin, stdout, stderr = session.client.exec_command(command, timeout=10)
        installed = stdout.read().decode('utf-8', errors='replace').split()
        return {'success': True, 'programs': installed}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.post('/api/launch')
async def launch(req: LaunchRequest):
    """Запуск программы в уже открытом KiTTY (терминал — веб-интерфейса).

    Веб-интерфейс не имеет своего терминала: команда отправляется в нужное
    окно KiTTY через Win32 PostMessage.
    """
    session = _require_session(req.session_id)
    command = req.command.strip()
    if not command:
        return {'success': False, 'error': 'Пустая команда'}
    import kitty_launcher
    success, message = kitty_launcher.send_command(command, hostname=session.hostname)
    return {'success': success, 'message': message}


@app.post('/api/tmux/sessions')
async def tmux_sessions(req: FileListRequest):
    """Список активных tmux-сессий на сервере (`tmux ls`).

    Read-only окно: в вебе их нельзя создать/убить — это делает GUI и KiTTY
    напрямую. Если tmux не установлен или сервер не запущен, приходит пустой
    список, а не ошибка.
    """
    _require_session(req.session_id)
    ok, out = _exec_on_session(req.session_id, 'tmux ls 2>&1')
    names = []
    if ok and out and out.strip() and 'no server running' not in out and 'error' not in out.lower():
        seen = set()
        for line in out.splitlines():
            name = line.strip().split(':', 1)[0].strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return {'success': True, 'sessions': names, 'running': bool(names)}


@app.post('/api/docker/start')
async def docker_start(req: DockerRequest):
    """«Docker-браузер»: проверить готовность и открыть либо выдать команды.

    Бэкенд НИЧЕГО не устанавливает сам: если docker + контейнер `chrome` уже
    есть — отдаём адрес `https://<хост>:3001`; если нет — отдаём список команд
    ручной установки, пользователь выполняет их на сервере и жмёт повторно.
    """
    session = _require_session(req.session_id)
    if docker_manager.check_ready(session.client):
        return {'success': True, 'state': 'ready', 'url': docker_manager.browser_url(session.hostname)}
    return {'success': True, 'state': 'install',
            'commands': docker_manager.DOCKER_SETUP_COMMANDS}


KITTY_LOG_FILE = os.path.join(tempfile.gettempdir(), 'kitty_session.log')
KITTY_LOG_TAIL = 8192


@app.post('/api/kitty/log')
async def kitty_log(req: KittyLogRequest):
    """Хвост буфера KiTTY (утечка не нужна — терминалы в веб-интерфейсе нет).

    KiTTY пишет свой лог в %TEMP%\\kitty_session.log. Клиент держит offset —
    начало чтения в файле, tail=True — читает последние 8 КБ.
    Если offset больше размера — файл перезапущен, отвечаем с reset=True,
    чтобы клиент прочитал с нуля и не ждал обрывов в буфере KiTTY.
    """
    _require_session(req.session_id)
    try:
        size = os.path.getsize(KITTY_LOG_FILE)
        if req.tail:
            start, reset = max(0, size - KITTY_LOG_TAIL), False
        elif req.offset > size:
            start, reset = max(0, size - KITTY_LOG_TAIL), True
        else:
            start, reset = req.offset, False
        with open(KITTY_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(start)
            content = f.read()
        return {'success': True, 'exists': True, 'content': content, 'offset': size, 'reset': reset}
    except OSError:
        return {'success': True, 'exists': False, 'content': '', 'offset': 0, 'reset': False}


if __name__ == '__main__':
    uvicorn.run('main:app', host='127.0.0.1', port=8000, reload=False, access_log=False)
