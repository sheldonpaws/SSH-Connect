/**
 * SSH Connect Web — файловый менеджер + меню программ (запуск в KiTTY).
 * Параметры сессии берутся из URL: ?s=session_id&t=auth_token
 */
(() => {
    'use strict';

    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get('s');
    const authToken = params.get('t');

    if (!sessionId || !authToken) {
        showToast('Нет параметров сессии (?s=...&t=...).', 'error');
        return;
    }

    // Токен для переходов между страницами (файловый менеджер ↔ Hermes)
    try {
        sessionStorage.setItem('ssh_session', sessionId);
        sessionStorage.setItem('ssh_token', authToken);
    } catch (e) { /* не критично */ }
    const hermesLink = document.getElementById('hermes-link');
    if (hermesLink) {
        hermesLink.href = '/hermes?s=' + encodeURIComponent(sessionId) +
            '&t=' + encodeURIComponent(authToken);
        // Если Hermes не установлен для этого пользователя — не открываем
        // интерфейс, а сразу предупреждаем
        hermesLink.addEventListener('click', async (e) => {
            e.preventDefault();
            try {
                const data = await api('/api/hermes/home', {
                    method: 'POST',
                    body: { session_id: sessionId },
                });
                if (!data.success || !data.exists) {
                    showToast('⚕ Hermes не установлен для этого пользователя' +
                        (data.hermes_dir ? ' (нет каталога ' + data.hermes_dir + ')' : '') +
                        '. Установите его по SSH.', 'error');
                    return;
                }
            } catch (err) {
                // Проверка не удалась (старый сервер/сеть) — откроем страницу,
                // там предупреждение покажется само при необходимости
            }
            window.location.href = hermesLink.href;
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
            showToast('Доступ запрещён. Переподключитесь.', 'error');
            throw new Error('403');
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    };

    // === Элементы ===
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
        toastEl.textContent = msg;
        toastEl.className = kind;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.add('hidden'), 3000);
    }

    function setStatus(text) {
        statusText.textContent = text;
    }

    // === Файловый менеджер ===
    const Icons = {
        dir: '📁', txt: '📄', img: '🖼️', code: '📜', zip: '🗜️',
        bin: '⚙️', conf: '⚙️', exec: '🚀', music: '🎵', video: '🎬',
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
        pathInput.value = path === '/' ? '/' : path.replace(/\/$/, '');
        clearSelection();
        try {
            const data = await api('/api/files/list', { method: 'POST', body: { session_id: sessionId, path } });
            if (!data.success) {
                fileList.innerHTML = '<div class="file-empty">Ошибка: ' + escapeHtml(data.error || '') + '</div>';
                return;
            }
            renderFiles(data.entries);
            setStatus('📁 ' + path);
        } catch (e) {
            fileList.innerHTML = '<div class="file-empty">Ошибка загрузки: ' + escapeHtml(e.message) + '</div>';
        }
    }

    function renderFiles(entries) {
        if (!entries.length) {
            fileList.innerHTML = '<div class="file-empty">Папка пуста</div>';
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

    // === Редактор ===
    async function openEditor(path) {
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

    document.getElementById('editor-close').addEventListener('click', () => {
        editorModal.classList.add('hidden');
    });
    editorModal.addEventListener('click', (e) => {
        if (e.target === editorModal) editorModal.classList.add('hidden');
    });
    document.getElementById('editor-save').addEventListener('click', async () => {
        const path = editorTitle.textContent;
        try {
            const data = await api('/api/files/save', {
                method: 'POST',
                body: { session_id: sessionId, path, content: editorContent.value }
            });
            showToast(data.success ? 'Сохранено' : friendlySaveError(data.message), data.success ? 'success' : 'error');
        } catch (e) {
            showToast('Ошибка сохранения: ' + e.message, 'error');
        }
    });

    // Переводит технические ошибки в понятные подсказки (права на запись и т.п.)
    function friendlySaveError(message) {
        if (!message) return 'Ошибка сохранения';
        if (/Permission denied/i.test(message)) {
            return 'Нет прав на запись. Зайдите на сервер под root и повторите попытку';
        }
        if (/No such file/i.test(message)) {
            return 'Файл не найден на сервере (возможно, удалён или переименован)';
        }
        if (/Read-only/i.test(message)) {
            return 'Файл доступен только для чтения';
        }
        return 'Ошибка сохранения: ' + message;
    }

    // === Действия ===
    document.getElementById('go-btn').addEventListener('click', () => {
        let p = pathInput.value.trim();
        if (!p) return;
        if (!p.startsWith('/')) p = '/' + p;
        loadDir(p);
    });
    pathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') document.getElementById('go-btn').click();
    });

    document.getElementById('refresh-btn').addEventListener('click', () => loadDir(currentPath));
    document.getElementById('up-btn').addEventListener('click', () => {
        const parts = currentPath.replace(/\/$/, '').split('/').filter(Boolean);
        if (!parts.length) return;
        parts.pop();
        loadDir(parts.length ? '/' + parts.join('/') : '/');
    });

    document.getElementById('new-folder-btn').addEventListener('click', () => {
        const name = prompt('Имя новой папки:');
        if (!name) return;
        api('/api/files/mkdir', { method: 'POST', body: { session_id: sessionId, path: joinPath(currentPath, name.trim()) } })
            .then(d => {
                showToast(d.success ? 'Папка создана' : ('Ошибка: ' + d.message), d.success ? 'success' : 'error');
                loadDir(currentPath);
            })
            .catch(e => showToast('Ошибка: ' + e.message, 'error'));
    });

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

    document.getElementById('delete-btn').addEventListener('click', async () => {
        if (!selectedPath) { showToast('Выберите файл или папку', 'error'); return; }
        if (!confirm(`Удалить «${selectedName}»?`)) return;
        try {
            const d = await api('/api/files/delete', {
                method: 'POST',
                body: { session_id: sessionId, path: selectedPath }
            });
            showToast(d.success ? 'Удалено' : ('Ошибка: ' + d.message), d.success ? 'success' : 'error');
            loadDir(currentPath);
        } catch (e) { showToast('Ошибка: ' + e.message, 'error'); }
    });

    // === Загрузка на сервер (файлы и папки) ===
    // Папки заливаются пофайлово: на каждый файл — один запрос
    // /api/files/upload с полным путём назначения; недостающие папки
    // сервер создаёт сам. Прогресс — в строке статуса.
    const fileInput = document.getElementById('file-input');
    const dirInput = document.getElementById('dir-input');

    document.getElementById('upload-btn').addEventListener('click', () => fileInput.click());
    document.getElementById('upload-dir-btn').addEventListener('click', () => dirInput.click());

    async function uploadFiles(files) {
        if (!files.length) return;
        let failed = 0;
        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            const rel = file.webkitRelativePath || file.name;
            setStatus(`⬆ ${i + 1}/${files.length}: ${rel}`);
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
                if (!data.success) {
                    failed++;
                    showToast('Ошибка: ' + friendlySaveError(data.message), 'error');
                }
            } catch (e) {
                failed++;
                showToast('Ошибка загрузки: ' + e.message, 'error');
            }
        }
        setStatus('🟢 Онлайн');
        if (!failed) {
            showToast(files.length === 1 ? 'Файл загружен' : `Загружено файлов: ${files.length}`, 'success');
        }
        loadDir(currentPath);
    }

    fileInput.addEventListener('change', () => {
        uploadFiles([...fileInput.files]);
        fileInput.value = '';
    });
    dirInput.addEventListener('change', () => {
        // webkitRelativePath начинается с имени выбранной папки —
        // она сохраняется как подпапка в текущем каталоге
        uploadFiles([...dirInput.files]);
        dirInput.value = '';
    });

    // === Скачивание с сервера ===
    // POST + JSON — тот же транспорт, что у остальных вызовов API:
    // GET с query-параметрами у некоторых прокси/расширений искажается.
    // Токен ходит только в заголовке, поэтому blob через fetch.
    async function downloadRemote(path, name, isDir) {
        const downloadName = isDir ? name + '.tar.gz' : name;
        setStatus('⬇ Скачивание: ' + name);
        try {
            const res = await fetch('/api/files/download', {
                method: 'POST',
                headers: { 'X-Auth-Token': authToken, 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, path })
            });
            if (!res.ok) {
                let detail = '';
                try {
                    const d = (await res.json()).detail;
                    if (typeof d === 'string') detail = d;
                    else if (Array.isArray(d)) {
                        detail = d.map(x => (x.loc && x.loc.length ? x.loc.join('.') + ': ' : '') + (x.msg || '')).join('; ');
                    }
                } catch (e) { /* не JSON */ }
                throw new Error('HTTP ' + res.status + (detail ? ': ' + detail : ''));
            }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = downloadName;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 5000);
            showToast('Скачано: ' + downloadName, 'success');
        } catch (e) {
            showToast('Ошибка скачивания: ' + e.message, 'error');
        }
        setStatus('🟢 Онлайн');
    }

    document.getElementById('download-btn').addEventListener('click', () => {
        if (!selectedPath) { showToast('Выберите файл или папку', 'error'); return; }
        downloadRemote(selectedPath, selectedName, selectedIsDir);
    });

    // === Меню «Программы» (запуск в KiTTY) ===
    function openProgramsMenu() {
        programsMenu.classList.toggle('hidden');
        if (programsMenu.classList.contains('hidden')) return;
        if (programsList.dataset.loaded) return;

        programsList.innerHTML = '<div class="program-empty">Загрузка...</div>';
        api('/api/programs', { method: 'POST', body: { session_id: sessionId, path: '/' } })
            .then(data => {
                programsList.dataset.loaded = '1';
                if (!data.success) {
                    programsList.innerHTML = '<div class="program-empty">Ошибка: ' + escapeHtml(data.error || '') + '</div>';
                    return;
                }
                if (!data.programs.length) {
                    programsList.innerHTML = '<div class="program-empty">Ничего не найдено</div>';
                    return;
                }
                programsList.innerHTML = '';
                for (const p of data.programs) {
                    const item = document.createElement('div');
                    item.className = 'program-item';
                    item.textContent = p;
                    item.addEventListener('click', () => {
                        programsMenu.classList.add('hidden');
                        launchProgram(p);
                    });
                    programsList.appendChild(item);
                }
            })
            .catch(e => {
                programsList.innerHTML = '<div class="program-empty">Ошибка: ' + escapeHtml(e.message) + '</div>';
            });
    }

    async function launchProgram(program) {
        setStatus('🚀 Запуск: ' + program);
        try {
            const d = await api('/api/launch', { method: 'POST', body: { session_id: sessionId, command: program } });
            if (d.success) {
                showToast('Запуск «' + program + '» — см. окно KiTTY', 'success');
                setStatus('🟢 Онлайн');
            } else {
                showToast('Не удалось запустить: ' + (d.message || d.error || ''), 'error');
                setStatus('🟢 Онлайн');
            }
        } catch (e) {
            showToast('Ошибка запуска: ' + e.message, 'error');
            setStatus('🟢 Онлайн');
        }
    }

    programsBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openProgramsMenu();
    });
    document.addEventListener('click', () => programsMenu.classList.add('hidden'));

    // === Вспомогательные ===
    function escapeHtml(s) {
        const div = document.createElement('div');
        div.textContent = s == null ? '' : String(s);
        return div.innerHTML;
    }

    // === Статус сессии ===
    setInterval(() => {
        fetch(`/api/status?session_id=${sessionId}`, { headers: { 'X-Auth-Token': authToken } })
            .then(r => r.json())
            .then(d => {
                if (d.active) setStatus('🟢 Онлайн');
                else setStatus('🔴 Сессия закрыта');
            })
            .catch(() => {});
    }, 5000);

    // === Запуск ===
    setStatus('🟢 Онлайн');
    loadDir('/');
})();