/**
 * SSH Connect Web — чат с Hermes Agent на сервере.
 * Параметры сессии: ?s=session_id&t=auth_token (или sessionStorage).
 */
(() => {
    'use strict';

    const params = new URLSearchParams(window.location.search);
    let sessionId = params.get('s');
    let authToken = params.get('t');

    // Переход между страницами: токен живёт в sessionStorage
    if (sessionId && authToken) {
        try {
            sessionStorage.setItem('ssh_session', sessionId);
            sessionStorage.setItem('ssh_token', authToken);
        } catch (e) { /* не критично */ }
    } else {
        try {
            sessionId = sessionId || sessionStorage.getItem('ssh_session');
            authToken = authToken || sessionStorage.getItem('ssh_token');
        } catch (e) { /* не критично */ }
    }

    const backLink = document.getElementById('back-link');
    if (sessionId && authToken) {
        backLink.href = '/?s=' + encodeURIComponent(sessionId) + '&t=' + encodeURIComponent(authToken);
    }

    if (!sessionId || !authToken) {
        showToast('Нет параметров сессии. Откройте страницу из приложения.', 'error');
        return;
    }

    // Убираем токен из адресной строки
    try {
        window.history.replaceState({}, document.title, window.location.pathname);
    } catch (e) { /* не критично */ }

    // Несколько секунд на ответ — иначе поверх очевидная ошибка вместо вечной «Загрузка…».
    const API_TIMEOUT_MS = 10000;

    class ApiError extends Error {
        constructor(kind, msg) { super(msg); this.kind = kind; }
    }

    const api = async (url, options = {}) => {
        let controller;
        const opts = Object.assign({}, options);
        // тело JSON
        const headers = Object.assign(
            { 'X-Auth-Token': authToken },
            opts.headers || {}
        );
        if (opts.body && typeof opts.body !== 'string') {
            headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(opts.body);
        }
        opts.headers = headers;
        // таймаут
        controller = new AbortController();
        opts.signal = controller.signal;
        const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
        let res;
        try {
            res = await fetch(url, opts);
        } catch (e) {
            clearTimeout(timer);
            if (e && e.name === 'AbortError') throw new ApiError('TIMEOUT', 'Нет ответа от сервера');
            throw new ApiError('NETWORK', e && e.message ? e.message : 'Нет связи');
        }
        clearTimeout(timer);
        if (res.status === 403) throw new ApiError('AUTH', 'Доступ запрещён (403)');
        if (!res.ok) throw Object.assign(new ApiError('HTTP', 'HTTP ' + res.status), { status: res.status });
        return res.json();
    };

    // === Элементы ===
    const chatLog = document.getElementById('chat-log');
    const chatInput = document.getElementById('chat-input');
    const sendBtn = document.getElementById('send-btn');
    const stopBtn = document.getElementById('stop-btn');
    const yoloChk = document.getElementById('yolo-chk');
    const skillsHint = document.getElementById('skills-hint');
    const sessionsList = document.getElementById('sessions-list');
    const skillsList = document.getElementById('skills-list');
    const jobsList = document.getElementById('jobs-list');
    const docsList = document.getElementById('docs-list');
    const workList = document.getElementById('work-list');
    const mediaList = document.getElementById('media-list');
    const promptsList = document.getElementById('prompts-list');
    const logView = document.getElementById('log-view');
    const statusText = document.getElementById('status-text');
    const sessionLabel = document.getElementById('hermes-session-label');
    const toastEl = document.getElementById('toast');
    const editorModal = document.getElementById('editor-modal');
    const editorContent = document.getElementById('editor-content');
    const editorTitle = document.getElementById('editor-title');
    const configSummary = document.getElementById('config-summary');
    const pacerInd = document.getElementById('pacer-ind');

    // === Состояние ===
    let hermesSession = null;   // id сессии агента (resume)
    let hermesDir = null;       // $HOME/.hermes подключённого пользователя
    let hermesUser = null;      // имя пользователя SSH на сервере
    let jobId = null;
    let jobOffset = 0;
    let pollTimer = null;
    let agentEl = null;         // пузырь стримящегося ответа
    // Скилы по умолчанию: включаются всегда, без ручной отметки.
    // reverse-ssh-tunnel даёт агенту доступ к папкам ПК через обратный туннель.
    const DEFAULT_SKILLS = ['reverse-ssh-tunnel'];
    let selectedSkills = new Set(DEFAULT_SKILLS);

    // === Toast ===
    let toastTimer = null;
    function showToast(msg, kind = '') {
        toastEl.textContent = msg;
        toastEl.className = kind;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toastEl.classList.add('hidden'), 3500);
    }

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // Описание скилла — небольшое модальное окно с текстом.
    function showSkillInfo(name, desc, cat) {
        const overlay = document.createElement('div');
        overlay.className = 'skill-popup';
        const box = document.createElement('div');
        box.className = 'skill-popup-box';
        const head = document.createElement('div');
        head.className = 'skill-popup-head';
        const headText = document.createElement('span');
        headText.textContent = name;
        const close = document.createElement('button');
        close.className = 'skill-popup-close';
        close.textContent = '✕';
        close.title = 'Закрыть';
        close.addEventListener('click', () => overlay.remove());
        head.append(headText, close);
        const body = document.createElement('div');
        body.className = 'skill-popup-body';
        if (cat) {
            const c = document.createElement('div');
            c.className = 'skill-popup-cat';
            c.textContent = cat;
            body.appendChild(c);
        }
        const p = document.createElement('div');
        p.textContent = desc;
        body.appendChild(p);
        box.append(head, body);
        overlay.appendChild(box);
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) overlay.remove();
        });
        document.body.appendChild(overlay);
    }

    // === Чат ===
    function addMsg(kind, text) {
        const div = document.createElement('div');
        div.className = 'msg ' + kind;
        div.textContent = text || '';
        chatLog.appendChild(div);
        chatLog.scrollTop = chatLog.scrollHeight;
        return div;
    }

    function setBusy(busy) {
        sendBtn.disabled = busy;
        stopBtn.classList.toggle('hidden', !busy);
    }

    function setSession(id) {
        hermesSession = id;
        sessionLabel.textContent = id ? ('сессия: ' + id.slice(0, 8) + '…') : '';
        renderSessionsActive();
    }

    async function send() {
        const text = chatInput.value.trim();
        if (!text || jobId) return;

        addMsg('user', text);
        chatInput.value = '';

        const body = {
            session_id: sessionId,
            message: text,
            resume: hermesSession || undefined,
            skills: selectedSkills.size ? [...selectedSkills].join(',') : undefined,
            yolo: yoloChk.checked,
        };

        let resp;
        try {
            resp = await api('/api/hermes/send', { method: 'POST', body });
        } catch (e) {
            addMsg('error', 'Ошибка отправки: ' + e.message);
            return;
        }
        if (!resp.success) {
            addMsg('error', resp.error || 'Не удалось запустить агента');
            return;
        }

        jobId = resp.job_id;
        jobOffset = 0;
        agentEl = addMsg('agent', '');
        const cursor = document.createElement('span');
        cursor.className = 'cursor';
        agentEl.appendChild(cursor);
        setBusy(true);
        pollJob();
        refreshPacer();
    }

    function pollJob() {
        pollTimer = setTimeout(async () => {
            if (!jobId) return;
            let data;
            try {
                data = await api('/api/hermes/job/' + jobId + '?offset=' + jobOffset);
            } catch (e) {
                finishJob(null, 'Связь с сервером потеряна');
                return;
            }
            if (data.chunk) {
                agentEl.textContent += data.chunk;
                chatLog.scrollTop = chatLog.scrollHeight;
            }
            jobOffset = data.offset || jobOffset;
            if (data.done) {
                finishJob(data.session_id, data.error || null, data.exit_code);
            } else {
                pollJob();
            }
        }, 700);
    }

    function finishJob(newSessionId, error, exitCode) {
        clearTimeout(pollTimer);
        pollTimer = null;
        jobId = null;
        setBusy(false);

        // Убираем курсор
        if (agentEl) {
            const cur = agentEl.querySelector('.cursor');
            if (cur) cur.remove();
            if (!agentEl.textContent.trim()) {
                agentEl.textContent = error ? '' : '(пустой ответ)';
                if (error) agentEl.classList.add('error');
            }
        }

        if (newSessionId && newSessionId !== hermesSession) {
            setSession(newSessionId);
            addMsg('system', 'Сессия агента сохранена: ' + newSessionId.slice(0, 8) + '…');
        }
        if (error) {
            addMsg('error', error);
        } else if (exitCode != null && exitCode !== 0) {
            addMsg('system', 'Агент завершился с кодом ' + exitCode);
        }
        loadSessions();
    }

    async function stopJob() {
        try {
            await api('/api/hermes/stop', { method: 'POST', body: { session_id: sessionId } });
            showToast('Останавливаю…', '');
        } catch (e) { /* игнорируем */ }
    }

    // === Сессии ===
    async function loadSessions() {
        let data;
        try {
            data = await api('/api/hermes/sessions', { method: 'POST', body: { session_id: sessionId, limit: 20 } });
        } catch (e) {
            sessionsList.innerHTML = '<div class="side-empty side-err">' + esc(e.kind === 'AUTH' ? 'Сессия истекла (403)' : 'Ошибка загрузки') + '</div>';
            return;
        }
        sessionsList.innerHTML = '';
        if (!data.success || !data.entries.length) {
            sessionsList.innerHTML = '<div class="side-empty">Пока нет сессий</div>';
            return;
        }
        for (const s of data.entries) {
            const row = document.createElement('div');
            row.className = 'side-row';
            const btn = document.createElement('button');
            btn.className = 'session-item';
            btn.dataset.sid = s.id || '';
            btn.textContent = s.title || s.raw || (s.id || '').slice(0, 8) || '?';
            btn.title = s.raw || s.id || '';
            btn.addEventListener('click', () => {
                if (!s.id) return;
                setSession(s.id);
                const loading = addMsg('system', 'Загружаю историю сессии «' + (s.title || s.id.slice(0, 8)) + '»…');
                loadHistory(s.id, loading);
            });
            row.appendChild(btn);
            const del = document.createElement('button');
            del.className = 'session-del';
            del.textContent = '✕';
            del.title = 'Удалить сессию (безвозвратно)';
            del.addEventListener('click', async () => {
                if (!s.id) return;
                if (!confirm('Удалить сессию «' + (s.title || s.id.slice(0, 8)) + '» безвозвратно?')) return;
                del.disabled = true;
                try {
                    const r = await api('/api/hermes/sessions/delete', { method: 'POST', body: { session_id: sessionId, sid: s.id } });
                    if (r.success && r.deleted) {
                        const jobs = (r.removed_jobs ? ', задач: ' + r.removed_jobs : '');
                        showToast('Сессия удалена' + jobs, 'success');
                        btn.remove();
                        del.remove();
                        if (hermesSession === s.id) hermesSession = '';
                        if (!sessionsList.querySelector('.session-item')) {
                            sessionsList.innerHTML = '<div class="side-empty">Пока нет сессий</div>';
                        }
                        loadJobs();
                    } else {
                        showToast(r.message || r.error || 'Не удалось удалить сессию', 'error');
                    }
                } catch (e) {
                    showToast(e.kind === 'AUTH' ? 'Сессия истекла (403)' : 'Ошибка удаления', 'error');
                } finally {
                    del.disabled = false;
                }
            });
            row.appendChild(del);
            sessionsList.appendChild(row);
        }
        renderSessionsActive();
    }

    // История сессии (в том числе начатой в терминале/tmux): читаем экспорт JSONL
    async function loadHistory(sid, loadingMsg) {
        let data;
        try {
            data = await api('/api/hermes/history', { method: 'POST', body: { session_id: sessionId, sid } });
        } catch (e) {
            if (loadingMsg) loadingMsg.remove();
            addMsg('error', 'Не удалось загрузить историю сессии');
            return;
        }
        if (loadingMsg) loadingMsg.remove();
        if (!data.success) {
            addMsg('error', 'История недоступна: ' + (data.error || 'ошибка сервера'));
            return;
        }
        if (!data.entries.length) {
            addMsg('system', 'В этой сессии пока нет сообщений. Напишите что-нибудь, чтобы продолжить контекст.');
            return;
        }
        for (const m of data.entries) {
            addMsg(m.role === 'user' ? 'user' : 'agent', m.text).classList.add('history');
        }
        addMsg('system', 'Показана история сессии. Новые сообщения продолжат этот диалог.');
    }

    function renderSessionsActive() {
        for (const el of sessionsList.querySelectorAll('.session-item')) {
            el.classList.toggle('active', !!hermesSession && el.dataset.sid === hermesSession);
        }
    }

    // === Фоновые задачи (detached) ===
    async function loadJobs() {
        let data;
        try {
            data = await api('/api/hermes/jobs/list', { method: 'POST', body: { session_id: sessionId } });
        } catch (e) {
            jobsList.innerHTML = '<div class="side-empty side-err">' + esc(e.kind === 'AUTH' ? 'Сессия истекла (403)' : 'Ошибка загрузки') + '</div>';
            return;
        }
        if (!data.success) {
            jobsList.innerHTML = '<div class="side-empty side-err">Ошибка загрузки</div>';
            return;
        }
        const jobs = data.jobs || [];
        jobsList.innerHTML = '';
        if (!jobs.length) {
            jobsList.innerHTML = '<div class="side-empty">Пока нет фоновых задач</div>';
            return;
        }
        for (const j of jobs) {
            const row = document.createElement('div');
            row.className = 'side-row';
            const name = document.createElement('button');
            name.className = 'job-item';
            name.textContent = j.title || j.name;
            name.title = 'Открыть вывод задачи ' + j.name;
            name.addEventListener('click', () => openJob(j.name));
            const status = document.createElement('span');
            status.className = 'mini-status';
            status.textContent = '…';
            row.appendChild(name);
            row.appendChild(status);
            const del = document.createElement('button');
            del.className = 'session-del';
            del.textContent = '✕';
            del.title = 'Удалить задачу';
            del.addEventListener('click', async () => {
                if (!confirm('Удалить задачу «' + (j.title || j.name) + '»?')) return;
                del.disabled = true;
                try {
                    const r = await api('/api/hermes/jobs/delete', { method: 'POST', body: { session_id: sessionId, name: j.name } });
                    if (r.success && r.deleted) {
                        showToast('Задача удалена', 'success');
                        row.remove();
                        if (!jobsList.querySelector('.side-row')) {
                            jobsList.innerHTML = '<div class="side-empty">Пока нет фоновых задач</div>';
                        }
                    } else {
                        showToast(r.error || 'Не удалось удалить задачу', 'error');
                        del.disabled = false;
                    }
                } catch (e) {
                    showToast(e.kind === 'AUTH' ? 'Сессия истекла (403)' : 'Ошибка удаления', 'error');
                    del.disabled = false;
                }
            });
            row.appendChild(del);
            // определим статус (завершена/идёт)
            checkJobStatus(j.name, row);
            jobsList.appendChild(row);
        }
    }

    let _jobCheckSeq = {};
    async function checkJobStatus(name, row) {
        if (_jobCheckSeq[name]) return;
        _jobCheckSeq[name] = true;
        try {
            const data = await api('/api/hermes/jobs/read', { method: 'POST', body: { session_id: sessionId, name, offset: 0 } });
            if (row && row.isConnected) {
                const st = row.querySelector('.mini-status');
                if (st) st.textContent = data.done ? '✓' : '…';
                st.className = 'mini-status ' + (data.done ? 'done' : 'run');
            }
        } catch (e) { /* ignore */ }
        _jobCheckSeq[name] = false;
    }

    async function openJob(name) {
        let data;
        try {
            data = await api('/api/hermes/jobs/read', { method: 'POST', body: { session_id: sessionId, name, offset: 0 } });
        } catch (e) {
            showToast('Ошибка чтения задачи', 'error');
            return;
        }
        if (!data.success) {
            showToast('Ошибка: ' + (data.error || 'неизвестно'), 'error');
            return;
        }
        const text = (data.text || '').trim();
        const done = data.done;
        addMsg(done ? 'agent' : 'system', (done ? 'Задача ' + name + ' завершена. Вывод ниже.' : 'Задача ' + name + ' выполняется. Частичный вывод:') );
        if (text) {
            // убираем служебные маркеры из лога
            const clean = text
                .split('\n').filter(l => l.indexOf('__START__') !== 0 && l.indexOf('__RC__=') !== 0)
                .join('\n').trim();
            addMsg('agent', clean || '(пусто)');
        } else {
            addMsg('system', '(в логе пока пусто)');
        }
    }

    // === Скиллы ===
    async function loadSkills() {
        let data;
        try {
            data = await api('/api/hermes/skills', { method: 'POST', body: { session_id: sessionId } });
        } catch (e) {
            skillsList.innerHTML = '<div class="side-empty side-err">' + esc(e.kind === 'AUTH' ? 'Сессия истекла (403)' : 'Ошибка загрузки') + '</div>';
            return;
        }
        skillsList.innerHTML = '';
        if (!data.success || !data.entries.length) {
            skillsList.innerHTML = '<div class="side-empty">Скиллы не найдены</div>';
            updateSkillsHint();
            return;
        }
        for (const sk of data.entries) {
            if (!sk.name) continue;
            const label = document.createElement('label');
            label.className = 'skill-item';
            const chk = document.createElement('input');
            chk.type = 'checkbox';
            chk.checked = selectedSkills.has(sk.name);
            chk.addEventListener('change', () => {
                if (chk.checked) selectedSkills.add(sk.name);
                else selectedSkills.delete(sk.name);
                updateSkillsHint();
            });
            const name = document.createElement('span');
            name.textContent = sk.name;
            name.classList.add('skill-name');
            name.title = sk.description_ru || sk.description || '';
            const cat = document.createElement('span');
            cat.className = 'skill-cat';
            cat.textContent = sk.category || '';
            label.append(chk, name, cat);
            if (sk.description_ru || sk.description) {
                const info = document.createElement('button');
                info.className = 'skill-info';
                info.type = 'button';
                info.textContent = 'i';
                info.title = 'Что делает скилл';
                info.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    showSkillInfo(sk.name, sk.description_ru || sk.description, sk.category);
                });
                label.appendChild(info);
            }
            skillsList.appendChild(label);
        }
        updateSkillsHint();
    }

    function updateSkillsHint() {
        skillsHint.textContent = selectedSkills.size
            ? ('скиллы: ' + [...selectedSkills].join(', '))
            : '';
    }

    // === Документы / рабочие / медиа / промпты / журнал (живые списки через SFTP) ===

    function fmtSize(n) {
        if (!n || n < 0) return '';
        if (n < 1024) return n + ' Б';
        if (n < 1048576) return (n / 1024).toFixed(1) + ' КБ';
        return (n / 1048576).toFixed(1) + ' МБ';
    }

    function fileRow(name, fullPath, size, onClick) {
        const row = document.createElement('div');
        row.className = 'side-row file-row';
        const b = document.createElement('button');
        b.className = 'file-item';
        b.textContent = name;
        b.title = fullPath;
        b.addEventListener('click', () => onClick(fullPath));
        row.appendChild(b);
        const sz = document.createElement('span');
        sz.className = 'file-size';
        sz.textContent = fmtSize(size);
        row.appendChild(sz);
        return row;
    }

    // Листинг рабочей области Hermes. Пути считает сервер (как и для сессий/скиллов),
    // фронту знать $HOME не нужно.
    const AREAS = {
        docs: docsList,
        work: workList,
        media: mediaList,
        prompts: promptsList,
    };
    async function loadArea(area, onOpen) {
        const listEl = AREAS[area];
        if (!listEl) return;
        let data;
        try {
            data = await api('/api/hermes/dir', { method: 'POST', body: { session_id: sessionId, area } });
        } catch (e) {
            const msg = e.kind === 'AUTH' ? 'Сессия истекла (403)'
                : (e.kind === 'HTTP' ? (e.message + ' — сервер не обновлён' + (e.status === 404 ? ', перезапустите его' : ''))
                : (e.kind === 'TIMEOUT' ? 'Нет ответа от сервера' : 'Ошибка загрузки'));
            listEl.innerHTML = '<div class="side-empty side-err">' + esc(msg) + '</div>';
            return;
        }
        if (!data.success) {
            listEl.innerHTML = '<div class="side-empty side-err">' + esc(data.error || 'Ошибка') + '</div>';
            return;
        }
        listEl.innerHTML = '';
        let any = false;
        for (const g of (data.groups || [])) {
            const files = (g.entries || []).filter((en) => !en.is_dir);
            if (!files.length) continue;
            any = true;
            const head = document.createElement('div');
            head.className = 'dir-head';
            head.textContent = g.label;
            listEl.appendChild(head);
            for (const en of files) {
                listEl.appendChild(fileRow(en.name, g.path + '/' + en.name, en.size, onOpen));
            }
        }
        if (!any) listEl.innerHTML = '<div class="side-empty">Пусто</div>';
    }

    function loadDocs() { loadArea('docs', (p) => openEditor(p)); }
    function loadWork() { loadArea('work', (p) => openEditor(p)); }
    function loadMedia() { loadArea('media', (p) => downloadFile(p)); }
    function loadPrompts() { loadArea('prompts', (p) => openEditor(p)); }

    // Скачивание произвольного файла по пути (медиа) — через blob->objectURL,
    // т.к. токен живёт только в заголовке X-Auth-Token.
    async function downloadFile(path) {
        try {
            const res = await fetch('/api/files/download?path=' + encodeURIComponent(path) + '&session_id=' + encodeURIComponent(sessionId),
                { headers: { 'X-Auth-Token': authToken } });
            if (!res.ok) { showToast('Ошибка скачивания', 'error'); return; }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = (path.split('/').pop() || 'file').split('#')[0].split('?')[0];
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (e) {
            showToast('Ошибка скачивания', 'error');
        }
    }

    // === Журнал агента ===
    async function loadLog() {
        let data;
        try {
            data = await api('/api/hermes/log', { method: 'POST', body: { session_id: sessionId, lines: 80 } });
        } catch (e) {
            logView.textContent = e.kind === 'AUTH' ? 'Сессия истекла (403)' : 'Ошибка загрузки';
            return;
        }
        if (!data.success) { logView.textContent = 'Нет журнала: ' + (data.error || ''); return; }
        logView.textContent = (data.lines || []).join('\n') || '(пусто)';
    }

    // === Редактор памяти/контекста ===
    let editorPath = null;
    async function openEditor(path) {
        if (!path) {
            showToast('Путь файла ещё не определён', 'error');
            return;
        }
        editorPath = path;
        editorTitle.textContent = path;
        editorContent.value = 'Загрузка…';
        editorModal.classList.remove('hidden');
        try {
            const data = await api('/api/files/read', { method: 'POST', body: { session_id: sessionId, path } });
            editorContent.value = data.success ? data.content : '';
            if (!data.success && data.error) {
                editorContent.value = '';
                showToast(data.error + ' — можно создать файл и сохранить', '');
            }
        } catch (e) {
            editorContent.value = '';
        }
    }

    async function saveEditor() {
        if (!editorPath) return;
        try {
            const data = await api('/api/files/save', {
                method: 'POST',
                body: { session_id: sessionId, path: editorPath, content: editorContent.value },
            });
            showToast(data.success ? 'Сохранено' : ('Ошибка: ' + data.error), data.success ? 'success' : 'error');
            if (data.success) editorModal.classList.add('hidden');
        } catch (e) {
            showToast('Ошибка сохранения', 'error');
        }
    }

    // === Домашний каталог и файлы памяти ===
    // Hermes хранит состояние в ~/.hermes ТЕКУЩЕГО пользователя SSH,
    // поэтому пути строим динамически (у root — /root/.hermes, у остальных — свои).
    // Возвращает 'ok' | 'missing' | 'error'.
    async function initMemoryButtons() {
        let data;
        try {
            data = await api('/api/hermes/home', { method: 'POST', body: { session_id: sessionId } });
        } catch (e) {
            return 'error';
        }
        const memBtns = [...document.querySelectorAll('.mem-btn')];
        if (!data || !data.success || !data.home) {
            for (const b of memBtns) b.disabled = true;
            showToast('Не удалось определить домашний каталог', 'error');
            return 'error';
        }
        if (!data.exists) {
            document.getElementById('ni-text').textContent =
                'Для этого пользователя на сервере нет каталога ' + data.hermes_dir + '.';
            return 'missing';
        }
        hermesDir = data.hermes_dir;
        hermesUser = data.user || '';
        for (const b of memBtns) {
            b.disabled = false;
            b.title = hermesDir + '/' + b.dataset.file;
        }
        return 'ok';
    }

    // === Сводка модели в тулбаре ===
    async function loadConfig() {
        let data;
        try {
            data = await api('/api/hermes/config/read', { method: 'POST', body: { session_id: sessionId } });
        } catch (e) {
            configSummary.textContent = '';
            return;
        }        if (!data.success) {
            configSummary.textContent = '';
            return;
        }
        const parts = [];
        if (data.provider) parts.push(data.provider);
        if (data.model) parts.push(data.model);
        configSummary.textContent = parts.length ? parts.join(' / ') : '';
    }

    // === Пейсер-прокси (ограничение 60 req/мин) ===
    async function refreshPacer() {
        let st;
        try {
            st = await api('/api/hermes/pacer', { method: 'POST', body: { session_id: sessionId } });
        } catch (e) {
            pacerInd.textContent = '⚪';
            pacerInd.title = 'Пейсер: нет данных';
            return;
        }
        if (!st || st.execute_ok === false || !st.raw) {
            pacerInd.textContent = '⚪';
            pacerInd.title = 'Пейсер: не определён';
            return;
        }
        const isOn = !!st.running;
        pacerInd.textContent = isOn ? '🟢' : '🔴';
        pacerInd.title = [
            isOn ? 'Пейсер активен (60 req/мин)' : 'Пейсер не запущен',
            'порт: ' + (st.port || '?'),
            'base_url: ' + (st.base_url || '?'),
            'upstream: ' + (st.upstream || '?'),
            'клик — проверить',
        ].join('\n');
    }

    // === Статус SSH-сессии ===
    async function refreshStatus() {
        try {
            const res = await fetch('/api/status?session_id=' + encodeURIComponent(sessionId),
                { headers: { 'X-Auth-Token': authToken } });
            const data = await res.json();
            statusText.textContent = data.active ? '🟢 Онлайн' : '🔴 Сессия закрыта';
        } catch (e) {
            statusText.textContent = '🔴 Нет связи';
        }
    }

    // === События ===
    sendBtn.addEventListener('click', send);
    stopBtn.addEventListener('click', stopJob);
    pacerInd.addEventListener('click', refreshPacer);
    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            send();
        }
    });
    document.getElementById('new-chat-btn').addEventListener('click', () => {
        setSession(null);
        addMsg('system', 'Новый чат. Следующее сообщение агент получит без прошлой истории.');
    });
    document.getElementById('reload-sessions').addEventListener('click', loadSessions);
    document.getElementById('reload-skills').addEventListener('click', loadSkills);
    document.getElementById('reload-jobs').addEventListener('click', loadJobs);
    if (document.getElementById('reload-docs')) document.getElementById('reload-docs').addEventListener('click', loadDocs);
    if (document.getElementById('reload-work')) document.getElementById('reload-work').addEventListener('click', loadWork);
    if (document.getElementById('reload-media')) document.getElementById('reload-media').addEventListener('click', loadMedia);
    if (document.getElementById('reload-prompts')) document.getElementById('reload-prompts').addEventListener('click', loadPrompts);
    if (document.getElementById('reload-log')) document.getElementById('reload-log').addEventListener('click', loadLog);
    // Кнопки обновления внутри заголовков-спойлеров не должны раскрывать/сворачивать секцию
    document.querySelectorAll('details.side-section > summary').forEach((sum) => {
        sum.addEventListener('click', (e) => {
            if (e.target.closest('button')) e.stopPropagation();
        });
    });
    document.getElementById('editor-close').addEventListener('click', () => editorModal.classList.add('hidden'));
    document.getElementById('editor-save').addEventListener('click', saveEditor);
    // Кнопки памяти всегда кликабельны: путь добирается лениво при первом
    // клике (если /api/hermes/home ещё не успел ответить или ошибочен).
    async function openMemoryFile(btn) {
        if (!hermesDir) await initMemoryButtons();
        if (!hermesDir) {
            showToast('Не удалось определить каталог ~/.hermes', 'error');
            return;
        }
        openEditor(hermesDir + '/' + btn.dataset.file);
    }
    for (const btn of document.querySelectorAll('.mem-btn')) {
        btn.addEventListener('click', () => openMemoryFile(btn));
    }

    // === Приветствие ===
    async function showWelcome() {
        let name = hermesUser;
        if (!name) {
            try {
                const d = await api('/api/hermes/home', { method: 'POST', body: { session_id: sessionId } });
                if (d && d.success) name = hermesUser = d.user || '';
            } catch (e) { /* имена не критичны */ }
        }
        const w = document.createElement('div');
        w.className = 'msg welcome';
        const big = document.createElement('div');
        big.className = 'welcome-hello';
        big.textContent = 'Привет, ' + (name || 'друг') + '!';
        const sub = document.createElement('div');
        sub.className = 'welcome-sub';
        sub.textContent = 'Рад тебя видеть! Чем займёмся сегодня?';
        w.append(big, sub);
        chatLog.appendChild(w);
        chatLog.scrollTop = chatLog.scrollHeight;
    }

    // === Инициализация ===
    setStatusInit();
    function setStatusInit() {
        statusText.textContent = '🟢 Онлайн';
    }
    // Приветствие в центре окна чата; списки сайдбара грузятся сразу, не дожидаясь
    // медленного /api/hermes/home — иначе приветствие задерживает наполнение.
    showWelcome();
    loadSessions();
    loadSkills();
    loadJobs();
    loadConfig();
    refreshStatus();
    refreshPacer();
    setInterval(loadJobs, 8000);
    setInterval(loadSessions, 20000);
    setInterval(loadSkills, 30000);
    setInterval(refreshStatus, 5000);
    setInterval(refreshPacer, 45000);
    chatInput.focus();    // Живые списки по рабочим каталогам Hermes — самодостаточны:
    // сами получают ~/.hermes через /api/hermes/home, поэтому стартуют сразу
    // и не зависят от исхода инициализации памяти.
    loadDocs();
    loadWork();
    loadMedia();
    loadPrompts();
    loadLog();
    setInterval(loadDocs, 30000);
    setInterval(loadWork, 30000);
    setInterval(loadMedia, 30000);
    setInterval(loadPrompts, 30000);
    setInterval(loadLog, 10000);
    // Проверка наличия hermes и настройка кнопок памяти — отдельно, не блокирует ничто.
    (async () => {
        const state = await initMemoryButtons();
        if (state === 'missing') {
            document.getElementById('toolbar').classList.add('hidden');
            document.getElementById('hermes-app').classList.add('hidden');
            document.getElementById('not-installed').classList.remove('hidden');
        }
    })();
})();
