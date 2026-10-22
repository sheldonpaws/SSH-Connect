/**
 * SSH Connect Web — файловый менеджер + меню программ (запуск в KiTTY).
 * Параметры передаются в URL: ?s=session_id&t=auth_token
 */
(() => {
    'use strict';

    const params = new URLSearchParams(window.location.search);
    // Параметры стираются из адресной строки (см. replaceState ниже), поэтому
    // при перезагрузке/открытии ссылки без query берём их из sessionStorage —
    // иначе F5 ломал бы страницу с «Нет параметров сессии».
    const store = {
        get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
        set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* не критично */ } }
    };
    const sessionId = params.get('s') || store.get('ssh_session');
    const authToken = params.get('t') || store.get('ssh_token');

    if (!sessionId || !authToken) {
        // showToast будет определён ниже
        console.error('Нет параметров сессии');
        return;
    }

    // Токен для переходов между страницами
    store.set('ssh_session', sessionId);
    store.set('ssh_token', authToken);

    // Кнопка «Docker-браузер»: бэкенд проверяет/ставит docker + chrome на VPS,
    // установка идёт фоном — опрашиваем статус, потом открываем страницу.
    const dockerLink = document.getElementById('docker-link');
    if (dockerLink) {
        dockerLink.addEventListener('click', (e) => {
            e.preventDefault();
            openDocker();
        });
    }

    // Кнопка «Hermes»: бэкенд поднимает локальный мост (порт 9120) к дашборду
    // Hermes на VPS через живую SSH-сессию — никаких ручных тоннелей KiTTY.
    const hermesLink = document.getElementById('hermes-link');
    if (hermesLink) {
        hermesLink.addEventListener('click', (e) => {
            e.preventDefault();
            openHermes();
        });
    }

    // Убираем токен из адресной строки (не светить в истории браузера).
    try {
        window.history.replaceState({}, document.title, window.location.pathname);
    } catch (e) { /* не критично */ }

    const api = async (url, options = {}) => {
        const headers = Object.assign(
            { 'X-Auth-Token': authToken },
            options.headers || {}
        );
        if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(options.body);
        }
        const res = await fetch(url, Object.assign({}, options, { headers }));
        if (res.status === 403) {
            // Сессия на бэкенде умерла — чиним кэш, чтобы F5 не подсовывал
            // заведомо мёртвый токен снова и снова.
            store.set('ssh_token', '');
            showToast('Доступ запрещён. Переподключитесь.', 'error');
            throw new Error('403');
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    };

    // === UI ===
    const pathInput = document.getElementById('path-input');
    const fileList = document.getElementById('file-list');
    const statusText = document.getElementById('status-text');
    const programsBtn = document.getElementById('programs-btn');
    const programsMenu = document.getElementById('programs-menu');
    const programsList = document.getElementById('programs-list');
    const toastEl = document.getElementById('toast');
    const editorModal = document.getElementById('editor-modal');
    const editorContent = document.getElementById('editor-content');
    const editorTitle = document.getElementById('editor-title');

    let currentPath = '/';
    let selectedPath = null;
    let selectedName = null;
    let selectedIsDir = false;

    // === Toast ===
    let toastTimer = null;
    function showToast(msg, kind = '') {
        if (!toastEl) return;
        toastEl.textContent = msg;
        toastEl.className = kind;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.add('hidden'), 3000);
    }

    // === Docker-браузер ===
    // Бэкенд ничего не устанавливает: если docker + контейнер chrome на сервере
    // уже есть — открываем страницу, иначе просто сообщаем, что не установлено.
    let dockerBusy = false;

    async function openDocker() {
        if (dockerBusy) return;
        dockerBusy = true;
        try {
            const start = await api('/api/docker/start', {
                method: 'POST',
                body: { session_id: sessionId },
            });
            if (!start.success) {
                showToast(start.error || 'Не удалось проверить Docker-браузер', 'error');
                return;
            }
            if (start.state === 'ready' && start.url) {
                window.open(start.url, '_blank');
                showToast('🐳 Открываю Docker-браузер', 'success');
                return;
            }
            showToast('Docker-браузер не установлен на сервере', 'error');
        } catch (err) {
            showToast('❌ ' + (err && err.message ? err.message : err), 'error');
        } finally {
            dockerBusy = false;
        }
    }

    // === Hermes-дашборд ===
    // Бэкенд поднимает локальный мост (127.0.0.1:9120) к дашборду на VPS
    // (127.0.0.1:9119) через живую SSH-сессию. Повторное нажатие, если мост
    // уже поднят, просто открывает страницу заново.
    let hermesBusy = false;
    async function openHermes() {
        if (hermesBusy) return;
        hermesBusy = true;
        try {
            const start = await api('/api/hermes/start', {
                method: 'POST',
                body: { session_id: sessionId },
            });
            if (!start.success) {
                showToast(start.error || 'Не удалось поднять мост к Hermes', 'error');
                return;
            }
            window.open(start.url, '_blank');
            showToast('🧠 Открываю дашборд Hermes', 'success');
        } catch (err) {
            showToast('❌ ' + (err && err.message ? err.message : err), 'error');
        } finally {
            hermesBusy = false;
        }
    }

    function setStatus(text) {
        if (statusText) statusText.textContent = text;
    }

    const Icons = {
        dir: '📁', txt: '📄', img: '🖼', code: '💻', zip: '🗜',
        bin: '🧱', conf: '⚙️', exec: '▶️', music: '🎵', video: '🎬',
        unknown: '📄'
    };

    function iconFor(name, isDir) {
        if (isDir) return Icons.dir;
        const ext = name.split('.').pop().toLowerCase();
        if (['png','jpg','jpeg','gif','webp','svg','ico'].includes(ext)) return Icons.img;
        if (['zip','tar','gz','bz2','xz','7z','rar'].includes(ext)) return Icons.zip;
        if (['py','js','sh','c','cpp','h','go','rs','java','rb','php','yaml','yml','json','html','css','sql'].includes(ext)) return Icons.code;
        if (['conf','ini','cfg','log','env'].includes(ext)) return Icons.conf;
        if (['mp3','wav','ogg','flac'].includes(ext)) return Icons.music;
        if (['mp4','avi','mkv','mov','webm'].includes(ext)) return Icons.video;
        if (['exe','deb','rpm','apk'].includes(ext)) return Icons.exec;
        return Icons.txt;
    }

    function formatSize(bytes) {
        if (!bytes) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    }

    async function loadDir(path) {
        currentPath = path;
        if (pathInput) pathInput.value = path === '/' ? '/' : path.replace(/\/$/, '');
        clearSelection();
        try {
            const data = await api('/api/files/list', { method: 'POST', body: { session_id: sessionId, path } });
            if (!data.success) {
                if (fileList) fileList.innerHTML = '<div class="file-empty">Ошибка: ' + escapeHtml(data.error || '') + '</div>';
                return;
            }
            renderFiles(data.entries);
            setStatus('📁 ' + path);
        } catch (e) {
            if (fileList) fileList.innerHTML = '<div class="file-empty">Ошибка загрузки: ' + escapeHtml(e.message) + '</div>';
        }
    }

    function renderFiles(entries) {
        if (!fileList) return;
        if (!entries.length) {
            fileList.innerHTML = '<div class="file-empty">Пусто</div>';
            return;
        }
        fileList.innerHTML = '';
        const frag = document.createDocumentFragment();
        for (const e of entries) {
            const row = document.createElement('div');
            row.className = 'file-item';
            row.dataset.name = e.name;
            row.dataset.isDir = e.is_dir;

            const ic = document.createElement('span');
            ic.className = 'f-icon';
            ic.textContent = iconFor(e.name, e.is_dir);
            const nm = document.createElement('span');
            nm.className = 'f-name';
            nm.textContent = e.name;
            nm.title = e.name;
            const sz = document.createElement('span');
            sz.className = 'f-size';
            sz.textContent = e.is_dir ? '' : formatSize(e.size);

            row.appendChild(ic);
            row.appendChild(nm);
            row.appendChild(sz);

            row.addEventListener('click', () => {
                selectRow(row, e.name, e.is_dir);
            });
            row.addEventListener('dblclick', () => {
                const path = currentPath === '/' ? '/' + e.name : currentPath + '/' + e.name;
                if (e.is_dir) {
                    loadDir(path);
                } else {
                    openEditor(path);
                }
            });
            frag.appendChild(row);
        }
        fileList.appendChild(frag);
    }

    function selectRow(row, name, isDir) {
        document.querySelectorAll('.file-item.selected').forEach(r => r.classList.remove('selected'));
        row.classList.add('selected');
        selectedPath = currentPath === '/' ? '/' + name : currentPath + '/' + name;
        selectedName = name;
        selectedIsDir = isDir;
    }

    function clearSelection() {
        selectedPath = null;
        selectedName = null;
        selectedIsDir = false;
    }

    function joinPath(base, name) {
        return base === '/' ? '/' + name : base.replace(/\/$/, '') + '/' + name;
    }

    async function openEditor(path) {
        if (!editorModal || !editorTitle || !editorContent) return;
        editorTitle.textContent = path;
        editorContent.value = '';
        editorModal.classList.remove('hidden');
        try {
            const data = await api('/api/files/read', { method: 'POST', body: { session_id: sessionId, path } });
            if (!data.success) {
                editorContent.value = 'Ошибка: ' + data.error;
                return;
            }
            editorContent.value = data.content;
        } catch (e) {
            editorContent.value = 'Ошибка: ' + e.message;
        }
    }

    function friendlySaveError(message) {
        if (!message) return 'Ошибка сохранения';
        if (/Permission denied/i.test(message)) return 'Нет прав на запись. Попробуйте не под root или другой путь.';
        if (/No such file/i.test(message)) return 'Файл не найден (возможно, папка не существует).';
        if (/Read-only/i.test(message)) return 'Файл доступен только для чтения.';
        return 'Ошибка сохранения: ' + message;
    }

    if (document.getElementById('editor-close')) {
        document.getElementById('editor-close').addEventListener('click', () => {
            if (editorModal) editorModal.classList.add('hidden');
        });
    }
    if (editorModal) {
        editorModal.addEventListener('click', (e) => {
            if (e.target === editorModal) editorModal.classList.add('hidden');
        });
    }
    if (document.getElementById('editor-save')) {
        document.getElementById('editor-save').addEventListener('click', async () => {
            const path = editorTitle ? editorTitle.textContent : '';
            try {
                const data = await api('/api/files/save', {
                    method: 'POST',
                    body: { session_id: sessionId, path, content: editorContent ? editorContent.value : '' }
                });
                showToast(data.success ? 'Сохранено' : friendlySaveError(data.message), data.success ? 'success' : 'error');
            } catch (e) {
                showToast('Ошибка сохранения: ' + e.message, 'error');
            }
        });
    }

    if (document.getElementById('go-btn')) {
        document.getElementById('go-btn').addEventListener('click', () => {
            let p = pathInput && pathInput.value.trim();
            if (!p) return;
            if (!p.startsWith('/')) p = '/' + p;
            loadDir(p);
        });
    }
    if (pathInput) {
        pathInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const b = document.getElementById('go-btn');
                if (b) b.click();
            }
        });
    }
    if (document.getElementById('refresh-btn')) document.getElementById('refresh-btn').addEventListener('click', () => loadDir(currentPath));
    if (document.getElementById('up-btn')) {
        document.getElementById('up-btn').addEventListener('click', () => {
            const parts = currentPath.replace(/\/$/, '').split('/').filter(Boolean);
            if (!parts.length) return;
            parts.pop();
            loadDir(parts.length ? '/' + parts.join('/') : '/');
        });
    }
    if (document.getElementById('new-folder-btn')) {
        document.getElementById('new-folder-btn').addEventListener('click', () => {
            const name = prompt('Имя папки:');
            if (!name) return;
            api('/api/files/mkdir', { method: 'POST', body: { session_id: sessionId, path: joinPath(currentPath, name.trim()) } })
                .then(d => {
                    showToast(d.success ? 'Папка создана' : ('Ошибка: ' + d.message), d.success ? 'success' : 'error');
                    loadDir(currentPath);
                })
                .catch(e => showToast('Ошибка: ' + e.message, 'error'));
        });
    }
    if (document.getElementById('rename-btn')) {
        document.getElementById('rename-btn').addEventListener('click', async () => {
            if (!selectedPath) { showToast('Выберите файл или папку', 'error'); return; }
            const name = prompt('Новое имя:', selectedName);
            if (!name || name === selectedName) return;
            const parts = selectedPath.split('/');
            parts[parts.length - 1] = name.trim();
            try {
                const d = await api('/api/files/rename', {
                    method: 'POST',
                    body: { session_id: sessionId, path: selectedPath, new_path: parts.join('/') }
                });
                showToast(d.success ? 'Переименовано' : ('Ошибка: ' + d.message), d.success ? 'success' : 'error');
                loadDir(currentPath);
            } catch (e) { showToast('Ошибка: ' + e.message, 'error'); }
        });
    }
    if (document.getElementById('delete-btn')) {
        document.getElementById('delete-btn').addEventListener('click', async () => {
            if (!selectedPath) { showToast('Выберите файл или папку', 'error'); return; }
            if (!confirm('Удалить «' + selectedName + '»?')) return;
            try {
                const d = await api('/api/files/delete', {
                    method: 'POST',
                    body: { session_id: sessionId, path: selectedPath }
                });
                showToast(d.success ? 'Удалено' : ('Ошибка: ' + d.message), d.success ? 'success' : 'error');
                loadDir(currentPath);
            } catch (e) { showToast('Ошибка: ' + e.message, 'error'); }
        });
    }

    const fileInput = document.getElementById('file-input');
    const dirInput = document.getElementById('dir-input');
    if (document.getElementById('upload-btn')) document.getElementById('upload-btn').addEventListener('click', () => fileInput && fileInput.click());
    if (document.getElementById('upload-dir-btn')) document.getElementById('upload-dir-btn').addEventListener('click', () => dirInput && dirInput.click());

    async function uploadFiles(files) {
        if (!files.length) return;
        let failed = 0;
        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            const rel = file.webkitRelativePath || file.name;
            setStatus('↑ ' + (i + 1) + '/' + files.length + ': ' + rel);
            const fd = new FormData();
            fd.append('session_id', sessionId);
            fd.append('path', joinPath(currentPath, rel));
            fd.append('file', file, file.name);
            try {
                const res = await fetch('/api/files/upload', {
                    method: 'POST',
                    headers: { 'X-Auth-Token': authToken },
                    body: fd
                });
                const data = await res.json();
                if (!data.success) { failed++; showToast('Ошибка: ' + friendlySaveError(data.message), 'error'); }
            } catch (e) { failed++; showToast('Ошибка загрузки: ' + e.message, 'error'); }
        }
        setStatus('Готово');
        if (!failed) showToast(files.length === 1 ? 'Файл загружен' : 'Загружено файлов: ' + files.length, 'success');
        loadDir(currentPath);
    }
    if (fileInput) fileInput.addEventListener('change', () => { uploadFiles([...fileInput.files]); fileInput.value = ''; });
    if (dirInput) dirInput.addEventListener('change', () => { uploadFiles([...dirInput.files]); dirInput.value = ''; });

    async function downloadRemote(path, name, isDir) {
        const downloadName = isDir ? name + '.tar.gz' : name;
        setStatus('↓ ' + name);
        try {
            const res = await fetch('/api/files/download', {
                method: 'POST',
                headers: { 'X-Auth-Token': authToken, 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, path })
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = downloadName;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 5000);
            showToast('Скачан: ' + downloadName, 'success');
        } catch (e) {
            showToast('Ошибка скачивания: ' + e.message, 'error');
        }
        setStatus('Готово');
    }
    if (document.getElementById('download-btn')) {
        document.getElementById('download-btn').addEventListener('click', () => {
            if (!selectedPath) { showToast('Выберите файл или папку', 'error'); return; }
            downloadRemote(selectedPath, selectedName, selectedIsDir);
        });
    }

    function openProgramsMenu() {
        if (!programsMenu || !programsList) return;
        programsMenu.classList.toggle('hidden');
        if (programsMenu.classList.contains('hidden')) return;
        if (programsList.dataset.loaded) return;
        programsList.innerHTML = '<div class="program-empty">Загрузка...</div>';
        api('/api/programs', { method: 'POST', body: { session_id: sessionId, path: '/' } })
            .then(data => {
                programsList.dataset.loaded = '1';
                if (!data.success) { programsList.innerHTML = '<div class="program-empty">Ошибка: ' + escapeHtml(data.error || '') + '</div>'; return; }
                if (!data.programs.length) { programsList.innerHTML = '<div class="program-empty">Не найдено</div>'; return; }
                programsList.innerHTML = '';
                for (const p of data.programs) {
                    const item = document.createElement('div');
                    item.className = 'program-item';
                    item.textContent = p;
                    item.addEventListener('click', () => { programsMenu.classList.add('hidden'); launchProgram(p); });
                    programsList.appendChild(item);
                }
            })
            .catch(e => { programsList.innerHTML = '<div class="program-empty">Ошибка: ' + escapeHtml(e.message) + '</div>'; });
    }
    async function launchProgram(program) {
        setStatus('▶ ' + program);
        try {
            const d = await api('/api/launch', { method: 'POST', body: { session_id: sessionId, command: program } });
            if (d.success) { showToast('Запущено: ' + program, 'success'); setStatus('Готово'); }
            else { showToast('Не удалось запустить: ' + (d.message || d.error || ''), 'error'); setStatus('Готово'); }
        } catch (e) { showToast('Ошибка запуска: ' + e.message, 'error'); setStatus('Готово'); }
    }
    if (programsBtn) programsBtn.addEventListener('click', (e) => { e.stopPropagation(); openProgramsMenu(); });
    document.addEventListener('click', () => { if (programsMenu) programsMenu.classList.add('hidden'); });

    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s == null ? '' : String(s);
        return div.innerHTML;
    }

    setInterval(() => {
        fetch(`/api/status?session_id=${sessionId}`, { headers: { 'X-Auth-Token': authToken } })
            .then(r => r.json())
            .then(d => { if (d.active) setStatus('Готово'); else setStatus('Сессия неактивна'); })
            .catch(() => {});
    }, 5000);

    setStatus('Готово');
    loadDir('/');
})();
