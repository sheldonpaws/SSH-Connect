"""SSH Connect Web — веб-интерфейс для сервера:
REST API файлов + меню установленных программ (запуск в KiTTY).
"""
import os
import re
import secrets
import shlex
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, File, Form, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

from ssh_manager import SSHSession
from file_manager import FileManager
import hermes_manager
import hermes_pacer

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
    for sid, session in sessions.items():
        hermes_jobs.drop_web_session(sid)
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


class KittyLogRequest(BaseModel):
    session_id: str
    offset: int = 0
    tail: bool = False


class HermesSendRequest(BaseModel):
    session_id: str
    message: str
    resume: Optional[str] = None
    model: Optional[str] = None
    skills: Optional[str] = None
    toolsets: Optional[str] = None
    yolo: bool = False


class HermesSessionsRequest(BaseModel):
    session_id: str
    limit: int = 20
    sid: Optional[str] = None


class HermesSessionDeleteRequest(BaseModel):
    session_id: str
    sid: str


class HermesJobsReadRequest(BaseModel):
    session_id: str
    name: Optional[str] = None
    offset: int = 0


class HermesLogRequest(BaseModel):
    session_id: str
    file: Optional[str] = None
    lines: int = 40


class HermesDirRequest(BaseModel):
    session_id: str
    area: str = 'docs'   # docs | work | media | prompts


class HermesConfigSaveRequest(BaseModel):
    session_id: str
    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    fallback_model: Optional[str] = None
    fallback_provider: Optional[str] = None
    keys: Optional[dict] = None


hermes_jobs = hermes_manager.HermesJobManager()

import os as _os
import sys as _sys


def _resource_path(relative_path):
    """Получить абсолютный путь к ресурсу (работает и в .exe, и локально)"""
    try:
        base_path = _sys._MEIPASS
    except Exception:
        base_path = _os.path.abspath('.')
    paths_to_try = [
        _os.path.join(base_path, relative_path),
        _os.path.join(base_path, '..', relative_path),
        _os.path.join(_os.path.dirname(__file__), '..', relative_path),
    ]
    for path in paths_to_try:
        if _os.path.isdir(path) or _os.path.isfile(path):
            return _os.path.abspath(path)
    return _os.path.join(base_path, relative_path)


_frontend_dir = _resource_path('frontend')
if _os.path.isdir(_frontend_dir):
    app.mount('/static', StaticFiles(directory=_frontend_dir), name='static')


@app.middleware('http')
async def no_cache_static(request, call_next):
    """Запретить кэширование статики/HTML, чтобы правки фронтенда сразу подхватывались"""
    response = await call_next(request)
    if request.url.path.startswith(('/static', '/')) and request.method == 'GET':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@app.get('/')
async def index():
    """Главная страница — веб-интерфейс"""
    html_path = _resource_path('frontend/index.html')
    return FileResponse(html_path)


@app.get('/hermes')
async def hermes_page():
    """Страница чата с Hermes Agent"""
    html_path = _resource_path('frontend/hermes.html')
    return FileResponse(html_path)


COMMON_PROGRAMS = [
    'htop', 'top', 'mc', 'nano', 'vim', 'neofetch', 'df', 'du', 'systemctl',
    'journalctl', 'docker', 'pm2', 'screen', 'tmux', 'git', 'python3', 'node', 'npm', 'tree', 'free'
]


@app.post('/api/programs')
async def programs(req: FileListRequest):
    """Какие из частых программ установлены на сервере"""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    session = sessions[req.session_id]
    try:
        command = 'command -v ' + ' '.join(COMMON_PROGRAMS) + ' 2>/dev/null'
        stdin, stdout, stderr = session.client.exec_command(command, timeout=10)
        installed = stdout.read().decode('utf-8', errors='replace').split()
        return {'success': True, 'programs': installed}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.post('/api/connect')
async def connect(req: ConnectRequest):
    """Создать SSH подключение"""
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
    """Закрыть SSH подключение"""
    if session_id in sessions:
        hermes_jobs.drop_web_session(session_id)
        sessions[session_id].close()
        del sessions[session_id]
        auth_tokens.pop(session_id, None)
        return {'success': True, 'message': 'Отключено'}
    return {'success': False, 'message': 'Сессия не найдена'}


@app.get('/api/status')
async def session_status(session_id: str):
    """Активна ли сессия (страница показывает живое или сессия исчезла)"""
    return {'active': session_id in sessions}


@app.post('/api/files/list')
async def file_list(req: FileListRequest):
    """Список файлов"""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    session = sessions[req.session_id]
    fm = FileManager(session)
    success, entries, error = fm.list_directory(req.path)
    if success:
        return {'success': True, 'entries': entries, 'path': req.path}
    return {'success': False, 'error': error}


@app.post('/api/files/read')
async def file_read(req: FileReadRequest):
    """Чтение файла"""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    session = sessions[req.session_id]
    fm = FileManager(session)
    success, content, error = fm.read_file(req.path)
    if success:
        return {'success': True, 'content': content}
    return {'success': False, 'error': error}


@app.post('/api/files/save')
async def file_save(req: FileSaveRequest):
    """Сохранение файла"""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    session = sessions[req.session_id]
    fm = FileManager(session)
    success, message = fm.save_file(req.path, req.content)
    return {'success': success, 'message': message}


@app.post('/api/files/delete')
async def file_delete(req: FileActionRequest):
    """Удаление файла/папки"""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    session = sessions[req.session_id]
    fm = FileManager(session)
    success, message = fm.delete(req.path)
    return {'success': success, 'message': message}


@app.post('/api/files/rename')
async def file_rename(req: FileActionRequest):
    """Переименование"""
    if not req.new_path:
        raise HTTPException(status_code=400, detail='Нужен new_path')
    session = sessions[req.session_id]
    fm = FileManager(session)
    success, message = fm.rename(req.path, req.new_path)
    return {'success': success, 'message': message}


@app.post('/api/files/mkdir')
async def file_mkdir(req: FileActionRequest):
    """Создание папки"""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    session = sessions[req.session_id]
    fm = FileManager(session)
    success, message = fm.mkdir(req.path)
    return {'success': success, 'message': message}


def _download_response(session_id: str, path: str):
    """Общая логика скачивания: файл — бинарный поток, папка — tar.gz с сервера.

    Токен принимается только в заголовке X-Auth-Token, поэтому фронт
    качает через fetch -> blob (прямая <a>-ссылка заголовки не задаст).
    POST-вариант основной: у клиента GET с query-параметрами может
    искажаться прокси/расширениями, POST+JSON проходит всегда.
    """
    from urllib.parse import quote
    from fastapi.responses import StreamingResponse
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    if not path.startswith('/') or '\x00' in path:
        raise HTTPException(status_code=400, detail='Некорректный путь')
    fm = FileManager(sessions[session_id])
    exists, is_dir, _size = fm.stat_path(path)
    if not exists:
        raise HTTPException(status_code=404, detail='Не найдено на сервере')
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
    """Скачать файл/папку (основной вариант: POST + JSON в теле)."""
    return _download_response(req.session_id, req.path)


@app.get('/api/files/download')
async def download_file_get(path: str, session_id: Optional[str] = None, session: Optional[str] = None):
    """Совместимый GET-вариант (session — устаревший алиас session_id)."""
    if not session_id:
        session_id = session
    if not session_id:
        raise HTTPException(status_code=400, detail='Нужен session_id')
    return _download_response(session_id, path)


@app.post('/api/files/upload')
async def file_upload(session_id: str = Form(...), path: str = Form(...), file: UploadFile = File(...)):
    """Загрузить ОДИН файл на сервер.

    `path` — полный удалённый путь назначения (промежуточные папки
    создаются автоматически), поэтому фронт заливает деревья каталогов
    пофайлово и может показывать прогресс по каждому файлу. Бинарные
    данные пишутся чанками — размер файла не ограничен памятью API.
    """
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    if not path.startswith('/') or '\x00' in path or path.endswith('/'):
        return {'success': False, 'message': 'Некорректный путь'}
    fm = FileManager(sessions[session_id])
    ok, message = fm.upload_file(path, file.file)
    try:
        await file.close()
    finally:
        pass
    return {'success': ok, 'message': message}


@app.post('/api/launch')
async def launch(req: LaunchRequest):
    """Запустить команду в окне KiTTY (терминал веб-интерфейса).

    Веб-интерфейс не имеет собственного терминала: команда отправляется в
    уже открытое окно KiTTY через Win32 PostMessage.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    command = req.command.strip()
    if not command:
        return {'success': False, 'error': 'Пустая команда'}
    import kitty_launcher
    success, message = kitty_launcher.send_command(command)
    return {'success': success, 'message': message}


import tempfile

KITTY_LOG_FILE = os.path.join(tempfile.gettempdir(), 'kitty_session.log')
KITTY_LOG_TAIL = 8192


@app.post('/api/kitty/log')
async def kitty_log(req: KittyLogRequest):
    """Новые символы журнала KiTTY (живое зеркало терминала).

    KiTTY пишет сессионный лог в %TEMP%\\kitty_session.log. Клиент шлёт
    offset — отдаём прирост с этой позиции. tail=True — последние 8 КБ.
    Если offset больше размера лога — файл пересоздан новой сессией KiTTY,
    возвращаем reset=True, чтобы клиент очистил буфер.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    try:
        size = os.path.getsize(KITTY_LOG_FILE)
        if req.tail:
            start = max(0, size - KITTY_LOG_TAIL)
            reset = False
        elif req.offset > size:
            start = max(0, size - KITTY_LOG_TAIL)
            reset = True
        else:
            start = req.offset
            reset = False
        with open(KITTY_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(start)
            content = f.read()
        return {'success': True, 'exists': True, 'content': content, 'offset': size, 'reset': reset}
    except OSError:
        return {'success': True, 'exists': False, 'content': '', 'offset': 0, 'reset': False}


@app.post('/api/hermes/send')
async def hermes_send(req: HermesSendRequest):
    """Запустить фоновую (detached) задачу агента `hermes chat`.

    Задача выполняется на сервере в фоне (nohup) и НЕ прервётся при закрытии
    вкладки/отключении: вывод пишется в лог-файл ~/.hermes/jobs/. Вернёт
    job_id; вывод читается через GET /api/hermes/job/{job_id}.

    Одновременно может идти только одна задача на веб-сессию.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    message = (req.message or '').strip()
    if not message:
        raise HTTPException(status_code=400, detail='Пустое сообщение')
    pacer_state = None
    try:
        _, pacer_out = _exec_on_session(
            req.session_id, hermes_pacer.build_pacer_ensure_command())
        pacer_state = hermes_pacer.parse_pacer_state(pacer_out)
    except Exception:
        pacer_state = None
    job_id = uuid.uuid4().hex[:12]
    command, log_path = hermes_manager.build_detached_chat_command(
        job_id,
        message,
        resume=req.resume,
        model=req.model,
        skills=req.skills,
        toolsets=req.toolsets,
        yolo=req.yolo,
    )
    job_id, running = hermes_jobs.start(job_id, req.session_id, command, log_path, sessions[req.session_id])
    if job_id is None and running is None:
        return {'success': False, 'error': 'Не удалось запустить фоновую задачу'}
    if job_id is None:
        return {'success': False, 'error': 'У агента уже выполняется запрос', 'job_id': running.id}
    resp = {'success': True, 'job_id': job_id}
    if pacer_state is not None:
        resp['pacer'] = pacer_state
    return resp


@app.post('/api/hermes/pacer')
async def hermes_pacer_status(req: HermesSessionsRequest):
    """Развернуть/поднять пейсер-прокси и вернуть его состояние."""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    ok, out = _exec_on_session(
        req.session_id, hermes_pacer.build_pacer_ensure_command())
    st = hermes_pacer.parse_pacer_state(out)
    st['execute_ok'] = ok
    return st


@app.get('/api/hermes/job/{job_id}')
async def hermes_job(job_id: str, offset: int = 0):
    """Прирост вывода джобы Hermes начиная с offset."""
    job = hermes_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='Джоба не найдена')
    data = job.chunk_since(max(0, offset))
    data['success'] = True
    if job.done:
        data['session_id'] = hermes_manager.extract_session_id(job.buffer)
    else:
        data['session_id'] = None
    return data


@app.post('/api/hermes/stop')
async def hermes_stop(req: FileListRequest):
    """Прервать текущую джобу агента для этой веб-сессии."""
    stopped = hermes_jobs.stop_active(req.session_id)
    return {'success': True, 'stopped': stopped}


@app.post('/api/hermes/sessions')
async def hermes_sessions(req: HermesSessionsRequest):
    """Список недавних сессий агента (`hermes sessions list`)."""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    limit = max(1, min(int(req.limit or 20), 100))
    command = hermes_manager.build_list_command('sessions', f'list --limit {limit}')
    ok, out = _exec_on_session(req.session_id, command)
    if not ok:
        return {'success': False, 'error': out, 'entries': [], 'raw': ''}
    rows = hermes_manager.parse_sessions_table(out)
    entries = []
    for row in rows:
        sid = row.get('ID') or row.get('Id') or ''
        title = row.get('Title') or ''
        if not sid:
            continue
        entries.append({
            'id': sid.strip(),
            'title': title.strip(),
            'raw': ' | '.join(f"{k}: {v}" for k, v in row.items() if v),
        })
    return {'success': True, 'entries': entries, 'raw': out}


@app.post('/api/hermes/sessions/delete')
async def hermes_session_delete(req: HermesSessionDeleteRequest):
    """Удалить сессию агента (`hermes sessions delete --yes`).

    Необратимая операция — фронт запрашивает подтверждение. Если id не
    существует, hermes печатает 'Session ... not found.' и delete не проходит:
    отвечаем deleted=false, чтобы фронт мог поправить список.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    sid = (req.sid or '').strip()
    if not sid:
        return {'success': False, 'error': 'Не указан идентификатор сессии'}
    command = hermes_manager.build_delete_session_command(sid)
    ok, out = _exec_on_session(req.session_id, command)
    if not ok:
        return {'success': False, 'error': out}
    text = (out or '').strip()
    raised = 'Session ' in text and 'not found' in text.lower()
    deleted = not raised
    removed_jobs = 0
    if deleted:
        # Задачи `~/.hermes/jobs/hj_*.log` для этой сессии (в вебе каждая
        # отправка создаёт свою задачу) удаляем вместе с сессией — иначе
        # спойлер «Задачи» копит мёртвые записи навсегда.
        pat = re.escape(sid)
        cleanup = (
            'JOBS="$HOME/.hermes/jobs"; '
            'for f in "$JOBS"/hj_*.log; do '
            '[ -f "$f" ] || continue; '
            f'if grep -q "session_id: {pat}$" "$f" 2>/dev/null; then '
            'rm -f "$f" "${f%.log}.q"; removed=$((removed+1)); fi; '
            'done; echo "__REMOVED__${removed:-0}"'
        )
        _ok, cout = _exec_on_session(req.session_id, 'bash -lc ' + shlex.quote(cleanup))
        m = re.search(r'__REMOVED__(\d+)', cout or '')
        if m:
            removed_jobs = int(m.group(1))
    return {'success': True, 'deleted': deleted, 'sid': sid,
            'removed_jobs': removed_jobs, 'message': text[:500]}


_skills_desc_cache = {'at': 0.0, 'data': {}, 'ru': {}}


@app.post('/api/hermes/skills')
async def hermes_skills(req: FileListRequest):
    """Список установленных скиллов (`hermes skills list`) + описания.

    Описание берётся из front-matter SKILL.md каждого установленного скилла
    (одним серверным вызовом, TTL-кэш 300 с — список обновляется редко, а
    фронт опрашивает каждые 30 с). Русские переводы описаний тоже кэшируются
    (перевод 73 фраз выполняется пакетно один раз).
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    command = hermes_manager.build_list_command('skills', 'list --enabled-only')
    ok, out = _exec_on_session(req.session_id, command)
    if not ok:
        return {'success': False, 'error': out, 'entries': [], 'raw': ''}
    rows = hermes_manager.parse_rich_table(out)
    now = time.time()
    if now - _skills_desc_cache['at'] > 300:
        okd, dout = _exec_on_session(req.session_id, hermes_manager.build_skills_descriptions_command())
        _skills_desc_cache['data'] = hermes_manager.parse_skills_descriptions(dout) if okd else {}
        _skills_desc_cache['ru'] = hermes_manager.translate_skills_pack(_skills_desc_cache['data'])
        _skills_desc_cache['at'] = now
    descs = _skills_desc_cache['data']
    descs_ru = _skills_desc_cache['ru']
    entries = []
    for row in rows:
        if row.get('Name'):
            name = row.get('Name', '')
            entries.append({
                'name': name,
                'category': row.get('Category', ''),
                'description': descs.get(name, ''),
                'description_ru': descs_ru.get(name) or descs.get(name, ''),
            })
    return {'success': True, 'entries': entries, 'raw': out}


@app.post('/api/hermes/home')
async def hermes_home(req: FileListRequest):
    """Домашний каталог SSH-пользователя и наличие ~/.hermes.

    Hermes хранит состояние в ~/.hermes КАЖДОГО пользователя, поэтому все
    пути (SOUL.md и т.д.) строятся от домашнего каталога подключившегося,
    а не от /root.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    command = 'echo "$HOME"; [ -d "$HOME/.hermes" ] && echo __HERMES_DIR_OK__ || echo __HERMES_DIR_MISSING__'
    ok, out = _exec_on_session(req.session_id, command)
    lines = (out or '').split()
    home = lines[0] if lines else ''
    exists = '__HERMES_DIR_OK__' in lines and bool(home)
    user = home.rsplit('/', 1)[-1] if home else ''
    return {'success': bool(home), 'home': home, 'user': user,
            'hermes_dir': f'{home}/.hermes', 'exists': exists}


@app.post('/api/hermes/log')
async def hermes_log(req: HermesLogRequest):
    """Хвост журнала Hermes (`tail -n ~/.hermes/logs/agent.log`).

    Живой вывод выполняется на сервере, поэтому журнал любого размера
    читается без загрузки целиком в память браузера (в отличие от /api/files/read,
    который ограничен 1 МБ и падает на больших логах).
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    ok, out = _exec_on_session(req.session_id, 'tail -n %d "$HOME/.hermes/logs/agent.log" 2>&1' % max(1, min(req.lines, 4000)))
    if not ok:
        return {'success': False, 'error': out, 'lines': []}
    lines = (out or '').splitlines()
    return {'success': True, 'lines': lines, 'path': '$HOME/.hermes/logs/agent.log'}


@app.post('/api/hermes/dir')
async def hermes_dir(req: HermesDirRequest):
    """Списки файлов по рабочим областям Hermes (docs/work/media/prompts).

    Каталог $HOME вычисляется на сервере, поэтому фронту не нужно знать пути
    (как и для /api/hermes/sessions|skills|jobs). Один вызов — все группы областей.
    """
    session_id = req.session_id
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    ok, out = _exec_on_session(session_id, 'echo "$HOME"; [ -d "$HOME/.hermes" ] && echo OK || echo NO')
    lines = (out or '').split()
    home = lines[0] if lines else ''
    if not home:
        return {'success': False, 'error': 'Не удалось определить $HOME', 'groups': []}
    hermes = f'{home}/.hermes'

    if req.area == 'docs':
        groups = [{'label': 'documents', 'path': f'{hermes}/cache/documents'}]
    elif req.area == 'work':
        groups = [
            {'label': 'drafts', 'path': f'{home}/drafts'},
            {'label': 'ink', 'path': f'{home}/ink'},
            {'label': 'projects', 'path': f'{home}/projects'},
        ]
    elif req.area == 'media':
        groups = [
            {'label': 'images', 'path': f'{hermes}/cache/images'},
            {'label': 'screenshots', 'path': f'{hermes}/cache/screenshots'},
        ]
    elif req.area == 'prompts':
        groups = [{'label': 'memories', 'path': f'{hermes}/memories'}]
    else:
        return {'success': False, 'error': f'Неизвестная область: {req.area}', 'groups': []}

    fm = FileManager(sessions[session_id])
    result = []
    for g in groups:
        ok2, entries, err = fm.list_directory(g['path'])
        if not ok2 or err:
            result.append({'label': g['label'], 'path': g['path'], 'entries': [], 'error': err})
            continue
        files = [{'name': e['name'], 'size': e['size'], 'is_dir': e['is_dir']} for e in entries if not e['is_dir']]
        result.append({'label': g['label'], 'path': g['path'], 'entries': files, 'error': ''})
    return {'success': True, 'groups': result, 'home': home}


@app.post('/api/tmux/sessions')
async def tmux_sessions(req: FileListRequest):
    """Список активных tmux-сессий на сервере (`tmux ls`).

    Read-only «живой» индикатор: показывает, что происходит в терминале/KiTTY
    параллельно с веб-интерфейсом. Если tmux не установлен или сессий нет —
    возвращает пустой список.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    ok, out = _exec_on_session(req.session_id, 'tmux ls 2>&1')
    names = []
    if ok and out and out.strip() and 'no server running' not in out and 'error' not in out.lower():
        seen = set()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            name = line.split(':', 1)[0].strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return {'success': True, 'sessions': names, 'running': bool(names)}



@app.post('/api/hermes/history')
async def hermes_history(req: HermesSessionsRequest):
    """Полная переписка сессии агента (`hermes sessions export --format jsonl`).

    Сессии, начатые в терминале/tmux, живут в том же хранилище ~/.hermes —
    поэтому их историю можно читать здесь, в браузере.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    sid = (req.sid or '').strip()
    if not sid or any(c in sid for c in '"\'`; $\n'):
        raise HTTPException(status_code=400, detail='Некорректный id сессии')
    command = hermes_manager.build_export_command(sid)
    ok, out = _exec_on_session(req.session_id, command)
    if not ok:
        return {'success': False, 'error': out, 'entries': []}
    entries = hermes_manager.parse_session_jsonl(out)
    return {'success': True, 'entries': entries}


@app.post('/api/hermes/jobs/list')
async def hermes_jobs_list(req: FileListRequest):
    """Список фоновых задач agent (`~/.hermes/jobs/hj_*.log`).

    Фоновые задачи переживают закрытие вкладки/разрыв SSH. Здесь их можно
    видеть и досматривать после завершения. Мета для каждой: имя, размер,
    есть ли маркер завершения __RC__.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    ok, out = _exec_on_session(req.session_id, 'ls -1t "$HOME/.hermes/jobs"/hj_*.log 2>/dev/null')
    if not ok or not out.strip():
        return {'success': True, 'jobs': []}
    jobs = []
    for line in (out or '').splitlines():
        p = line.strip()
        if not p:
            continue
        name = p.rsplit('/', 1)[-1].replace('.log', '')
        # читаемый заголовок: содержимое .q (это уже текст сообщения); если .q
        # нет — берём первую содержательную строку из лога (минуя служебные)
        title = _exec_on_session(
            req.session_id,
            'test -f "$HOME/.hermes/jobs/' + name + '.q" && cat '
            '"$HOME/.hermes/jobs/' + name + '.q" 2>/dev/null | head -c 120'
        )[1]
        title = (title or '').strip()
        if not title:
            ok2, fallback = _exec_on_session(
                req.session_id,
                "grep -vE '^(__START__|__RC__=|session_id:)' "
                "'$HOME/.hermes/jobs/" + name + ".log' 2>/dev/null | "
                "grep -v '^[[:space:]]*$' | head -1 | head -c 120"
            )
            title = (fallback or '').strip()
        jobs.append({'name': name, 'title': title, 'path': p})
    return {'success': True, 'jobs': jobs}


@app.post('/api/hermes/jobs/read')
async def hermes_jobs_read(req: HermesJobsReadRequest):
    """Прочитать лог фоновой задачи по имени (инкрементально по offset)."""
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    name = (req.name or '').strip()
    if not name or any(c in name for c in '"\'`; $\n/\\'):
        raise HTTPException(status_code=400, detail='Некорректное имя задачи')
    path = '$HOME/.hermes/jobs/' + name + '.log'
    offset = max(0, int(req.offset or 0))
    cmd = 'tail -c +%d %s 2>/dev/null' % (offset + 1, path)
    ok, out = _exec_on_session(req.session_id, cmd)
    size_cmd = 'wc -c < %s 2>/dev/null' % path
    ok2, size_out = _exec_on_session(req.session_id, size_cmd)
    size = 0
    try:
        size = int(size_out.strip()) if ok2 and size_out.strip().isdigit() else len(out or '')
    except Exception:
        size = len(out or '')
    text = (out or '')
    done = '__RC__=' in text
    return {'success': True, 'name': name, 'size': size, 'done': done, 'text': text}


@app.post('/api/hermes/jobs/delete')
async def hermes_jobs_delete(req: HermesJobsReadRequest):
    """Удалить фоновую задачу agent (`~/.hermes/jobs/hj_<name>.log/.q`).

    Только свои файлы задачи: имя строго валидируется (без пути/слешей),
    удаляются сам лог и сопутствующий .q (текст запроса).
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    name = (req.name or '').strip()
    if not name or any(c in name for c in '"\'`; $\n/\\'):
        raise HTTPException(status_code=400, detail='Некорректное имя задачи')
    if not re.fullmatch(r'hj_[0-9a-f]{12}|[A-Za-z0-9_\-]{3,64}', name):
        raise HTTPException(status_code=400, detail='Некорректное имя задачи')
    cmd = ('test -f "$HOME/.hermes/jobs/%s.log" && '
           'rm -f "$HOME/.hermes/jobs/%s.log" "$HOME/.hermes/jobs/%s.q" && '
           'echo __DELETED__ || echo __MISSING__') % (name, name, name)
    ok, out = _exec_on_session(req.session_id, cmd)
    if not ok:
        return {'success': False, 'error': out}
    removed = '__DELETED__' in (out or '')
    return {'success': True, 'deleted': removed, 'name': name}


@app.post('/api/hermes/config/read')
async def hermes_config_read(req: FileListRequest):
    """Прочитать текущую модель/провайдера и наличие API-ключей.

    Сами значения ключей НИКОГДА не возвращаются — только признак
    SET/MISSING для каждого известного провайдера.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    command = hermes_manager.build_config_read_command()
    ok, out = _exec_on_session(req.session_id, command)
    if not ok:
        return {'success': False, 'error': out}
    state = hermes_manager.parse_config_read_output(out)
    state['success'] = True
    return state


@app.post('/api/hermes/config/save')
async def hermes_config_save(req: HermesConfigSaveRequest):
    """Применить модель/провайдера и (опционально) добавить API-ключи.

    Модель/провайдер задаются через `hermes config set`. Ключи пишутся в
    ~/.hermes/.env с правами 600 и хранятся ТОЛЬКО на сервере — в ответ не
    возвращаются.
    """
    if req.session_id not in sessions:
        raise HTTPException(status_code=404, detail='Сессия не найдена')
    cmd = hermes_manager.build_config_save_command(
        model=req.model,
        provider=req.provider,
        base_url=req.base_url,
        fallback_model=req.fallback_model,
        fallback_provider=req.fallback_provider,
    )
    if cmd:
        ok, out = _exec_on_session(req.session_id, cmd)
        if not ok:
            return {'success': False, 'error': out}
    applied = []
    errors = []
    valid_names = set(hermes_manager._PROVIDER_ENV_VARS.values())
    for name, value in (req.keys or {}).items():
        if name not in valid_names:
            errors.append(f'Неизвестный ключ: {name}')
            continue
        if not str(value).strip():
            continue
        kcmd = hermes_manager.build_env_key_command(name, str(value).strip())
        if kcmd is None:
            errors.append(f'{name}: недопустимое значение')
            continue
        ok, out = _exec_on_session(req.session_id, kcmd)
        if ok:
            applied.append(name)
        else:
            errors.append(f'{name}: {out}')
    return {'success': not errors, 'applied_keys': applied, 'errors': errors}


def _exec_on_session(session_id: str, command: str):
    """Выполнить команду в SSH-сессии, вернуть (ok, текст)."""
    session = sessions[session_id]
    try:
        stdin, stdout, stderr = session.client.exec_command(command, timeout=60)
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        code = stdout.channel.recv_exit_status()
        if code != 0 and error and not output:
            return False, error
        if output:
            return True, output
        return True, error
    except Exception as e:
        return False, str(e)


if __name__ == '__main__':
    uvicorn.run('main:app', host='127.0.0.1', port=8000, reload=False, access_log=False)
