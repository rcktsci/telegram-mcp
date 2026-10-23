"""
UI сервер для управления чатами.
"""
import asyncio
import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_session_factory
from src.db.models import AuthToken, Chat, Message
from src.db.repositories import AuthTokenRepository
from src.telegram.folders import fetch_folders

logger = logging.getLogger(__name__)

ui_app = FastAPI(title="Telegram MCP UI")


class TokenUpdate(BaseModel):
    comment: str
    folder_ids: list[int] = []
    chat_ids: list[int] = []


def _token_to_dict(token: AuthToken) -> dict:
    """Wire shape of /api/tokens (fixed contract, PLAN_AUTH_TOKENS)."""
    return {
        "id": token.id,
        "token": token.token,
        "comment": token.comment,
        "folder_ids": token.folder_ids,
        "chat_ids": token.chat_ids,
        "created_at": token.created_at.isoformat(),
    }

# Папки Telegram (dialog filters) живут в аккаунте и меняются редко;
# кэш с TTL спасает UI от лишнего RPC при каждом рендере списка.
_folders_cache: dict = {}
_FOLDERS_TTL_SECONDS = 60


class ChatUpdate(BaseModel):
    watching: bool


class ChatResponse(BaseModel):
    id: int
    chat_id: int
    title: str
    watching: bool
    is_group: bool
    is_private: bool
    min_message_id: Optional[int] = None
    max_message_id: Optional[int] = None
    message_count: Optional[int] = None


@ui_app.get("/", response_class=HTMLResponse)
async def ui_root():
    """Отдать HTML страницу UI."""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telegram MCP - Управление чатами</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #333; margin-bottom: 20px; }
        .filters { background: white; padding: 15px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .filters input, .filters select { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .filters input { width: 300px; }
        .filters select { margin-left: 10px; }
        .chats { background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .chat-row { display: grid; grid-template-columns: 40px 1fr 80px 100px 100px 80px; gap: 10px; padding: 15px; border-bottom: 1px solid #eee; align-items: center; }
        .chat-row:hover { background: #f9f9f9; }
        .chat-row.header { font-weight: 600; color: #666; font-size: 13px; text-transform: uppercase; }
        .chat-title { font-weight: 500; color: #333; }
        .chat-id { color: #999; font-size: 12px; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500; }
        .badge-group { background: #e3f2fd; color: #1976d2; }
        .badge-private { background: #f3e5f5; color: #7b1fa2; }
        .toggle { position: relative; width: 50px; height: 26px; }
        .toggle input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #ccc; transition: .3s; border-radius: 26px; }
        .slider:before { position: absolute; content: ""; height: 20px; width: 20px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; }
        input:checked + .slider { background-color: #4caf50; }
        input:checked + .slider:before { transform: translateX(24px); }
        .btn { padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; transition: .2s; }
        .btn-danger { background: #f44336; color: white; }
        .btn-danger:hover { background: #d32f2f; }
        .btn-danger:disabled { background: #ccc; cursor: not-allowed; }
        .stats { color: #666; font-size: 13px; }
        .loading { text-align: center; padding: 40px; color: #666; }
        .error { background: #ffebee; color: #c62828; padding: 15px; border-radius: 8px; margin-bottom: 20px; }
        .success { background: #e8f5e9; color: #2e7d32; padding: 10px; border-radius: 4px; margin-bottom: 10px; }
        .bulk-btn { padding: 8px 14px; border: none; border-radius: 4px; background: #4caf50; color: white; font-size: 14px; cursor: pointer; margin-top: 2px; }
        .bulk-btn:hover { background: #43a047; }
        .bulk-btn:disabled { background: #cccccc; cursor: not-allowed; }
        /* Токены: кнопка в шапке + модал (таблица/форма, одностраничный стиль). */
        .header-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
        .btn-primary { padding: 8px 14px; border: none; border-radius: 4px; background: #1976d2; color: white; font-size: 14px; cursor: pointer; white-space: nowrap; }
        .btn-primary:hover { background: #1565c0; }
        .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 100; overflow-y: auto; padding: 20px; }
        .modal-overlay.open { display: block; }
        .modal { background: white; border-radius: 8px; max-width: 860px; margin: 30px auto; padding: 20px; box-shadow: 0 8px 30px rgba(0,0,0,.25); }
        .modal h2 { margin-top: 0; }
        .token-row { display: grid; grid-template-columns: minmax(220px, 1.2fr) 1fr minmax(140px, .8fr) 160px 140px; gap: 10px; padding: 12px; border-bottom: 1px solid #eee; align-items: center; font-size: 13px; }
        .token-row:hover { background: #f9f9f9; }
        .token-row.header { font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; }
        .token-value { font-family: monospace; font-size: 12px; user-select: all; word-break: break-all; }
        .token-copy { padding: 2px 8px; font-size: 12px; }
        .token-folders { color: #555; }
        .token-created { color: #999; }
        .modal-form { background: #f7f7f7; border-radius: 6px; padding: 14px; margin-top: 14px; }
        .modal-form label { display: block; font-size: 13px; color: #555; margin: 8px 0 4px; }
        .modal-form input, .modal-form select { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        .modal-form .hint { font-size: 12px; color: #888; margin-top: 4px; }
        .modal-actions { margin-top: 12px; display: flex; gap: 8px; }
        .btn-secondary { background: #eee; color: #333; }
        @media (max-width: 720px) { .token-row { grid-template-columns: 1fr; } }
        /* Mobile: только <=720px, десктоп не затронут. Ячейки размечены
           классами c-*, на узких экранах строка складывается в компактную
           карточку 2х2: заголовок+тумблер / счётчик+кнопка. */
        @media (max-width: 720px) {
            body { padding: 8px; }
            h1 { font-size: 20px; margin-bottom: 12px; }
            .filters { padding: 10px; }
            .filters input, .filters select { width: 100%; margin-left: 0; margin-top: 6px; }
            .chat-row {
                grid-template-columns: 1fr auto;
                grid-template-areas:
                    "title watch"
                    "count action";
                gap: 8px 12px;
                padding: 12px;
            }
            .c-id, .c-type { display: none; }
            .c-title { grid-area: title; }
            .c-count { grid-area: count; }
            .c-watch { grid-area: watch; }
            .c-action { grid-area: action; justify-self: end; }
            .filters .bulk-btn { width: 100%; margin-left: 0; margin-top: 6px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-row">
            <h1>🤖 Telegram MCP - Управление чатами</h1>
            <div style="display:flex; gap:8px;">
                <button class="btn-primary" onclick="openTokensModal()">🔑 Токены</button>
                <button class="btn btn-secondary" id="logout-btn" onclick="doLogout()">Выйти</button>
            </div>
        </div>
        <div id="error" class="error" style="display:none;"></div>
        <div id="success" class="success" style="display:none;"></div>
        
        <div class="filters">
            <input type="text" id="search" placeholder="🔍 Поиск по названию..." oninput="filterChats()">
            <select id="filter-type" onchange="filterChats()">
                <option value="">Все типы</option>
                <option value="group">Группы</option>
                <option value="private">Личные</option>
            </select>
            <select id="filter-watching" onchange="filterChats()">
                <option value="">Все статусы</option>
                <option value="true">Watching ✅</option>
                <option value="false">Не watching ❌</option>
            </select>
            <select id="filter-folder" onchange="filterChats()">
                <option value="0">Все папки</option>
            </select>
            <select id="sort" onchange="filterChats()">
                <option value="title">Сортировка: по названию</option>
                <option value="date">по дате сообщения</option>
                <option value="id">по ID</option>
                <option value="messages">по кол-ву сообщений</option>
            </select>
            <button id="bulk-watch" class="bulk-btn" onclick="bulkWatch()">✅ Отметить всё</button>
        </div>
        
        <div class="chats">
            <div class="chat-row header">
                <span class="c-id">ID</span>
                <span class="c-title">Чат</span>
                <span class="c-type">Тип</span>
                <span class="c-count">Сообщения</span>
                <span class="c-watch">Watching</span>
                <span class="c-action">Действия</span>
            </div>
            <div id="chat-list"></div>
        </div>
    </div>

    <div id="login-overlay" class="modal-overlay" style="background:#f5f5f5; z-index:200;">
        <div class="modal" style="max-width:380px; margin-top:15vh;">
            <h2>🔒 Вход</h2>
            <div class="modal-form">
                <div id="login-error" class="error" style="display:none; margin-bottom:10px;"></div>
                <label>Мастер-токен</label>
                <input type="password" id="login-token" autocomplete="off">
                <div class="hint">Токен хранится только в localStorage этого браузера и не попадает в страницу.</div>
                <div class="modal-actions">
                    <button class="btn-primary" onclick="doLogin(this)">Войти</button>
                </div>
            </div>
        </div>
    </div>

    <div id="tokens-modal" class="modal-overlay" onclick="if (event.target === this) closeTokensModal()">
        <div class="modal">
            <div class="header-row">
                <h2>🔑 Токены доступа</h2>
                <div style="display:flex; gap:8px;">
                    <button class="btn-primary" onclick="showTokenForm()">➕ Создать</button>
                    <button class="btn btn-secondary" onclick="closeTokensModal()">✕ Закрыть</button>
                </div>
            </div>
            <div id="token-form" class="modal-form" style="display:none;">
                <label>Назначение токена (comment)</label>
                <input type="text" id="token-comment" placeholder="например: CI-агент">
                <label>Папки (доступ = смотримые чаты папки)</label>
                <select id="token-folders" multiple size="6"></select>
                <div class="hint">Права = объединение выбранных папок. Состав папок живёт в Telegram:
                если папку сузили/удалили — доступ токена сузится следом. Отдельные chat_ids через UI не редактируются.</div>
                <div class="modal-actions">
                    <button class="btn-primary" id="token-save" onclick="saveToken()">Сохранить</button>
                    <button class="btn btn-secondary" onclick="hideTokenForm()">Отмена</button>
                </div>
            </div>
            <div class="token-row header">
                <span>Токен</span><span>Назначение</span><span>Папки</span><span>Создан</span><span></span>
            </div>
            <div id="token-list"></div>
        </div>
    </div>

    <script>
        let allChats = [];
        let folders = [];
        
        async function loadFolders() {
            try {
                const response = await apiFetch('/api/folders');
                if (!response.ok) throw new Error('Failed to load folders');
                folders = await response.json();
            } catch (e) {
                console.warn('Папки недоступны: ' + e.message);
                folders = [];
            }
            const sel = document.getElementById('filter-folder');
            const current = sel.value;
            sel.innerHTML = '';
            const all = document.createElement('option');
            all.value = '0';
            all.textContent = 'Все папки';
            sel.appendChild(all);
            for (const f of folders) {
                const opt = document.createElement('option');
                opt.value = String(f.folder_id);
                opt.textContent = f.title + ' (' + f.chat_ids.length + ')';
                sel.appendChild(opt);
            }
            // Восстановим выбор, если пункт ещё существует.
            if (current && [...sel.options].some(o => o.value === current)) {
                sel.value = current;
            }
        }
        
        // Папка Фильтрует по membership из dialog filters (telegram_chat_id).
        function folderAllows(chat) {
            const folderVal = document.getElementById('filter-folder').value;
            if (!folderVal || folderVal === '0') return true;
            const folder = folders.find(f => String(f.folder_id) === folderVal);
            // Папка недоступна (не загрузились) — не режем список.
            if (!folder) return true;
            return folder.chat_ids.includes(chat.chat_id);
        }
        
        async function loadChats() {
            try {
                const response = await apiFetch('/api/chats');
                if (!response.ok) throw new Error('Failed to load chats');
                allChats = await response.json();
                filterChats();
            } catch (e) {
                showError(e.message);
            }
        }
        
        // Общая фильтрация (search/type/watching/folder) — единая для
        // рендера и для батчевой «отметить всё в отфильтрованном».
        function filteredChats() {
            const search = document.getElementById('search').value.toLowerCase();
            const type = document.getElementById('filter-type').value;
            const watching = document.getElementById('filter-watching').value;
            return allChats.filter(chat => {
                const matchSearch = chat.title.toLowerCase().includes(search);
                const matchType = !type || (type === 'group' && chat.is_group) || (type === 'private' && chat.is_private);
                const matchWatching = !watching || (watching === 'true' && chat.watching) || (watching === 'false' && !chat.watching);
                return matchSearch && matchType && matchWatching && folderAllows(chat);
            });
        }
        
        function filterChats() {
            const sort = document.getElementById('sort').value;
            const filtered = filteredChats();
            
            // Сортировка
            if (sort === 'title') filtered.sort((a, b) => a.title.localeCompare(b.title));
            else if (sort === 'date') filtered.sort((a, b) => new Date(b.max_message_date || 0) - new Date(a.max_message_date || 0));
            else if (sort === 'id') filtered.sort((a, b) => a.id - b.id);
            else if (sort === 'messages') filtered.sort((a, b) => (b.message_count || 0) - (a.message_count || 0));
            
            // Кнопка батч-отмечания: столько, сколько реально поменяется.
            // Безопасность: без конкретной выбранной папки кнопка выключена —
            // случайный тап не должен разом включить watching всему списку.
            const folderVal = document.getElementById('filter-folder').value;
            const noFolder = !folderVal || folderVal === '0';
            const pending = filtered.filter(c => !c.watching).length;
            const bulkBtn = document.getElementById('bulk-watch');
            if (bulkBtn) {
                bulkBtn.disabled = noFolder || pending === 0;
                bulkBtn.textContent = noFolder ? '✅ Выберите папку'
                    : pending > 0 ? `✅ Отметить всё (${pending})` : '✅ Всё отмечено';
            }
            
            renderChats(filtered);
        }
        
        function renderChats(chats) {
            const container = document.getElementById('chat-list');
            if (chats.length === 0) {
                container.innerHTML = '<div class="loading">Чаты не найдены</div>';
                return;
            }
            
            container.innerHTML = chats.map(chat => `
                <div class="chat-row" data-id="${chat.id}">
                    <span class="c-id">${chat.id}</span>
                    <div class="c-title">
                        <div class="chat-title">${escapeHtml(chat.title)}</div>
                        <div class="chat-id">TG: ${chat.chat_id} ${chat.user_id ? '👤' + chat.user_id : ''}</div>
                    </div>
                    <span class="c-type">
                        ${chat.is_group ? '<span class="badge badge-group">Группа</span>' : ''}
                        ${chat.is_private ? '<span class="badge badge-private">Личный</span>' : ''}
                    </span>
                    <span class="stats c-count">${chat.message_count || 0} сообщений</span>
                    <label class="c-watch toggle">
                        <input type="checkbox" ${chat.watching ? 'checked' : ''} onchange="toggleWatching(${chat.id}, this.checked)">
                        <span class="slider"></span>
                    </label>
                    <button class="c-action btn btn-danger" ${chat.watching ? 'disabled' : ''} onclick="clearHistory(${chat.id})">
                        Очистить
                    </button>
                </div>
            `).join('');
        }
        
        async function toggleWatching(id, watching) {
            // Оптимистично: checkbox уже показывает кликнутое состояние, и
            // ПОЛНАЯ перезагрузка списка здесь is banned by design - параллельные
            // loadChats() приходят в произвольном порядке, и более ранний
            // ответ перезаписывает свежие тумблеры ("скидывание" при
            // пакетном отмечании). Обновляем только локальный стейт;
            // полный ресинк - только по ошибке или по F5.
            try {
                const response = await apiFetch(`/api/chats/${id}/watching`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({watching})
                });
                if (!response.ok) throw new Error('Failed to update');
                const chat = allChats.find(c => c.id === id);
                if (chat) chat.watching = watching;
                showSuccess(`Watching ${watching ? 'включен' : 'выключен'}`);
            } catch (e) {
                showError(e.message);
                loadChats(); // ошибка/неопределённость - синк с сервером
            }
        }
        
        // Массовое включение watching для всех отфильтрованных (ещё не
        // watching) чатов. PATCH-и идут последовательно с паузой: каждый
        // запускает на сервере задачу догрузки истории — сотня одновременных
        // задач перегрузит telethon-клиент. Прогресс рисуем на кнопке,
        // стейт держим локально (тот же принцип, что и у одиночного тумблера).
        async function bulkWatch() {
            // Страховка на папках: массовое включение разрешено только при
            // выбранной папке (данное условие повторяет disabled кнопки).
            const folderVal = document.getElementById('filter-folder').value;
            if (!folderVal || folderVal === '0') return;
            const targets = filteredChats().filter(c => !c.watching);
            if (targets.length === 0) return;
            if (!confirm(`Включить watching для ${targets.length} чат(ов)? Каждому стартует догрузка истории.`)) return;
            
            const bulkBtn = document.getElementById('bulk-watch');
            bulkBtn.disabled = true;
            let ok = 0, failed = 0;
            for (const chat of targets) {
                try {
                    const response = await apiFetch(`/api/chats/${chat.id}/watching`, {
                        method: 'PATCH',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({watching: true})
                    });
                    if (!response.ok) throw new Error('Failed to update');
                    chat.watching = true;
                    ok++;
                } catch (e) {
                    failed++;
                }
                bulkBtn.textContent = `⏳ ${ok + failed} / ${targets.length}...`;
                if (ok + failed < targets.length) await new Promise(r => setTimeout(r, 150));
            }
            bulkBtn.disabled = false;
            if (failed > 0) showError(`Не отметилось чатов: ${failed}`); else showSuccess(`Отмечено: ${ok}`);
            filterChats(); // локальный стейт уже актуален: обновить бейджи и счётчик кнопки
        }
        
        async function clearHistory(id) {
            if (!confirm('Вы уверены? Это удалит все сообщения из этого чата.')) return;
            try {
                const response = await apiFetch(`/api/chats/${id}/messages`, {method: 'DELETE'});
                if (!response.ok) throw new Error('Failed to clear');
                showSuccess('История очищена');
                loadChats();
            } catch (e) {
                showError(e.message);
            }
        }
        
        function showError(msg) {
            const el = document.getElementById('error');
            el.textContent = '❌ ' + msg;
            el.style.display = 'block';
            setTimeout(() => el.style.display = 'none', 5000);
        }
        
        function showSuccess(msg) {
            const el = document.getElementById('success');
            el.textContent = '✅ ' + msg;
            el.style.display = 'block';
            setTimeout(() => el.style.display = 'none', 3000);
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // ===== Мастер-токен (PLAN_UI_MASTER_TOKEN): логин + заголовок на все fetch. =====
        const MASTER_KEY = 'ui_master_token';

        function authHeaders() {
            const token = localStorage.getItem(MASTER_KEY);
            return token ? {'Authorization': 'Bearer ' + token} : {};
        }

        function showLogin() {
            document.getElementById('login-overlay').classList.add('open');
            document.getElementById('logout-btn').style.display = 'none';
            // Общий #error лежит под непрозрачным оверлеем — у логина свой блок.
            document.getElementById('login-error').style.display = 'none';
            const input = document.getElementById('login-token');
            input.value = '';
            input.focus();
        }

        function showLoginError(msg) {
            const el = document.getElementById('login-error');
            el.textContent = '❌ ' + msg;
            el.style.display = 'block';
        }

        function hideLogin() {
            document.getElementById('login-overlay').classList.remove('open');
            document.getElementById('logout-btn').style.display = '';
        }

        async function doLogin(btn) {
            const candidate = document.getElementById('login-token').value;
            if (!candidate) { showLoginError('Введите мастер-токен'); return; }

            // Валидируем кандидата ДО сохранения: сохранение любого чушь-значения
            // «впускало» (оверлей прятался), а данные падали 401. Проба —
            // GET /api/tokens: 200/401 только от БД, без зависимости от Telegram
            // (в отличие от /api/folders, отдающего 503 без кэша).
            btn.disabled = true;
            try {
                const probe = await fetch('/api/tokens', {
                    headers: {'Authorization': 'Bearer ' + candidate},
                });
                if (probe.status === 401) {
                    showLoginError('Неверный мастер-токен');
                    document.getElementById('login-token').value = '';
                    return;
                }
                if (!probe.ok) throw new Error('Не удалось проверить мастер-токен');
            } catch (e) {
                showLoginError(e.message);
                return;
            } finally {
                btn.disabled = false;
            }

            // Токен живёт только в localStorage: в HTML-интерполяции его нет (XSS).
            localStorage.setItem(MASTER_KEY, candidate);
            document.getElementById('login-token').value = '';
            hideLogin();
            loadChats();
            loadFolders();
        }

        function doLogout() {
            localStorage.removeItem(MASTER_KEY);
            showLogin();
        }

        // Единая точка: добавляет Bearer и любой 401 от API снова показывает логин.
        async function apiFetch(url, options = {}) {
            options.headers = Object.assign({}, authHeaders(), options.headers || {});
            const response = await fetch(url, options);
            if (response.status === 401) showLogin();
            return response;
        }

        if (!localStorage.getItem(MASTER_KEY)) showLogin();

        // ===== Токены доступа (PLAN_AUTH_TOKENS): CRUD + попап. =====
        let editingTokenId = null;

        function folderTitle(folderId) {
            // Папка могла исчезнуть из живого /api/folders — показываем id.
            const folder = folders.find(f => String(f.folder_id) === String(folderId));
            return folder ? folder.title : '#' + folderId;
        }

        async function openTokensModal() {
            document.getElementById('tokens-modal').classList.add('open');
            hideTokenForm();
            await loadTokens();
        }

        function closeTokensModal() {
            document.getElementById('tokens-modal').classList.remove('open');
        }

        async function loadTokens() {
            try {
                const response = await apiFetch('/api/tokens');
                if (!response.ok) throw new Error('Failed to load tokens');
                renderTokens(await response.json());
            } catch (e) {
                showError(e.message);
            }
        }

        function renderTokens(tokens) {
            const container = document.getElementById('token-list');
            if (tokens.length === 0) {
                container.innerHTML = '<div class="loading">Токенов пока нет</div>';
                return;
            }
            container.innerHTML = tokens.map(t => `
                <div class="token-row">
                    <div class="token-value">${t.token}
                        <button class="btn btn-secondary token-copy" onclick="copyToken('${t.token}')">📋</button>
                    </div>
                    <div>${escapeHtml(t.comment || '')}</div>
                    <div class="token-folders">${t.folder_ids.map(id => escapeHtml(folderTitle(id))).join(', ') || '—'}</div>
                    <div class="token-created">${t.created_at ? new Date(t.created_at).toLocaleString() : ''}</div>
                    <div>
                        <button class="btn btn-secondary" onclick="showTokenForm(${t.id})">✏️</button>
                        <button class="btn btn-danger" onclick="deleteToken(${t.id})">🗑</button>
                    </div>
                </div>
            `).join('');
        }

        async function copyToken(token) {
            try {
                await navigator.clipboard.writeText(token);
                showSuccess('Токен скопирован');
            } catch (e) {
                showError('Копирование недоступно — выделите токен вручную');
            }
        }

        function fillFolderMultiselect(selectedIds) {
            const sel = document.getElementById('token-folders');
            sel.innerHTML = '';
            for (const f of folders) {
                const opt = document.createElement('option');
                opt.value = String(f.folder_id);
                opt.textContent = `${f.title} (${f.chat_ids.length})`;
                if (selectedIds.includes(f.folder_id)) opt.selected = true;
                sel.appendChild(opt);
            }
        }

        function showTokenForm(tokenId) {
            editingTokenId = tokenId || null;
            const form = document.getElementById('token-form');
            const commentInput = document.getElementById('token-comment');
            if (tokenId) {
                // chat_ids из UI не редактируются (ультра-урезание): PATCH
                // возвращает их как есть, форма отправляет сохранённые.
                apiFetch('/api/tokens').then(r => r.json()).then(tokens => {
                    const token = tokens.find(t => t.id === tokenId);
                    if (!token) { showError('Токен не найден'); return; }
                    commentInput.value = token.comment || '';
                    fillFolderMultiselect(token.folder_ids);
                    form.style.display = 'block';
                }).catch(e => showError(e.message));
            } else {
                commentInput.value = '';
                fillFolderMultiselect([]);
                form.style.display = 'block';
            }
            commentInput.focus();
        }

        function hideTokenForm() {
            document.getElementById('token-form').style.display = 'none';
            editingTokenId = null;
        }

        async function saveToken() {
            const comment = document.getElementById('token-comment').value.trim();
            const folderIds = [...document.getElementById('token-folders').selectedOptions].map(o => Number(o.value));
            if (!comment) { showError('Укажите назначение токена'); return; }

            // Всё сетевое — внутри try: сбой перечитывания chat_ids не должен
            // оставлять молча неработающую кнопку.
            try {
                let url = '/api/tokens', method = 'POST';
                const body = {comment, folder_ids: folderIds, chat_ids: []};
                if (editingTokenId) {
                    // chat_ids сохраняем как есть: UI их не редактирует.
                    const tokens = await (await apiFetch('/api/tokens')).json();
                    const token = tokens.find(t => t.id === editingTokenId);
                    if (!token) { showError('Токен не найден'); return; }
                    url = `/api/tokens/${editingTokenId}`;
                    method = 'PATCH';
                    body.chat_ids = token.chat_ids || [];
                }
                const response = await apiFetch(url, {
                    method,
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                });
                if (!response.ok) throw new Error('Failed to save token');
                showSuccess(editingTokenId ? 'Токен обновлён' : 'Токен создан');
                hideTokenForm();
                loadTokens();
            } catch (e) {
                showError(e.message);
            }
        }

        async function deleteToken(tokenId) {
            if (!confirm('Удалить токен? Агенты с ним получат 401 сразу.')) return;
            try {
                const response = await apiFetch(`/api/tokens/${tokenId}`, {method: 'DELETE'});
                if (!response.ok) throw new Error('Failed to delete token');
                showSuccess('Токен удалён');
                loadTokens();
            } catch (e) {
                showError(e.message);
            }
        }

        loadChats();
        loadFolders();
    </script>
</body>
</html>
    """)


@ui_app.get("/api/tokens")
async def get_tokens():
    """Список токенов (новые сверху), plaintext по решению владельца."""
    async with get_session_factory()() as session:
        tokens = await AuthTokenRepository(session).get_all()
    return [_token_to_dict(t) for t in tokens]


@ui_app.post("/api/tokens", status_code=201)
async def create_token(data: TokenUpdate):
    """Создать токен: UUID4 генерирует сервер."""
    async with get_session_factory()() as session:
        token = await AuthTokenRepository(session).create(
            comment=data.comment, folder_ids=data.folder_ids, chat_ids=data.chat_ids,
        )
    return _token_to_dict(token)


@ui_app.patch("/api/tokens/{token_id}")
async def update_token(token_id: int, data: TokenUpdate):
    """Полная замена comment/folder_ids/chat_ids (контракт PLAN_AUTH_TOKENS)."""
    async with get_session_factory()() as session:
        updated = await AuthTokenRepository(session).update(
            token_id, comment=data.comment, folder_ids=data.folder_ids, chat_ids=data.chat_ids,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Token not found")
    return _token_to_dict(updated)


@ui_app.delete("/api/tokens/{token_id}")
async def delete_token(token_id: int):
    async with get_session_factory()() as session:
        deleted = await AuthTokenRepository(session).delete(token_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"status": "ok"}


@ui_app.get("/api/chats")
async def get_chats():
    """Получить список всех чатов."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(select(Chat).order_by(Chat.title))
        chats = result.scalars().all()
        
        # Получим количество сообщений для каждого чата
        chat_tg_ids = [chat.chat_id for chat in chats]
        if chat_tg_ids:
            count_result = await session.execute(
                select(Message.chat_id, func.count(Message.id))
                .where(Message.chat_id.in_(chat_tg_ids))
                .group_by(Message.chat_id)
            )
            # Маппинг: TG chat_id -> count
            tg_to_count = dict(count_result.all())
            # Создаём маппинг Chat.id -> count
            id_to_count = {}
            for chat in chats:
                id_to_count[chat.id] = tg_to_count.get(chat.chat_id, 0)
        else:
            id_to_count = {}
        
        return [
            {
                "id": chat.id,
                "chat_id": chat.chat_id,
                "title": chat.title,
                "watching": chat.watching,
                "is_group": chat.is_group,
                "is_private": chat.is_private,
                "user_id": chat.user_id,
                "min_message_id": chat.min_message_id,
                "max_message_id": chat.max_message_id,
                "min_message_date": chat.min_message_date.isoformat() if chat.min_message_date else None,
                "max_message_date": chat.max_message_date.isoformat() if chat.max_message_date else None,
                "message_count": id_to_count.get(chat.id, 0)
            }
            for chat in chats
        ]


@ui_app.patch("/api/chats/{chat_id}/watching")
async def update_watching(chat_id: int, data: ChatUpdate):
    """Обновить статус watching для чата."""
    # ВАЖНО: не `from main import ...` — main при запуске как __main__ был бы
    # повторно исполнен в новом неймспейсе, где менеджер ещё None, и тумблер
    # молча пропускал бы загрузку истории (баг без единой строки в логах).
    from src.runtime import get_worker_manager

    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            update(Chat)
            .where(Chat.id == chat_id)
            .values(watching=data.watching)
            .returning(Chat.chat_id, Chat.user_id)
        )
        row = result.one_or_none()
        if row is None:
            await session.rollback()
            raise HTTPException(status_code=404, detail="Chat not found")
        await session.commit()
        telegram_chat_id = row.chat_id
        user_id = row.user_id

    if data.watching:
        wm = get_worker_manager()
        if wm:
            if not user_id:
                # Ownerless chat (added before ownership tracking): bind it to an
                # available account, otherwise history loading is skipped forever.
                user_id = wm.pick_user_id()
                if user_id:
                    async with session_factory() as session:
                        await session.execute(
                            update(Chat)
                            .where(Chat.id == chat_id)
                            .values(user_id=user_id)
                        )
                        await session.commit()
            if user_id:
                asyncio.create_task(wm.load_history(telegram_chat_id, user_id))
            else:
                logger.warning(f"Watching enabled for chat {chat_id} but no account available to own it")

    return {"status": "ok", "watching": data.watching}


@ui_app.post("/api/sweep/run")
async def run_history_sweep():
    """Запустить history sweep немедленно, не дожидаясь очередного тика."""
    # Тот же runtime-менеджер, что у соседних endpoint'ов (не `from main import ...`).
    from src.runtime import get_worker_manager

    wm = get_worker_manager()
    if not wm:
        raise HTTPException(status_code=503, detail="Worker manager is not running")
    wm.request_sweep_now()
    return {"triggered": True}


@ui_app.delete("/api/chats/{chat_id}/messages")
async def clear_chat_messages(chat_id: int):
    """Удалить все сообщения чата (только если watching=False)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        # Проверим что watching=False
        result = await session.execute(select(Chat.watching).where(Chat.id == chat_id))
        watching = result.scalar_one_or_none()
        
        if watching is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        if watching:
            raise HTTPException(status_code=400, detail="Cannot clear messages for watching chat")
        
        # Получим Telegram chat_id по internal id
        chat_result = await session.execute(select(Chat.chat_id).where(Chat.id == chat_id))
        tg_chat_id = chat_result.scalar_one_or_none()
        if tg_chat_id is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        # Удалим сообщения по Telegram chat_id
        await session.execute(delete(Message).where(Message.chat_id == tg_chat_id))
        await session.commit()

    return {"status": "ok"}


@ui_app.get("/api/folders")
async def get_folders():
    """Папки Telegram (dialog filters) + состав их чатов для фильтра в UI."""
    cached = _folders_cache.get("data")
    fetched_at = _folders_cache.get("ts", 0)

    if cached and time.time() - fetched_at < _FOLDERS_TTL_SECONDS:
        return cached

    try:
        folders = await fetch_folders()
        if folders is None:
            raise RuntimeError("нет доступного Telegram-клиента")
    except Exception as e:
        # Причина в логи: UI молча скрывает пустую выпадашку, без лога
        # отладка с телефона невозможна.
        logger.warning(f"GET /api/folders failed: {e}", exc_info=True)
        # Отдаём устаревший кэш вместо поломанного UI, если он есть.
        if cached:
            logger.warning("Serving stale folder cache")
            return cached
        raise HTTPException(status_code=503, detail=f"Не удалось получить папки Telegram: {e}")

    _folders_cache.clear()
    _folders_cache.update({"data": folders, "ts": time.time()})
    return folders
