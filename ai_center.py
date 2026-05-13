from .shared import *
from .state import *
from contextvars import ContextVar
from time import perf_counter

from fastapi import Request

from .controllers import create_diagnostics_router
from .core import audit_log, configure_centralized_logging
from .repositories import DiagnosticsRepository
from .services import AnomalyService, RetryService, TTLCache

def html_escape(s: Optional[str]) -> str:
    """Lightweight safe HTML escape used for building messages shown to users.
    Uses html.escape but ensures None -> empty string.
    """
    if s is None:
        return ""
    # html.escape covers & < > and quotes when requested; keep behavior conservative
    return html.escape(str(s), quote=False)


# ----------------------------
# AI Assistant / FastAPI helpers
# ----------------------------

AI_APP_HOST = os.getenv("AI_APP_HOST", "0.0.0.0")
AI_APP_PORT = int(os.getenv("AI_APP_PORT", "8000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
AI_WEBAPP_URL = os.getenv(
    "AI_WEBAPP_URL",
    f"{RENDER_EXTERNAL_URL}/miniapp" if RENDER_EXTERNAL_URL else f"http://localhost:{AI_APP_PORT}/miniapp",
)
AI_MAX_RESULT_ROWS = int(os.getenv("AI_MAX_RESULT_ROWS", "40"))
AI_MAX_ANSWER_CHARS = int(os.getenv("AI_MAX_ANSWER_CHARS", "700"))
AI_MAX_QUESTION_CHARS = int(os.getenv("AI_MAX_QUESTION_CHARS", "700"))
AI_ALLOW_LOCAL_USER_ID = os.getenv("AI_ALLOW_LOCAL_USER_ID", "0") == "1"
AI_INITDATA_MAX_AGE_SEC = int(os.getenv("AI_INITDATA_MAX_AGE_SEC", "86400"))
AI_OPENROUTER_RETRIES = int(os.getenv("AI_OPENROUTER_RETRIES", "3"))
AI_AVATAR_CACHE_TTL_SEC = int(os.getenv("AI_AVATAR_CACHE_TTL_SEC", "900"))
AI_AVATAR_MAX_BYTES = int(os.getenv("AI_AVATAR_MAX_BYTES", "2097152"))
AI_ARCHIVE_PAGE_SIZE = int(os.getenv("AI_ARCHIVE_PAGE_SIZE", "40"))
AI_OVERVIEW_CACHE_TTL_SEC = int(os.getenv("AI_OVERVIEW_CACHE_TTL_SEC", "20"))
AI_DIRECTORY_CACHE_TTL_SEC = int(os.getenv("AI_DIRECTORY_CACHE_TTL_SEC", "15"))
AI_AUTOFIX_ON_READ = os.getenv("AI_AUTOFIX_ON_READ", "1") == "1"
AI_AUTOFIX_MAX_PER_REQUEST = int(os.getenv("AI_AUTOFIX_MAX_PER_REQUEST", "5"))
AI_ALLOWED_TABLES = {"messages", "deleted_messages", "chat_messages", "chat_dialogs", "risk_events", "risk_profiles"}
AI_FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "ATTACH",
    "DETACH",
    "REPLACE",
    "PRAGMA",
}
AI_SCHEMA_DESCRIPTION = (
    "messages(id INTEGER, user_id INTEGER, chat_id INTEGER, text TEXT, date TEXT); "
    "deleted_messages(id INTEGER, user_id INTEGER, chat_id INTEGER, text TEXT, date TEXT, "
    "chat_title TEXT, sender_username TEXT, content_type TEXT); "
    "chat_messages(id INTEGER, user_id INTEGER, chat_id INTEGER, msg_id INTEGER, sender_id INTEGER, sender_username TEXT, text TEXT, status TEXT, created_at TEXT, content_type TEXT); "
    "chat_dialogs(chat_id INTEGER, user_id INTEGER, title TEXT, username TEXT, dialog_type TEXT, last_message_at TEXT, history_complete INTEGER); "
    "risk_events(id INTEGER, user_id INTEGER, chat_id INTEGER, sender_id INTEGER, msg_id INTEGER, signal_type TEXT, severity TEXT, score REAL, title TEXT, detail TEXT, event_at TEXT); "
    "risk_profiles(id INTEGER, user_id INTEGER, profile_kind TEXT, profile_id INTEGER, risk_score REAL, delete_count INTEGER, edit_count INTEGER, disappearing_count INTEGER, night_count INTEGER, burst_count INTEGER, last_event_at TEXT, summary TEXT)"
)

configure_centralized_logging(CONFIG.logs_dir)

REQUEST_CONTEXT_USER_ID: ContextVar[int] = ContextVar("ai_request_user_id", default=0)
REQUEST_CONTEXT_AUTOFIX_COUNT: ContextVar[int] = ContextVar("ai_request_autofix_count", default=0)
DIAGNOSTICS_REPOSITORY = DiagnosticsRepository(CONFIG.db_path)
ANOMALY_SERVICE = AnomalyService(DIAGNOSTICS_REPOSITORY, logger)
RETRY_SERVICE = RetryService(logger)
AI_RESPONSE_CACHE = TTLCache(default_ttl=AI_OVERVIEW_CACHE_TTL_SEC, max_items=2048)


def _is_https_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme.lower() == "https" and bool(parsed.netloc)
    except Exception:
        return False


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _legacy_build_start_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📱 По номеру", callback_data="auth_phone"),
            InlineKeyboardButton("🗝 По QR-коду", callback_data="auth_qr"),
        ]
    ]

    if _is_https_url(AI_WEBAPP_URL):
        rows.append([InlineKeyboardButton("🤖 AI-ассистент", web_app=WebAppInfo(url=AI_WEBAPP_URL))])
    else:
        logger.warning("AI_WEBAPP_URL=%r is not a public HTTPS URL; AI button is disabled in /start", AI_WEBAPP_URL)

    return InlineKeyboardMarkup(rows)


# NOTE:
# The file contains legacy mojibake literals. We redefine this helper with
# explicit Unicode escapes to guarantee correct button labels in Telegram.
def build_start_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("\U0001F4F1 \u041F\u043E \u043D\u043E\u043C\u0435\u0440\u0443", callback_data="auth_phone"),
            InlineKeyboardButton("\U0001F5DD \u041F\u043E QR-\u043A\u043E\u0434\u0443", callback_data="auth_qr"),
        ]
    ]

    if _is_https_url(AI_WEBAPP_URL):
        rows.append([InlineKeyboardButton("\U0001F916 AI-\u0430\u0441\u0441\u0438\u0441\u0442\u0435\u043D\u0442", web_app=WebAppInfo(url=AI_WEBAPP_URL))])
    else:
        logger.warning("AI_WEBAPP_URL=%r is not a public HTTPS URL; AI button is disabled in /start", AI_WEBAPP_URL)

    return InlineKeyboardMarkup(rows)
AI_SYSTEM_PROMPT = (
    "Ты — генератор SQL-запросов для SQLite. "
    f"В базе доступны только таблицы: {AI_SCHEMA_DESCRIPTION}. "
    "Можно использовать только SELECT-запросы. "
    "Запрещены INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/DETACH/PRAGMA/REPLACE. "
    "Данные в таблицах уже отфильтрованы только под текущего пользователя. "
    "Выдавай только один рабочий SQL-запрос без пояснений и markdown. "
    "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 LIMIT, \u0435\u0441\u043b\u0438 \u043f\u043e\u0434\u0445\u043e\u0434\u0438\u0442 \u043f\u043e \u0437\u0430\u0434\u0430\u0447\u0435."
)
AI_RESULT_PROMPT = (
    "Ты помощник для обычного пользователя Telegram. "
    "Объясни результат простым языком без технических терминов и без упоминания SQL. "
    "Если данных нет, так и скажи. Дай короткий, полезный вывод."
)
MINIAPP_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Message Control Center</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0f131a;
      --bg-soft: #141923;
      --surface: rgba(23, 29, 39, 0.96);
      --surface-strong: rgba(28, 35, 47, 0.98);
      --line: rgba(255, 255, 255, 0.08);
      --line-strong: rgba(108, 140, 255, 0.28);
      --text: #edf1f7;
      --muted: #98a2b3;
      --accent: #7c9cff;
      --accent-soft: rgba(124, 156, 255, 0.12);
      --danger: #e35d6a;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      color: var(--text);
      font-family: "Inter", "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #0d1117 0%, #121822 100%);
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background: radial-gradient(circle at top right, rgba(124, 156, 255, 0.08), transparent 28%);
    }
    .shell {
      width: min(1160px, calc(100% - 24px));
      margin: 0 auto;
      padding: 18px 0 40px;
      display: grid;
      gap: 14px;
    }
    .panel {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }
    .hero {
      padding: 24px;
      display: grid;
      gap: 14px;
    }
    .hero-top {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .hero h1 {
      margin: 0 0 8px;
      font-family: "Inter", "Segoe UI", sans-serif;
      color: var(--accent);
      font-size: clamp(1.35rem, 4vw, 1.95rem);
      letter-spacing: -0.03em;
      font-weight: 700;
    }
    .hero p {
      margin: 0;
      max-width: 780px;
      color: var(--muted);
      line-height: 1.55;
    }
    .badge-row, .action-row, .quick-grid, .section-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .badge, .chip, .nav-button {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.03);
      color: #d8dfeb;
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 0.86rem;
      line-height: 1;
    }
    .section-nav {
      display: none;
    }
    .nav-button {
      cursor: pointer;
      font: inherit;
      font-weight: 600;
      transition: border-color 0.18s ease, color 0.18s ease, background 0.18s ease;
    }
    .nav-button.active {
      border-color: rgba(67,255,126,0.42);
      background: linear-gradient(180deg, rgba(18, 43, 24, 0.95), rgba(11, 28, 15, 0.98));
      color: var(--accent);
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(290px, 330px) minmax(0, 1fr);
      gap: 18px;
    }
    .stack {
      display: grid;
      gap: 18px;
    }
    .stack > .section-panel:nth-of-type(1) { order: 2; }
    .stack > .section-panel:nth-of-type(2) { order: 1; }
    .stack > .section-panel:nth-of-type(3) { order: 3; }
    .section-panel {
      display: grid;
    }
    .section-panel.active {
      display: grid;
    }
    .card {
      padding: 22px;
      display: grid;
      gap: 14px;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      flex-wrap: wrap;
    }
    .eyebrow {
      margin: 0 0 6px;
      color: var(--muted);
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.14em;
    }
    .card h2 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
    }
    .muted {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }
    .profile {
      display: grid;
      gap: 16px;
      align-content: start;
      position: sticky;
      top: 12px;
    }
    .profile-head {
      display: grid;
      grid-template-columns: 76px minmax(0, 1fr);
      gap: 14px;
      align-items: center;
    }
    .avatar {
      width: 76px;
      height: 76px;
      border-radius: 18px;
      border: 1px solid var(--line-strong);
      background: linear-gradient(180deg, rgba(124, 156, 255, 0.24), rgba(124, 156, 255, 0.08));
      display: grid;
      place-items: center;
      overflow: hidden;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.04);
    }
    .avatar img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: none;
    }
    .avatar-fallback {
      font-size: 1.3rem;
      color: var(--accent);
      font-weight: 700;
    }
    .profile-name {
      margin: 0;
      font-size: 1.18rem;
      font-weight: 700;
      line-height: 1.25;
    }
    .profile-username {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 0.94rem;
    }
    .status-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .summary-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .status-tile, .stat, .summary-tile {
      border: 1px solid var(--line);
      background: var(--surface-strong);
      border-radius: 14px;
      padding: 14px 15px;
    }
    .status-label, .stat-label, .summary-label {
      color: var(--muted);
      font-size: 0.8rem;
      margin-bottom: 8px;
    }
    .status-value {
      color: var(--text);
      font-size: 0.95rem;
      font-weight: 600;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .stat-value {
      font-size: 1.5rem;
      color: var(--accent);
      font-weight: 700;
    }
    .stat-note {
      margin-top: 8px;
      color: #b3ceb8;
      font-size: 0.84rem;
    }
    .summary-note {
      color: #b3ceb8;
      font-size: 0.84rem;
      line-height: 1.45;
    }
    .detail-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .detail-card {
      border: 1px solid var(--line);
      background: rgba(19, 25, 34, 0.92);
      border-radius: 14px;
      padding: 16px;
      display: grid;
      gap: 10px;
    }
    .detail-title {
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text);
    }
    .detail-list {
      display: grid;
      gap: 10px;
    }
    .detail-item {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .detail-item:first-child {
      border-top: none;
      padding-top: 0;
    }
    .detail-item strong {
      display: block;
      font-size: 0.92rem;
      margin-bottom: 4px;
      color: var(--text);
    }
    .detail-item span {
      display: block;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.45;
    }
    .assistant-area {
      display: grid;
      gap: 12px;
    }
    .session-block {
      display: grid;
      gap: 12px;
    }
    textarea {
      width: 100%;
      min-height: 144px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(15, 20, 29, 0.92);
      color: var(--text);
      font: inherit;
      padding: 14px 15px;
      resize: vertical;
      outline: none;
      transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
    }
    textarea:focus {
      border-color: var(--line-strong);
      box-shadow: 0 0 0 3px rgba(67,255,126,0.10);
      transform: translateY(-1px);
    }
    button {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.03);
      color: var(--text);
      font: inherit;
      font-weight: 600;
      padding: 11px 14px;
      cursor: pointer;
      transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease, color 0.18s ease;
    }
    button:hover { border-color: var(--line-strong); }
    button:active { transform: translateY(1px); }
    button:disabled { opacity: 0.65; cursor: not-allowed; transform: none; }
    .btn-primary {
      background: linear-gradient(180deg, rgba(98, 126, 224, 0.9), rgba(74, 99, 188, 0.92));
      color: #f8faff;
      border-color: rgba(124, 156, 255, 0.28);
    }
    .btn-secondary {
      color: #d6deea;
    }
    .btn-danger {
      background: linear-gradient(180deg, rgba(121, 49, 63, 0.92), rgba(94, 36, 48, 0.94));
      border-color: rgba(227, 93, 106, 0.24);
      color: #ffe5e8;
    }
    .chip {
      cursor: pointer;
      background: rgba(255, 255, 255, 0.02);
      padding: 12px 14px;
      text-align: left;
    }
    .chip:hover {
      border-color: var(--line-strong);
      color: var(--accent);
    }
    .result, .notice {
      border: 1px solid var(--line);
      background: rgba(17, 22, 31, 0.92);
      border-radius: 14px;
      padding: 14px 15px;
      line-height: 1.6;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .notice {
      color: #cbe9d1;
    }
    .notice.danger {
      border-color: rgba(255, 95, 125, 0.28);
      background: rgba(38, 10, 16, 0.72);
      color: #ffc0cd;
    }
    .notice.success {
      border-color: rgba(124, 156, 255, 0.24);
      background: rgba(28, 38, 58, 0.82);
      color: #dfe8ff;
    }
    .notice.warning {
      border-color: rgba(247, 215, 116, 0.18);
      background: rgba(55, 47, 22, 0.78);
      color: #fbe8a6;
    }
    details {
      border: 1px dashed var(--line);
      background: rgba(16, 21, 29, 0.82);
      border-radius: 14px;
      padding: 10px 12px;
    }
    summary {
      cursor: pointer;
      color: #a9d8b2;
      user-select: none;
      font-size: 0.92rem;
    }
    pre {
      margin: 10px 0 0;
      padding: 12px;
      border-radius: 14px;
      background: #040804;
      border: 1px solid rgba(255, 255, 255, 0.08);
      max-height: 260px;
      overflow: auto;
      font-size: 0.82rem;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .footer-note {
      color: var(--muted);
      font-size: 0.82rem;
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      .profile { position: static; order: 2; }
      .stack { order: 1; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .detail-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 580px) {
      .shell { width: min(100% - 16px, 100%); }
      .hero, .card { padding: 16px; }
      .status-grid, .stats-grid, .summary-grid { grid-template-columns: 1fr; }
      .profile-head { grid-template-columns: 64px minmax(0, 1fr); }
      .avatar { width: 64px; height: 64px; border-radius: 18px; }
      .action-row button, .section-actions button { width: 100%; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel hero">
      <div class="hero-top">
        <div>
          <h1>Message Control Center</h1>
          <p>Личный центр управления архивом сообщений. Здесь можно посмотреть свою статистику, задать вопрос ассистенту простыми словами и безопасно завершить текущую сессию.</p>
        </div>
        <div class="badge-row">
          <span class="badge" id="identity-pill">Пользователь не определен</span>
          <span class="badge">Показ строк в деталях: {{MAX_ROWS}}</span>
        </div>
      </div>
      <nav class="section-nav" aria-label="Разделы центра">
        <button class="nav-button active" type="button" data-section="dashboard">Обзор</button>
        <button class="nav-button" type="button" data-section="assistant">AI-помощник</button>
        <button class="nav-button" type="button" data-section="session">Сессия</button>
      </nav>
    </section>

    <section class="layout">
      <aside class="panel card profile">
        <div>
          <p class="eyebrow">Профиль</p>
          <div class="profile-head">
            <div class="avatar">
              <img id="profile-avatar" alt="Аватар пользователя" />
              <span class="avatar-fallback" id="profile-avatar-fallback">U</span>
            </div>
            <div>
              <h2 class="profile-name" id="profile-name">Загрузка профиля...</h2>
              <p class="profile-username" id="profile-username">@username</p>
            </div>
          </div>
        </div>

        <div class="status-grid">
            <div class="status-tile">
              <div class="status-label">ID пользователя</div>
              <div class="status-value" id="profile-id">\u2014</div>
            </div>
            <div class="status-tile">
              <div class="status-label">\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a \u0432\u0445\u043e\u0434\u0430</div>
              <div class="status-value" id="profile-source">Telegram Mini App</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Сессия</div>
              <div class="status-value" id="profile-session">Проверяется...</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Watcher</div>
              <div class="status-value" id="profile-watcher">Проверяется...</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Язык</div>
              <div class="status-value" id="profile-language">\u2014</div>
            </div>
            <div class="status-tile">
              <div class="status-label">Telegram Premium</div>
              <div class="status-value" id="profile-premium">\u2014</div>
            </div>
          </div>
        <div class="summary-grid">
          <div class="summary-tile">
            <div class="summary-label">Краткий статус</div>
            <div class="summary-note" id="session-state-copy">Данные о сессии появятся после проверки.</div>
          </div>
          <div class="summary-tile">
            <div class="summary-label">Мониторинг</div>
            <div class="summary-note" id="watcher-state-copy">Статус watcher будет показан после загрузки.</div>
          </div>
        </div>
        <p class="footer-note">Профиль остается слева, а основные функции вынесены в отдельные разделы.</p>
      </aside>

      <div class="stack">
        <section class="panel card section-panel active" data-section-panel="dashboard">
          <div class="card-head">
            <div>
              <p class="eyebrow">Общая статистика</p>
              <h2>Ваш архив</h2>
            </div>
            <div class="badge-row">
              <span class="badge" id="top-chat-badge">Топ-чат: нет данных</span>
              <span class="badge" id="last-event-badge">Последнее событие: нет данных</span>
            </div>
          </div>
          <div class="stats-grid">
            <div class="stat">
              <div class="stat-label">Удалено всего</div>
              <div class="stat-value" id="stat-total-deleted">0</div>
              <div class="stat-note">Все удаленные сообщения, найденные watcher'ом.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Удалено сегодня</div>
              <div class="stat-value" id="stat-today-deleted">0</div>
              <div class="stat-note">Активность за текущие сутки.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Сообщений в архиве</div>
              <div class="stat-value" id="stat-total-messages">0</div>
              <div class="stat-note">Сохраненные оригиналы сообщений.</div>
            </div>
            <div class="stat">
              <div class="stat-label">Лидер по удалениям</div>
              <div class="stat-value" id="stat-top-chat-count">0</div>
              <div class="stat-note" id="stat-top-chat-name">Нет данных</div>
            </div>
          </div>
          <div class="notice success" id="overview-summary">Загружаю статистику...</div>
          <div class="detail-grid">
            <div class="detail-card">
              <div class="detail-title">Ключевая динамика</div>
              <div class="detail-list">
                <div class="detail-item">
                  <strong id="stat-week-deleted">0 за 7 дней</strong>
                  <span>Недавняя активность по удалениям в архиве.</span>
                </div>
                <div class="detail-item">
                  <strong id="detail-last-event">Нет данных</strong>
                  <span>Последнее зафиксированное удаление.</span>
                </div>
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-title">Главные чаты</div>
              <div class="detail-list" id="top-chats-list">
                <div class="detail-item">
                  <strong>Нет данных</strong>
                  <span>Пока в архиве недостаточно данных для ранжирования.</span>
                </div>
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-title">Последние удаления</div>
              <div class="detail-list" id="recent-deletions-list">
                <div class="detail-item">
                  <strong>Нет данных</strong>
                  <span>Здесь появятся последние записи из личного архива.</span>
                </div>
              </div>
            </div>
          </div>
          <div class="section-actions">
            <button id="refresh-overview" class="btn-secondary" type="button">Обновить данные</button>
            <button class="btn-primary" type="button" data-go-section="assistant">Открыть AI-помощника</button>
          </div>
        </section>

        <section class="panel card section-panel" data-section-panel="assistant">
          <div class="card-head">
            <div>
              <p class="eyebrow">AI-помощник</p>
              <h2>Спросите обычным языком</h2>
            </div>
            <div class="badge-row">
              <span class="badge">Ответ без технического жаргона</span>
            </div>
          </div>

          <div class="quick-grid">
            <button class="chip" type="button" data-question="Кто удалил больше всего сообщений сегодня?">Кто удалял сегодня больше всего</button>
            <button class="chip" type="button" data-question="В каком чате у меня больше всего удаленных сообщений?">Какой чат самый активный</button>
            <button class="chip" type="button" data-question="Сколько у меня удалений было вчера?">Сколько было вчера</button>
            <button class="chip" type="button" data-question="Покажи последние 5 удаленных сообщений.">Последние 5 удалений</button>
          </div>

          <div class="assistant-area">
            <textarea id="question" placeholder="Например: кто чаще всего удаляет сообщения в моем архиве за неделю?" aria-label="Вопрос к AI-ассистенту"></textarea>
            <div class="action-row">
              <button id="ask-button" class="btn-primary" type="button">Получить ответ</button>
              <button class="btn-secondary" type="button" data-go-section="dashboard">Вернуться к обзору</button>
            </div>
            <div class="result" id="answer">Задайте вопрос, и я объясню результат простыми словами.</div>
            <div class="notice" id="status-text">Готов к работе.</div>
            <details>
              <summary>Технические детали для отладки</summary>
              <pre id="sql-output"></pre>
              <pre id="data-output">[]</pre>
            </details>
          </div>
        </section>
        <section class="panel card section-panel" data-section-panel="session">
          <div class="card-head">
            <div>
              <p class="eyebrow">Сессия</p>
              <h2>Управление подключением</h2>
            </div>
            <div class="badge-row">
              <span class="badge">Безопасное завершение</span>
            </div>
          </div>
          <div class="summary-grid">
            <div class="summary-tile">
              <div class="summary-label">Состояние сессии</div>
              <div class="summary-note" id="session-info-box">Данные о сессии будут показаны после первой проверки.</div>
            </div>
            <div class="summary-tile">
              <div class="summary-label">Статус watcher</div>
              <div class="summary-note" id="watcher-info-box">Watcher будет проверен автоматически при загрузке.</div>
            </div>
          </div>
          <div class="notice" id="session-result">Состояние сессии будет показано после проверки.</div>
          <div class="action-row">
            <button id="session-refresh" class="btn-secondary" type="button">Обновить статус</button>
            <button id="logout-session" class="btn-danger" type="button">Завершить сессию</button>
          </div>
        </section>
      </div>
    </section>
  </main>

  <script>
    const maxRows = {{MAX_ROWS}};
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) {
      tg.ready();
      tg.expand();
      if (typeof tg.enableClosingConfirmation === "function") {
        tg.enableClosingConfirmation();
      }
    }

    const identityPill = document.getElementById("identity-pill");
    const profileNameEl = document.getElementById("profile-name");
    const profileUsernameEl = document.getElementById("profile-username");
    const profileIdEl = document.getElementById("profile-id");
    const profileSourceEl = document.getElementById("profile-source");
    const profileSessionEl = document.getElementById("profile-session");
    const profileWatcherEl = document.getElementById("profile-watcher");
    const profileLanguageEl = document.getElementById("profile-language");
    const profilePremiumEl = document.getElementById("profile-premium");
    const profileAvatarEl = document.getElementById("profile-avatar");
    const profileAvatarFallbackEl = document.getElementById("profile-avatar-fallback");
    const totalDeletedEl = document.getElementById("stat-total-deleted");
    const todayDeletedEl = document.getElementById("stat-today-deleted");
    const totalMessagesEl = document.getElementById("stat-total-messages");
    const topChatCountEl = document.getElementById("stat-top-chat-count");
    const topChatNameEl = document.getElementById("stat-top-chat-name");
    const topChatBadgeEl = document.getElementById("top-chat-badge");
    const lastEventBadgeEl = document.getElementById("last-event-badge");
    const overviewSummaryEl = document.getElementById("overview-summary");
    const weekDeletedEl = document.getElementById("stat-week-deleted");
    const detailLastEventEl = document.getElementById("detail-last-event");
    const topChatsListEl = document.getElementById("top-chats-list");
    const recentDeletionsListEl = document.getElementById("recent-deletions-list");
    const sessionStateCopyEl = document.getElementById("session-state-copy");
    const watcherStateCopyEl = document.getElementById("watcher-state-copy");
    const sessionInfoBoxEl = document.getElementById("session-info-box");
    const watcherInfoBoxEl = document.getElementById("watcher-info-box");
    const questionEl = document.getElementById("question");
    const answerEl = document.getElementById("answer");
    const sqlEl = document.getElementById("sql-output");
    const dataEl = document.getElementById("data-output");
    const statusText = document.getElementById("status-text");
    const sessionResultEl = document.getElementById("session-result");
    const askBtn = document.getElementById("ask-button");
    const refreshOverviewBtn = document.getElementById("refresh-overview");
    const sessionRefreshBtn = document.getElementById("session-refresh");
    const logoutSessionBtn = document.getElementById("logout-session");
    const quickQuestionButtons = Array.from(document.querySelectorAll("[data-question]"));
    const navButtons = Array.from(document.querySelectorAll("[data-section]"));
    const sectionPanels = Array.from(document.querySelectorAll("[data-section-panel]"));
    const jumpButtons = Array.from(document.querySelectorAll("[data-go-section]"));

    const state = {
      identity: null,
      avatarBlobUrl: null,
      activeSection: "dashboard",
    };

    const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

    const switchSection = (sectionName) => {
      state.activeSection = sectionName || "dashboard";
      navButtons.forEach((button) => {
        button.classList.toggle("active", button.dataset.section === state.activeSection);
      });
      sectionPanels.forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.sectionPanel === state.activeSection);
      });
    };

    const renderStatus = (text, kind = "") => {
      statusText.textContent = text || "";
      statusText.className = kind ? `notice ${kind}` : "notice";
    };

    const renderRows = (rows) => {
      dataEl.textContent = rows && rows.length ? JSON.stringify(rows, null, 2) : "[]";
    };

    const escapeHtml = (value) => String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    const renderNamedList = (container, items, emptyTitle, emptyText, formatter) => {
      if (!container) {
        return;
      }
      if (!items || !items.length) {
        container.innerHTML = `<div class="detail-item"><strong>${escapeHtml(emptyTitle)}</strong><span>${escapeHtml(emptyText)}</span></div>`;
        return;
      }
      container.innerHTML = items.map((item) => formatter(item)).join("");
    };

    const readLaunchParam = (name) => {
      const sources = [
        new URLSearchParams(window.location.search),
        new URLSearchParams(window.location.hash.startsWith("#") ? window.location.hash.slice(1) : ""),
      ];
      for (const source of sources) {
        const value = source.get(name);
        if (value) {
          return value;
        }
      }
      return "";
    };

    const parseInitData = (initData) => {
      const parsed = {};
      if (!initData) {
        return parsed;
      }
      const params = new URLSearchParams(initData);
      params.forEach((value, key) => {
        parsed[key] = value;
      });
      if (parsed.user) {
        try {
          parsed.user = JSON.parse(parsed.user);
        } catch (error) {
          parsed.user = null;
        }
      }
      return parsed;
    };

    const getTelegramContext = () => {
      const rawInitData = tg && typeof tg.initData === "string" && tg.initData.trim()
        ? tg.initData.trim()
        : readLaunchParam("tgWebAppData");
      const initData = rawInitData || "";
      const parsed = parseInitData(initData);
      const user = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || parsed.user || null;
      return { initData, parsed, user };
    };

    const buildIdentityPayload = () => {
      const context = getTelegramContext();
      const user = context.user || {};
      return {
        init_data: context.initData || "",
        user_id: user.id || null,
        first_name: user.first_name || "",
        last_name: user.last_name || "",
        username: user.username || "",
        photo_url: user.photo_url || "",
        language_code: user.language_code || "",
      };
    };

    const waitForIdentityPayload = async () => {
      for (let attempt = 0; attempt < 12; attempt += 1) {
        const payload = buildIdentityPayload();
        if (payload.init_data || payload.user_id) {
          state.identity = payload;
          return payload;
        }
        await sleep(250);
      }
      const fallbackPayload = buildIdentityPayload();
      state.identity = fallbackPayload;
      return fallbackPayload;
    };

    const displayNameFromProfile = (profile) => {
      if (!profile) {
        return "Пользователь";
      }
      return profile.display_name || [profile.first_name, profile.last_name].filter(Boolean).join(" ").trim() || (profile.username ? `@${profile.username}` : "Пользователь");
    };

    const renderAvatar = async (profile, identityPayload) => {
      const initials = (profile && profile.initials) || "U";
      profileAvatarFallbackEl.textContent = initials;
      profileAvatarFallbackEl.style.display = "grid";
      profileAvatarEl.style.display = "none";
      profileAvatarEl.removeAttribute("src");

      if (state.avatarBlobUrl) {
        URL.revokeObjectURL(state.avatarBlobUrl);
        state.avatarBlobUrl = null;
      }

      if (profile && profile.photo_url) {
        profileAvatarEl.src = profile.photo_url;
        profileAvatarEl.style.display = "block";
        profileAvatarFallbackEl.style.display = "none";
        return;
      }

      try {
        const response = await fetch("/ai/profile/avatar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(identityPayload),
        });
        if (!response.ok) {
          return;
        }
        const blob = await response.blob();
        if (!blob || !blob.size) {
          return;
        }
        state.avatarBlobUrl = URL.createObjectURL(blob);
        profileAvatarEl.src = state.avatarBlobUrl;
        profileAvatarEl.style.display = "block";
        profileAvatarFallbackEl.style.display = "none";
      } catch (error) {
        console.warn("Avatar load failed", error);
      }
    };

    const renderProfile = async (profile, meta = {}) => {
      const identityPayload = state.identity || buildIdentityPayload();
      const name = displayNameFromProfile(profile);
      const username = profile && profile.username ? `@${profile.username}` : "username не указан";
      const sessionText = meta.session_active ? "Активна" : "Не активна";
      const watcherText = meta.watcher_active ? "Подключен" : "Не подключен";
      profileNameEl.textContent = name;
      profileUsernameEl.textContent = username;
      profileIdEl.textContent = profile && profile.user_id ? String(profile.user_id) : "\u2014";
      profileSourceEl.textContent = profile && profile.source === "local" ? "Локальный режим" : "Telegram Mini App";
      profileSessionEl.textContent = sessionText;
      profileWatcherEl.textContent = watcherText;
      profileLanguageEl.textContent = profile && profile.language_code ? String(profile.language_code).toUpperCase() : "Не указан";
      profilePremiumEl.textContent = profile && profile.is_premium ? "Да" : "Нет";
      identityPill.textContent = profile && profile.user_id ? `${name} · ID ${profile.user_id}` : "Пользователь не определен";
      sessionStateCopyEl.textContent = meta.session_active
        ? "Сессия активна. Можно продолжать работу с архивом и аналитикой."
        : "Сессия не активна. Для сбора новых данных потребуется повторная авторизация.";
      watcherStateCopyEl.textContent = meta.watcher_active
        ? "Watcher подключен и продолжает отслеживать новые события."
        : "Watcher сейчас не подключен. Новые события не будут собираться до восстановления сессии.";
      sessionInfoBoxEl.textContent = meta.session_active
        ? "Текущая сессия действует. При необходимости ее можно завершить вручную."
        : "Текущая сессия уже не активна. Если нужен сбор новых сообщений, потребуется вход заново.";
      watcherInfoBoxEl.textContent = meta.watcher_active
        ? "Мониторинг работает штатно и сохраняет новые события."
        : "Мониторинг остановлен или еще не запускался для этого пользователя.";
      await renderAvatar(profile, identityPayload);
    };

    const postJson = async (url, payload) => {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const parsed = await response.json().catch(() => ({ detail: `Ошибка ${response.status}` }));
      if (!response.ok) {
        throw new Error(parsed.detail || `Ошибка ${response.status}`);
      }
      return parsed;
    };

    const refreshOverview = async () => {
      refreshOverviewBtn.disabled = true;
      sessionRefreshBtn.disabled = true;
      try {
        const identityPayload = await waitForIdentityPayload();
        const payload = await postJson("/ai/overview", identityPayload);
        totalDeletedEl.textContent = payload.total_deleted || 0;
        todayDeletedEl.textContent = payload.deleted_today || 0;
        totalMessagesEl.textContent = payload.total_messages || 0;
        topChatCountEl.textContent = payload.top_chat?.count || 0;
        topChatNameEl.textContent = payload.top_chat?.name || "Нет данных";
        topChatBadgeEl.textContent = `Топ-чат: ${payload.top_chat?.name || "нет данных"}`;
        lastEventBadgeEl.textContent = `Последнее событие: ${payload.last_event || "нет данных"}`;
        weekDeletedEl.textContent = "\u2014";
        detailLastEventEl.textContent = payload.last_event || "Нет данных";
        overviewSummaryEl.textContent = payload.summary || "Статистика обновлена.";
        overviewSummaryEl.className = "notice success";
        renderNamedList(
          topChatsListEl,
          payload.top_chats || [],
          "Нет данных",
          "Пока в архиве недостаточно данных для ранжирования.",
          (item) => `<div class="detail-item"><strong>${escapeHtml(item.name || "Без названия")}</strong><span>${escapeHtml(String(item.count || 0))} удалений</span></div>`
        );
        renderNamedList(
          recentDeletionsListEl,
          payload.latest_deleted || [],
          "Нет данных",
          "Здесь появятся последние записи из личного архива.",
          (item) => `<div class="detail-item"><strong>${escapeHtml(item.chat_name || "Неизвестный чат")}</strong><span>${escapeHtml(item.text_preview || "Без текста")}</span><span>${escapeHtml(item.deleted_at || "")}${item.sender_username ? ` · @${escapeHtml(item.sender_username)}` : ""}${item.content_type ? ` · ${escapeHtml(item.content_type)}` : ""}</span></div>`
        );
        sessionResultEl.textContent = payload.session_active
          ? "Сессия активна. Watcher готов обрабатывать события."
          : "Сессия не активна. Для сбора сообщений потребуется авторизация.";
        sessionResultEl.className = payload.session_active ? "notice success" : "notice warning";
        await renderProfile(payload.profile || {}, payload);
      } catch (error) {
        overviewSummaryEl.textContent = error?.message || "Не удалось загрузить статистику.";
        overviewSummaryEl.className = "notice danger";
        weekDeletedEl.textContent = "\u2014";
        detailLastEventEl.textContent = "Нет данных";
        sessionResultEl.textContent = "Не удалось определить пользователя. Откройте центр через кнопку внутри Telegram.";
        sessionResultEl.className = "notice danger";
        renderNamedList(topChatsListEl, [], "Нет данных", "Не удалось загрузить статистику по чатам.", () => "");
        renderNamedList(recentDeletionsListEl, [], "Нет данных", "Не удалось загрузить последние записи.", () => "");
        renderStatus("Не удалось определить пользователя. Откройте центр через кнопку внутри Telegram.", "danger");
      } finally {
        refreshOverviewBtn.disabled = false;
        sessionRefreshBtn.disabled = false;
      }
    };

    const askAssistant = async () => {
      const question = questionEl.value.trim();
      if (!question) {
        switchSection("assistant");
        renderStatus("Введите вопрос для ассистента.", "danger");
        return;
      }
      askBtn.disabled = true;
      switchSection("assistant");
      renderStatus("Готовлю ответ и анализирую архив...");
      sqlEl.textContent = "";
      dataEl.textContent = "[]";
      answerEl.textContent = "Обрабатываю запрос...";
      try {
        const identityPayload = await waitForIdentityPayload();
        const payload = await postJson("/ai", { question, ...identityPayload });
        answerEl.textContent = payload.answer || "Нет ответа.";
        sqlEl.textContent = payload.sql || "";
        renderRows(payload.result?.rows ?? []);
        renderStatus(
          payload.result?.truncated
            ? `Ответ готов. Для технических деталей показаны только первые ${maxRows} строк.`
            : "Ответ готов.",
          "success"
        );
      } catch (error) {
        answerEl.textContent = "Не удалось получить ответ.";
        renderStatus(error?.message || "Ошибка при выполнении запроса.", "danger");
      } finally {
        askBtn.disabled = false;
      }
    };

    const logoutSession = async () => {
      if (!window.confirm("Завершить текущую сессию? После этого потребуется авторизоваться заново.")) {
        return;
      }
      logoutSessionBtn.disabled = true;
      switchSection("session");
      try {
        const identityPayload = await waitForIdentityPayload();
        const payload = await postJson("/ai/session/logout", identityPayload);
        sessionResultEl.textContent = payload.message || "Сессия завершена.";
        sessionResultEl.className = payload.session_closed ? "notice success" : "notice danger";
        await refreshOverview();
      } catch (error) {
        sessionResultEl.textContent = error?.message || "Не удалось завершить сессию.";
        sessionResultEl.className = "notice danger";
      } finally {
        logoutSessionBtn.disabled = false;
      }
    };

    navButtons.forEach((button) => {
      button.addEventListener("click", () => {
        switchSection(button.dataset.section || "dashboard");
      });
    });
    jumpButtons.forEach((button) => {
      button.addEventListener("click", () => {
        switchSection(button.dataset.goSection || "dashboard");
      });
    });
    askBtn.addEventListener("click", askAssistant);
    refreshOverviewBtn.addEventListener("click", refreshOverview);
    sessionRefreshBtn.addEventListener("click", refreshOverview);
    logoutSessionBtn.addEventListener("click", logoutSession);
    questionEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        askAssistant();
      }
    });
    quickQuestionButtons.forEach((button) => {
      button.addEventListener("click", () => {
        questionEl.value = button.dataset.question || "";
        switchSection("assistant");
        questionEl.focus();
      });
    });

    switchSection("dashboard");
    refreshOverview();
  </script>
</body>
</html>
"""
MINIAPP_HTML = MINIAPP_TEMPLATE.replace("{{MAX_ROWS}}", str(AI_MAX_RESULT_ROWS))
ARCHIVE_MINIAPP_PATH = os.path.join(os.path.dirname(__file__), "archive_miniapp.html")
ARCHIVE_MINIAPP_CSS_PATH = os.path.join(os.path.dirname(__file__), "archive_miniapp.css")
ARCHIVE_MINIAPP_JS_PATH = os.path.join(os.path.dirname(__file__), "archive_miniapp.js")


def _render_archive_mini_app() -> str:
    try:
        with open(ARCHIVE_MINIAPP_PATH, "r", encoding="utf-8") as handle:
            template = handle.read()
        css_v = str(int(os.path.getmtime(ARCHIVE_MINIAPP_CSS_PATH))) if os.path.exists(ARCHIVE_MINIAPP_CSS_PATH) else "1"
        js_v = str(int(os.path.getmtime(ARCHIVE_MINIAPP_JS_PATH))) if os.path.exists(ARCHIVE_MINIAPP_JS_PATH) else "1"
        rendered = template.replace("{{ARCHIVE_PAGE_SIZE}}", str(AI_ARCHIVE_PAGE_SIZE))
        rendered = rendered.replace("/miniapp.css", f"/miniapp.css?v={css_v}")
        rendered = rendered.replace("/miniapp.js", f"/miniapp.js?v={js_v}")
        return rendered
    except Exception:
        logger.exception("Failed to load archive mini app template from %s", ARCHIVE_MINIAPP_PATH)
        return (
            "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Telegram Archive</title></head><body>"
            "<h2>Mini App временно недоступен</h2>"
            "<p>Не удалось загрузить шаблон интерфейса. Попробуйте позже.</p>"
            "</body></html>"
        )

ai_app = FastAPI(title="Saved Delete Messages — AI assistant", version="1.0")
ai_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@ai_app.on_event("startup")
async def _ai_startup() -> None:
    await DIAGNOSTICS_REPOSITORY.init_schema()
    audit_log(logger, "ai.startup.ready", db_path=CONFIG.db_path)


@ai_app.middleware("http")
async def _api_observability_middleware(request: Request, call_next):
    started = perf_counter()
    ctx_token = REQUEST_CONTEXT_USER_ID.set(0)
    autofix_token = REQUEST_CONTEXT_AUTOFIX_COUNT.set(0)
    response = None
    caught_error: Optional[BaseException] = None
    try:
        response = await call_next(request)
        return response
    except BaseException as exc:  # noqa: BLE001
        caught_error = exc
        raise
    finally:
        elapsed_ms = int((perf_counter() - started) * 1000)
        status_code = int(response.status_code) if response is not None else 500
        user_id = int(REQUEST_CONTEXT_USER_ID.get(0) or 0)
        try:
            await ANOMALY_SERVICE.record_api_request(
                endpoint=request.url.path,
                method=request.method,
                status_code=status_code,
                duration_ms=elapsed_ms,
                user_id=user_id,
                error_type=type(caught_error).__name__ if caught_error else "",
                note=(str(caught_error)[:300] if caught_error else ""),
            )
        except Exception:
            logger.exception("Failed to store API observability event")
        REQUEST_CONTEXT_USER_ID.reset(ctx_token)
        REQUEST_CONTEXT_AUTOFIX_COUNT.reset(autofix_token)

BOT_RUNTIME_APP: Optional[Any] = None
BOT_RUNTIME_LOOP: Optional[asyncio.AbstractEventLoop] = None
AI_AVATAR_CACHE: Dict[int, Dict[str, Any]] = {}
AI_SERVER_THREAD: Optional[Thread] = None


@dataclass(frozen=True)
class AIIdentityContext:
    user_id: int
    profile: Dict[str, Any]
    source: str


class AIIdentityPayload(BaseModel):
    init_data: str = ""
    user_id: Optional[int] = None
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    language_code: str = ""


class AIQuestionPayload(AIIdentityPayload):
    question: str = Field(..., min_length=1, max_length=AI_MAX_QUESTION_CHARS)


class AIQueryResult(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    limit: int


class AIQueryResponse(BaseModel):
    answer: str
    sql: str
    result: AIQueryResult


class AIUserProfile(BaseModel):
    user_id: int
    display_name: str
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    initials: str = "U"
    language_code: str = ""
    source: str = "telegram"
    is_premium: bool = False


class AIOverviewChatItem(BaseModel):
    name: str
    count: int


class AIOverviewDeletedItem(BaseModel):
    chat_name: str
    text_preview: str
    deleted_at: str
    sender_username: str = ""
    content_type: str = ""


class AIOverviewResponse(BaseModel):
    profile: AIUserProfile
    total_deleted: int
    deleted_today: int
    deleted_last_7_days: int
    total_messages: int
    top_chat: Dict[str, Any]
    top_chats: List[AIOverviewChatItem]
    latest_deleted: List[AIOverviewDeletedItem]
    last_event: str
    session_active: bool
    watcher_active: bool
    summary: str


class AIArchiveUsersRequest(AIIdentityPayload):
    pass


class AIArchiveUserItem(BaseModel):
    sender_key: str
    sender_id: Optional[int] = None
    username: str = ""
    display_name: str
    total_deleted: int
    text_count: int = 0
    photo_count: int = 0
    video_count: int = 0
    voice_count: int = 0
    other_count: int = 0
    last_deleted_at: str = ""


class AIArchiveUsersResponse(BaseModel):
    profile: AIUserProfile
    session_active: bool
    watcher_active: bool
    users: List[AIArchiveUserItem]


class AIArchiveListRequest(AIIdentityPayload):
    sender_key: str = ""
    category: str = "all"
    search: str = ""
    sort: str = "date_desc"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=AI_ARCHIVE_PAGE_SIZE, ge=1, le=100)


class AIArchiveRecord(BaseModel):
    item_id: int
    sender_key: str
    sender_label: str
    sender_username: str = ""
    chat_title: str = ""
    text: str = ""
    original_text: str = ""
    deleted_at: str = ""
    content_type: str = ""
    category: str
    has_media: bool = False
    has_preview: bool = False
    can_send_to_chat: bool = False
    file_name: str = ""


class AIArchiveListResponse(BaseModel):
    selected_sender_key: str
    category: str
    sort: str
    total: int
    offset: int
    limit: int
    has_more: bool
    items: List[AIArchiveRecord]


class AIArchiveMediaRequest(AIIdentityPayload):
    item_id: int = Field(..., gt=0)
    chat_id: Optional[int] = None


class AIArchiveSendResponse(BaseModel):
    ok: bool
    message: str


class AIThreadUsersRequest(AIIdentityPayload):
    pass


class AIThreadUserItem(BaseModel):
    sender_key: str
    sender_id: Optional[int] = None
    username: str = ""
    display_name: str
    total_count: int
    active_count: int = 0
    deleted_count: int = 0
    edited_count: int = 0
    last_message_at: str = ""


class AIThreadUsersResponse(BaseModel):
    profile: AIUserProfile
    session_active: bool
    watcher_active: bool
    users: List[AIThreadUserItem]


class AIThreadListRequest(AIIdentityPayload):
    sender_key: str = ""
    status: str = "all"
    search: str = ""
    sort: str = "date_desc"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=AI_ARCHIVE_PAGE_SIZE, ge=1, le=100)


class AIThreadRecord(BaseModel):
    item_id: int
    sender_key: str
    sender_label: str
    sender_username: str = ""
    chat_id: Optional[int] = None
    chat_title: str = ""
    msg_id: Optional[int] = None
    status: str = "active"
    text: str = ""
    original_text: str = ""
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str = ""
    edit_count: int = 0
    content_type: str = ""
    has_media: bool = False
    has_preview: bool = False
    can_send_to_chat: bool = False
    file_name: str = ""


class AIThreadListResponse(BaseModel):
    selected_sender_key: str
    status: str
    sort: str
    total: int
    offset: int
    limit: int
    has_more: bool
    items: List[AIThreadRecord]


class AIThreadHistoryRequest(AIIdentityPayload):
    item_id: int = Field(..., gt=0)
    chat_id: Optional[int] = None


class AIThreadAllRequest(AIIdentityPayload):
    status: str = "all"
    search: str = ""
    sort: str = "date_desc"
    category: str = "all"
    from_date: str = ""
    to_date: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=AI_ARCHIVE_PAGE_SIZE, ge=1, le=100)


class AIThreadHistoryEvent(BaseModel):
    event_type: str
    text: str = ""
    previous_text: str = ""
    created_at: str = ""


class AIThreadHistoryResponse(BaseModel):
    item_id: int
    events: List[AIThreadHistoryEvent]


class AIChatDialogsRequest(AIIdentityPayload):
    search: str = ""
    limit: int = Field(default=150, ge=1, le=300)


class AIChatDialogItem(BaseModel):
    chat_id: int
    title: str
    username: str = ""
    dialog_type: str = "private"
    last_message_id: Optional[int] = None
    last_message_at: str = ""
    last_message_preview: str = ""
    last_sender_label: str = ""
    unread_count: int = 0
    message_count: int = 0
    deleted_count: int = 0
    edited_count: int = 0
    has_recent_changes: bool = False
    has_photo: bool = False
    history_complete: bool = False
    sync_status: str = "idle"


class AIChatDialogsResponse(BaseModel):
    profile: AIUserProfile
    session_active: bool
    watcher_active: bool
    dialogs: List[AIChatDialogItem]


class AIChatMessagesRequest(AIIdentityPayload):
    chat_id: int
    before_msg_id: Optional[int] = Field(default=None, gt=0)
    limit: int = Field(default=60, ge=1, le=200)


class AIChatAvatarRequest(AIIdentityPayload):
    chat_id: int


class AIChatMessageItem(BaseModel):
    item_id: int
    chat_id: int
    msg_id: Optional[int] = None
    sender_id: Optional[int] = None
    sender_label: str
    sender_username: str = ""
    sender_display_name: str = ""
    is_outgoing: bool = False
    status: str = "active"
    text: str = ""
    original_text: str = ""
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str = ""
    edit_count: int = 0
    content_type: str = ""
    has_media: bool = False
    has_preview: bool = False
    file_name: str = ""
    reply_to_msg_id: Optional[int] = None
    reply_preview_sender_label: str = ""
    reply_preview_text: str = ""
    reply_preview_status: str = ""
    reply_preview_missing: bool = False
    dialog_type: str = "private"


class AIChatMessagesResponse(BaseModel):
    profile: AIUserProfile
    session_active: bool
    watcher_active: bool
    chat_id: int
    title: str
    username: str = ""
    dialog_type: str = "private"
    has_photo: bool = False
    history_complete: bool = False
    oldest_loaded_msg_id: Optional[int] = None
    newest_loaded_msg_id: Optional[int] = None
    has_more: bool = False
    messages: List[AIChatMessageItem]


class AIArchiveStatusResponse(BaseModel):
    profile: AIUserProfile
    session_active: bool
    watcher_active: bool
    total_dialogs: int = 0
    synced_dialogs: int = 0
    pending_dialogs: int = 0
    history_complete_dialogs: int = 0
    archived_messages: int = 0
    deleted_messages: int = 0
    pending_messages: int = 0
    risk_events_24h: int = 0
    high_risk_chats: int = 0
    high_risk_senders: int = 0
    last_sync_at: str = ""


class AIRiskProfileItem(BaseModel):
    label: str
    profile_kind: str
    profile_id: int
    risk_score: float = 0
    delete_count: int = 0
    edit_count: int = 0
    disappearing_count: int = 0
    night_count: int = 0
    burst_count: int = 0
    last_event_at: str = ""
    summary: str = ""


class AIRiskEventItem(BaseModel):
    signal_type: str
    severity: str = "info"
    score: float = 0
    title: str = ""
    detail: str = ""
    chat_id: Optional[int] = None
    sender_id: Optional[int] = None
    msg_id: Optional[int] = None
    event_at: str = ""


class AIRiskSummaryResponse(BaseModel):
    profile: AIUserProfile
    session_active: bool
    watcher_active: bool
    top_profiles: List[AIRiskProfileItem]
    recent_events: List[AIRiskEventItem]


class AISessionLogoutResponse(BaseModel):
    message: str
    session_closed: bool
    file_removed: bool = False
    watcher_stopped: bool = False
    state_reset: bool = False


def _normalize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(value).decode("ascii")
    return value


def _short_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def _clean_profile_text(value: Any, max_len: int = 120) -> str:
    text = repair_mojibake(value).replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    return _short_text(text, max_len) if text else ""


def _clean_username(value: Any) -> str:
    username = _clean_profile_text(value, 64).lstrip("@")
    return re.sub(r"[^0-9A-Za-z_]", "", username)


def _clean_photo_url(value: Any) -> str:
    url = _clean_profile_text(value, 600)
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return ""


def _build_profile_payload(user_id: int, source: str, user_data: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    user_data = user_data or {}
    payload = payload or {}
    first_name = _clean_profile_text(user_data.get("first_name") or payload.get("first_name"), 80)
    last_name = _clean_profile_text(user_data.get("last_name") or payload.get("last_name"), 80)
    username = _clean_username(user_data.get("username") or payload.get("username"))
    language_code = _clean_profile_text(user_data.get("language_code") or payload.get("language_code"), 16)
    photo_url = _clean_photo_url(user_data.get("photo_url") or payload.get("photo_url"))

    display_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not display_name and username:
        display_name = f"@{username}"
    if not display_name:
        display_name = f"Пользователь {user_id}"

    initials_source = first_name or username or str(user_id)
    if first_name and last_name:
        initials = (first_name[:1] + last_name[:1]).upper()
    else:
        letters = re.findall(r"[A-Za-zА-Яа-яЁё0-9]", initials_source)
        initials = "".join(letters[:2]).upper() if letters else "U"

    return {
        "user_id": int(user_id),
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "photo_url": photo_url,
        "initials": initials or "U",
        "language_code": language_code,
        "source": source,
        "is_premium": bool(user_data.get("is_premium")),
    }


def _format_event_time(value: Any) -> str:
    if not value:
        return "Нет данных"
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        event_dt = datetime.fromisoformat(raw)
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        local_dt = event_dt.astimezone(CONFIG.tz)
        return local_dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return _short_text(str(value), 80)


def _archive_category_from_values(content_type: Any, media_path: Any) -> str:
    ctype = str(content_type or "").lower()
    path = str(media_path or "").lower()

    if any(token in ctype for token in ("голос", "voice", "audio")) or path.endswith((".ogg", ".oga", ".mp3", ".wav", ".m4a")):
        return "voice"
    if any(token in ctype for token in ("видео", "video", "кружочек", "video_note")) or path.endswith((".mp4", ".mov", ".mkv", ".webm")):
        return "video"
    if any(token in ctype for token in ("фото", "photo", "image")) or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")):
        return "photo"
    if not path:
        return "text"
    return "other"


def _archive_category_sql(content_col: str = "content_type", media_col: str = "media_path") -> str:
    return f"""
    CASE
        WHEN lower(COALESCE({content_col}, '')) LIKE '%голос%'
          OR lower(COALESCE({content_col}, '')) LIKE '%voice%'
          OR lower(COALESCE({content_col}, '')) LIKE '%audio%'
          OR lower(COALESCE({media_col}, '')) LIKE '%.ogg'
          OR lower(COALESCE({media_col}, '')) LIKE '%.oga'
          OR lower(COALESCE({media_col}, '')) LIKE '%.mp3'
          OR lower(COALESCE({media_col}, '')) LIKE '%.wav'
          OR lower(COALESCE({media_col}, '')) LIKE '%.m4a'
        THEN 'voice'
        WHEN lower(COALESCE({content_col}, '')) LIKE '%видео%'
          OR lower(COALESCE({content_col}, '')) LIKE '%video%'
          OR lower(COALESCE({content_col}, '')) LIKE '%кружочек%'
          OR lower(COALESCE({content_col}, '')) LIKE '%video_note%'
          OR lower(COALESCE({media_col}, '')) LIKE '%.mp4'
          OR lower(COALESCE({media_col}, '')) LIKE '%.mov'
          OR lower(COALESCE({media_col}, '')) LIKE '%.mkv'
          OR lower(COALESCE({media_col}, '')) LIKE '%.webm'
        THEN 'video'
        WHEN lower(COALESCE({content_col}, '')) LIKE '%фото%'
          OR lower(COALESCE({content_col}, '')) LIKE '%photo%'
          OR lower(COALESCE({content_col}, '')) LIKE '%image%'
          OR lower(COALESCE({media_col}, '')) LIKE '%.jpg'
          OR lower(COALESCE({media_col}, '')) LIKE '%.jpeg'
          OR lower(COALESCE({media_col}, '')) LIKE '%.png'
          OR lower(COALESCE({media_col}, '')) LIKE '%.webp'
          OR lower(COALESCE({media_col}, '')) LIKE '%.bmp'
          OR lower(COALESCE({media_col}, '')) LIKE '%.gif'
        THEN 'photo'
        WHEN TRIM(COALESCE({media_col}, '')) = ''
        THEN 'text'
        ELSE 'other'
    END
    """


def _archive_sender_key(sender_id: Any, sender_username: Any) -> str:
    try:
        if sender_id is not None:
            return f"id:{int(sender_id)}"
    except Exception:
        pass
    username = _clean_profile_text(sender_username, 80).strip().lower()
    if username in {"удалённый", "удаленный", "deleted"}:
        return "unknown"
    if username:
        return f"username:{username}"
    return "unknown"


def _archive_sender_label(sender_id: Any, sender_username: Any) -> str:
    username = _clean_profile_text(sender_username, 80).strip()
    if username.lower() in {"удалённый", "удаленный", "deleted"}:
        username = ""
    if username:
        return f"@{username}" if not username.startswith("@") else username
    try:
        if sender_id is not None:
            return f"ID {int(sender_id)}"
    except Exception:
        pass
    return "Удалённый"


def _archive_order_sql(sort_value: str) -> str:
    normalized = (sort_value or "date_desc").strip().lower()
    if normalized == "date_asc":
        return "saved_at ASC, id ASC"
    if normalized == "type":
        return (
            "CASE category "
            "WHEN 'text' THEN 1 "
            "WHEN 'photo' THEN 2 "
            "WHEN 'video' THEN 3 "
            "WHEN 'voice' THEN 4 "
            "ELSE 5 END ASC, "
            "saved_at DESC, id DESC"
        )
    return "saved_at DESC, id DESC"


def _thread_status_value(value: Any) -> str:
    normalized = str(value or "active").strip().lower()
    if normalized in {"all", "active", "deleted", "edited", "changed"}:
        return normalized
    return "active"


def _thread_order_sql(sort_value: str) -> str:
    normalized = (sort_value or "date_desc").strip().lower()
    if normalized == "date_asc":
        return "event_ts ASC, id ASC"
    if normalized == "type":
        return (
            "CASE status_norm "
            "WHEN 'active' THEN 1 "
            "WHEN 'edited' THEN 2 "
            "WHEN 'deleted' THEN 3 "
            "ELSE 4 END ASC, "
            "event_ts DESC, id DESC"
        )
    return "event_ts DESC, id DESC"


def _archive_sender_where(sender_key: str) -> Tuple[str, List[Any]]:
    normalized = (sender_key or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Выберите пользователя из списка.")
    if normalized.startswith("id:"):
        try:
            sender_id = int(normalized.split(":", 1)[1])
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Некорректный sender_key.") from exc
        return "sender_id = ?", [sender_id]
    if normalized.startswith("username:"):
        username = normalized.split(":", 1)[1].strip().lower()
        if not username:
            raise HTTPException(status_code=400, detail="Некорректный sender_key.")
        return "sender_id IS NULL AND lower(trim(COALESCE(sender_username, ''))) = ?", [username]
    if normalized == "unknown":
        return (
            "sender_id IS NULL AND (trim(COALESCE(sender_username, '')) = '' "
            "OR lower(trim(COALESCE(sender_username, ''))) IN ('удалённый', 'удаленный', 'deleted'))",
            [],
        )
    raise HTTPException(status_code=400, detail="Некорректный sender_key.")


def _archive_send_method(content_type: Any, media_path: Any) -> Tuple[str, str]:
    category = _archive_category_from_values(content_type, media_path)
    if category == "photo":
        return "sendPhoto", "photo"
    if category == "video":
        return "sendVideo", "video"
    if category == "voice":
        return "sendVoice", "voice"
    return "sendDocument", "document"


def _can_send_to_chat(category: str, text_value: str, has_media: bool) -> bool:
    normalized = str(category or "").strip().lower()
    if normalized == "text":
        return bool(str(text_value or "").strip())
    if normalized in {"voice", "video"}:
        return bool(has_media)
    return False


def _verify_telegram_init_data(init_data: str) -> Tuple[Dict[str, str], Dict[str, Any]]:
    import hashlib
    import hmac
    from urllib.parse import parse_qsl

    if not CONFIG.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен.")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    init_hash = parsed.pop("hash", None)
    if not init_hash:
        raise HTTPException(status_code=401, detail="Некорректный Telegram initData.")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData",
        CONFIG.bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, init_hash):
        raise HTTPException(status_code=401, detail="Подпись Mini App не прошла проверку.")

    auth_date_raw = parsed.get("auth_date")
    if auth_date_raw:
        try:
            auth_date = int(auth_date_raw)
            if abs(int(time.time()) - auth_date) > AI_INITDATA_MAX_AGE_SEC:
                raise HTTPException(
                    status_code=401,
                    detail="Сессия Mini App устарела. Откройте приложение заново.",
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Некорректный auth_date в initData.")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Пользователь не найден в initData.")

    try:
        user_data = json.loads(user_raw)
        user_id = int(user_data.get("id"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Некорректный профиль пользователя.") from exc

    if user_id <= 0:
        raise HTTPException(status_code=401, detail="Некорректный идентификатор пользователя.")

    return parsed, user_data


def _resolve_identity(payload: Dict[str, Any]) -> AIIdentityContext:
    """
    Resolve user identity from Telegram initData or fallback to local user_id.
    This is called on EVERY API request - logging is critical for debugging.
    """
    init_data = str(payload.get("init_data") or "").strip()
    local_user_id = payload.get("user_id")
    
    # Try Telegram auth first
    if init_data:
        try:
            _, user_data = _verify_telegram_init_data(init_data)
            user_id = int(user_data["id"])
            logger.debug("✅ Telegram auth successful for user_id=%s", user_id)
            
            if not is_user_subscription_active_sync(user_id):
                logger.warning("❌ User %s subscription inactive", user_id)
                raise HTTPException(
                    status_code=402,
                    detail="Подписка неактивна. Откройте бота и оплатите тариф командой /plans.",
                )
            
            REQUEST_CONTEXT_USER_ID.set(user_id)
            return AIIdentityContext(
                user_id=user_id,
                profile=_build_profile_payload(user_id, source="telegram", user_data=user_data),
                source="telegram",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("❌ Telegram auth failed: %s", str(e), exc_info=True)
            raise HTTPException(status_code=401, detail="Ошибка валидации Telegram подписи.")

    # Fallback to local user_id (for development/testing)
    if AI_ALLOW_LOCAL_USER_ID and local_user_id is not None:
        try:
            user_id = int(local_user_id)
            logger.debug("⚠️ Using local auth mode for user_id=%s (dev only)", user_id)
        except Exception as exc:
            logger.warning("❌ Invalid local user_id: %s", local_user_id)
            raise HTTPException(status_code=400, detail="Некорректный user_id.") from exc
        
        if user_id <= 0:
            raise HTTPException(status_code=400, detail="Некорректный user_id.")
        
        if CONFIG.admin_ids and user_id not in CONFIG.admin_ids:
            logger.warning("❌ Unauthorized local access attempt for user_id=%s (not in admin_ids)", user_id)
            raise HTTPException(
                status_code=401,
                detail="Локальный режим разрешен только для admin user_id. Откройте Mini App через Telegram.",
            )
        
        if not is_user_subscription_active_sync(user_id):
            logger.warning("❌ Local user %s subscription inactive", user_id)
            raise HTTPException(
                status_code=402,
                detail="Подписка неактивна. Откройте бота и оплатите тариф командой /plans.",
            )
        
        REQUEST_CONTEXT_USER_ID.set(user_id)
        return AIIdentityContext(
            user_id=user_id,
            profile=_build_profile_payload(user_id, source="local", payload=payload),
            source="local",
        )

    # No auth data provided
    logger.error(
        "❌ CRITICAL: No auth data provided. initData=%s, local_user_id=%s, AI_ALLOW_LOCAL_USER_ID=%s",
        "provided" if init_data else "empty",
        local_user_id,
        AI_ALLOW_LOCAL_USER_ID
    )
    raise HTTPException(
        status_code=401,
        detail="Не удалось определить пользователя. Откройте Mini App через кнопку в Telegram.",
    )


def _resolve_user_id(payload: Dict[str, Any]) -> int:
    return _resolve_identity(payload).user_id


ai_app.include_router(create_diagnostics_router(_resolve_identity, ANOMALY_SERVICE))


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=? LIMIT 1",
        (table_name,),
    ) as cur:
        return bool(await cur.fetchone())


async def _table_columns(conn: aiosqlite.Connection, table_name: str) -> List[str]:
    async with conn.execute(f"PRAGMA table_info({table_name})") as cur:
        rows = await cur.fetchall()
    return [str(row[1]) for row in rows] if rows else []


def _pick_column(columns: List[str], *candidates: str) -> Optional[str]:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


async def _create_messages_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.messages")
    if await _table_exists(conn, "messages"):
        cols = await _table_columns(conn, "messages")
        id_col = _pick_column(cols, "id")
        user_col = _pick_column(cols, "user_id")
        chat_col = _pick_column(cols, "chat_id")
        text_col = _pick_column(cols, "text")
        date_col = _pick_column(cols, "date", "message_date", "saved_at")
        if user_col:
            await conn.execute(
                f"""
                CREATE TEMP VIEW messages AS
                SELECT
                    {id_col if id_col else 'NULL'} AS id,
                    {user_col} AS user_id,
                    {chat_col if chat_col else 'NULL'} AS chat_id,
                    COALESCE({text_col if text_col else "''"}, '') AS text,
                    {date_col if date_col else 'NULL'} AS date
                FROM main.messages
                WHERE {user_col} = {int(user_id)}
                """
            )
            return

    if await _table_exists(conn, "pending"):
        await conn.execute(
            f"""
            CREATE TEMP VIEW messages AS
            SELECT
                id AS id,
                owner_id AS user_id,
                chat_id AS chat_id,
                COALESCE(text, '') AS text,
                COALESCE(message_date, added_at) AS date
            FROM main.pending
            WHERE owner_id = {int(user_id)}
            """
        )
        return

    await conn.execute(
        """
        CREATE TEMP VIEW messages AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            NULL AS chat_id,
            '' AS text,
            NULL AS date
        WHERE 0
        """
    )


async def _create_deleted_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.deleted_messages")
    if await _table_exists(conn, "deleted_messages"):
        cols = await _table_columns(conn, "deleted_messages")
        id_col = _pick_column(cols, "id")
        user_col = _pick_column(cols, "user_id", "owner_id")
        chat_col = _pick_column(cols, "chat_id")
        text_col = _pick_column(cols, "text", "text_full", "text_preview", "original_text_full", "original_text_preview")
        date_col = _pick_column(cols, "date", "saved_at", "original_timestamp")
        chat_title_col = _pick_column(cols, "chat_title")
        sender_col = _pick_column(cols, "sender_username")
        content_col = _pick_column(cols, "content_type")
        if user_col:
            await conn.execute(
                f"""
                CREATE TEMP VIEW deleted_messages AS
                SELECT
                    {id_col if id_col else 'NULL'} AS id,
                    {user_col} AS user_id,
                    {chat_col if chat_col else 'NULL'} AS chat_id,
                    COALESCE({text_col if text_col else "''"}, '') AS text,
                    {date_col if date_col else 'NULL'} AS date,
                    COALESCE({chat_title_col if chat_title_col else "''"}, '') AS chat_title,
                    COALESCE({sender_col if sender_col else "''"}, '') AS sender_username,
                    COALESCE({content_col if content_col else "''"}, '') AS content_type
                FROM main.deleted_messages
                WHERE {user_col} = {int(user_id)}
                """
            )
            return

    await conn.execute(
        """
        CREATE TEMP VIEW deleted_messages AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            NULL AS chat_id,
            '' AS text,
            NULL AS date,
            '' AS chat_title,
            '' AS sender_username,
            '' AS content_type
        WHERE 0
        """
    )


async def _create_chat_messages_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.chat_messages")
    if await _table_exists(conn, "chat_thread_messages"):
        await conn.execute(
            f"""
            CREATE TEMP VIEW chat_messages AS
            SELECT
                id AS id,
                owner_id AS user_id,
                chat_id AS chat_id,
                COALESCE(msg_id, 0) AS msg_id,
                sender_id AS sender_id,
                COALESCE(sender_username, '') AS sender_username,
                COALESCE(text, '') AS text,
                COALESCE(status, 'active') AS status,
                COALESCE(created_at, updated_at, deleted_at) AS created_at,
                COALESCE(content_type, '') AS content_type
            FROM main.chat_thread_messages
            WHERE owner_id = {int(user_id)}
            """
        )
        return

    await conn.execute(
        """
        CREATE TEMP VIEW chat_messages AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            NULL AS chat_id,
            NULL AS msg_id,
            NULL AS sender_id,
            '' AS sender_username,
            '' AS text,
            'active' AS status,
            NULL AS created_at,
            '' AS content_type
        WHERE 0
        """
    )


async def _create_chat_dialogs_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.chat_dialogs")
    if await _table_exists(conn, "chat_dialogs"):
        await conn.execute(
            f"""
            CREATE TEMP VIEW chat_dialogs AS
            SELECT
                chat_id AS chat_id,
                owner_id AS user_id,
                COALESCE(title, '') AS title,
                COALESCE(username, '') AS username,
                COALESCE(dialog_type, 'private') AS dialog_type,
                COALESCE(last_message_at, '') AS last_message_at,
                COALESCE(history_complete, 0) AS history_complete
            FROM main.chat_dialogs
            WHERE owner_id = {int(user_id)}
            """
        )
        return

    await conn.execute(
        """
        CREATE TEMP VIEW chat_dialogs AS
        SELECT
            NULL AS chat_id,
            NULL AS user_id,
            '' AS title,
            '' AS username,
            'private' AS dialog_type,
            NULL AS last_message_at,
            0 AS history_complete
        WHERE 0
        """
    )


async def _create_risk_events_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.risk_events")
    if await _table_exists(conn, "risk_events"):
        await conn.execute(
            f"""
            CREATE TEMP VIEW risk_events AS
            SELECT
                id AS id,
                owner_id AS user_id,
                chat_id AS chat_id,
                sender_id AS sender_id,
                msg_id AS msg_id,
                COALESCE(signal_type, '') AS signal_type,
                COALESCE(severity, 'info') AS severity,
                COALESCE(score, 0) AS score,
                COALESCE(title, '') AS title,
                COALESCE(detail, '') AS detail,
                COALESCE(event_at, created_at) AS event_at
            FROM main.risk_events
            WHERE owner_id = {int(user_id)}
            """
        )
        return

    await conn.execute(
        """
        CREATE TEMP VIEW risk_events AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            NULL AS chat_id,
            NULL AS sender_id,
            NULL AS msg_id,
            '' AS signal_type,
            'info' AS severity,
            0 AS score,
            '' AS title,
            '' AS detail,
            NULL AS event_at
        WHERE 0
        """
    )


async def _create_risk_profiles_view(conn: aiosqlite.Connection, user_id: int) -> None:
    await conn.execute("DROP VIEW IF EXISTS temp.risk_profiles")
    if await _table_exists(conn, "risk_profiles"):
        await conn.execute(
            f"""
            CREATE TEMP VIEW risk_profiles AS
            SELECT
                id AS id,
                owner_id AS user_id,
                COALESCE(profile_kind, '') AS profile_kind,
                profile_id AS profile_id,
                COALESCE(risk_score, 0) AS risk_score,
                COALESCE(delete_count, 0) AS delete_count,
                COALESCE(edit_count, 0) AS edit_count,
                COALESCE(disappearing_count, 0) AS disappearing_count,
                COALESCE(night_count, 0) AS night_count,
                COALESCE(burst_count, 0) AS burst_count,
                COALESCE(last_event_at, '') AS last_event_at,
                COALESCE(summary, '') AS summary
            FROM main.risk_profiles
            WHERE owner_id = {int(user_id)}
            """
        )
        return

    await conn.execute(
        """
        CREATE TEMP VIEW risk_profiles AS
        SELECT
            NULL AS id,
            NULL AS user_id,
            '' AS profile_kind,
            NULL AS profile_id,
            0 AS risk_score,
            0 AS delete_count,
            0 AS edit_count,
            0 AS disappearing_count,
            0 AS night_count,
            0 AS burst_count,
            NULL AS last_event_at,
            '' AS summary
        WHERE 0
        """
    )


async def _prepare_user_views(conn: aiosqlite.Connection, user_id: int) -> None:
    await _create_messages_view(conn, user_id)
    await _create_deleted_view(conn, user_id)
    await _create_chat_messages_view(conn, user_id)
    await _create_chat_dialogs_view(conn, user_id)
    await _create_risk_events_view(conn, user_id)
    await _create_risk_profiles_view(conn, user_id)


def sanitize_sql(query: str) -> str:
    if not query:
        raise HTTPException(status_code=400, detail="Пустой SQL-запрос.")
    trimmed = query.strip()
    trimmed = trimmed.rstrip(";").strip()
    if ";" in trimmed:
        raise HTTPException(status_code=400, detail="Разрешён только один SELECT-запрос.")
    if not re.match(r"(?i)^select\b", trimmed):
        raise HTTPException(status_code=400, detail="Допустимы только SELECT-запросы.")
    if "--" in trimmed or "/*" in trimmed or "*/" in trimmed:
        raise HTTPException(status_code=400, detail="SQL-комментарии запрещены.")
    for keyword in AI_FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", trimmed, re.IGNORECASE):
            raise HTTPException(status_code=400, detail=f"Запрещённый оператор в SQL: {keyword}.")
    if re.search(r"(?i)\b(?:main|temp|sqlite_master|sqlite_temp_master)\s*\.", trimmed):
        raise HTTPException(status_code=400, detail="Прямой доступ к системным схемам запрещен.")
    found_tables = {
        tbl.lower().strip('`"')
        for tbl in re.findall(r"(?i)\b(?:from|join)\s+([a-zA-Z0-9_]+)", trimmed)
        if tbl
    }
    invalid_tables = found_tables - AI_ALLOWED_TABLES
    if invalid_tables:
        raise HTTPException(
            status_code=400,
            detail=f"Разрешены только таблицы: {', '.join(sorted(AI_ALLOWED_TABLES))}. "
            f"Найдено: {', '.join(sorted(invalid_tables))}.",
        )
    return trimmed


def _extract_sql(content: str) -> str:
    text = (content or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="AI не вернул SQL-запрос.")
    if text.lower().startswith("sql:"):
        text = text.split(":", 1)[1].strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
    match = re.search(r"(?is)(select\b.*)", text)
    sql_candidate = match.group(1) if match else text
    if ";" in sql_candidate:
        sql_candidate = sql_candidate[: sql_candidate.find(";") + 1]
    return sanitize_sql(sql_candidate)


async def _openrouter_chat(
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0,
    max_tokens: int = 600,
) -> List[Dict[str, Any]]:
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY не настроен.")
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }
    last_error: Optional[str] = None
    for attempt in range(1, max(1, AI_OPENROUTER_RETRIES) + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        except httpx.RequestError as exc:
            last_error = str(exc)
            if attempt < AI_OPENROUTER_RETRIES:
                await asyncio.sleep(0.6 * attempt)
                continue
            raise HTTPException(status_code=502, detail=f"OpenRouter network error: {last_error}")

        if response.status_code in (429, 500, 502, 503, 504) and attempt < AI_OPENROUTER_RETRIES:
            await asyncio.sleep(0.6 * attempt)
            continue

        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase or "OpenRouter response error."
            logger.error("OpenRouter returned %s: %s", response.status_code, detail)
            raise HTTPException(status_code=502, detail=f"OpenRouter error ({response.status_code}): {detail}")

        try:
            data = response.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="OpenRouter вернул некорректный JSON.")
        choices = data.get("choices") or []
        if not isinstance(choices, list):
            raise HTTPException(status_code=502, detail="OpenRouter не вернул choices.")
        return choices

    raise HTTPException(status_code=502, detail=f"OpenRouter unavailable: {last_error or 'unknown error'}")


async def generate_sql(question: str) -> str:
    safe_question = _short_text(question.strip(), 700)
    prompt = [
        {"role": "system", "content": AI_SYSTEM_PROMPT},
        {"role": "user", "content": f"Вопрос: {safe_question}"},
    ]
    try:
        choices = await _openrouter_chat(prompt, temperature=0, max_tokens=600)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("OpenRouter SQL generation failed")
        raise HTTPException(status_code=502, detail="Не удалось сгенерировать SQL через OpenRouter.") from exc
    if not choices:
        raise HTTPException(status_code=502, detail="OpenRouter не вернул содержимое.")
    content = choices[0].get("message", {}).get("content", "")
    sql = _extract_sql(content)
    logger.info("AI SQL generated: %s", sql)
    return sql


async def run_sql(query: str, user_id: int) -> Dict[str, Any]:
    sanitized = sanitize_sql(query)
    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await _prepare_user_views(conn, user_id)
        await conn.execute("PRAGMA busy_timeout = 5000")
        try:
            async with conn.execute(sanitized) as cur:
                columns = [desc[0] for desc in (cur.description or [])]
                rows = []
                row_count = 0
                truncated = False
                while True:
                    row = await cur.fetchone()
                    if row is None:
                        break
                    row_count += 1
                    if len(rows) < AI_MAX_RESULT_ROWS:
                        row_dict = {col: _normalize_value(row[col]) for col in columns}
                        rows.append(row_dict)
                    else:
                        truncated = True
                        break
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": row_count,
                    "truncated": truncated,
                    "limit": AI_MAX_RESULT_ROWS,
                }
        except sqlite3.Error as exc:
            logger.exception("AI SQL execution failed")
            await ANOMALY_SERVICE.record_anomaly(
                endpoint="/ai/sql",
                user_id=user_id,
                category="sql_execution_error",
                details={"error": str(exc)[:300], "query": sanitized[:400]},
                note="SQLite query execution failed",
            )
            raise HTTPException(status_code=400, detail=f"SQL execution error: {exc}")


async def explain_result(question: str, sql: str, result: Dict[str, Any]) -> str:
    if not OPENROUTER_API_KEY:
        return "AI-ассистент недоступен без OPENROUTER_API_KEY."
    payload = {
        "question": question,
        "sql": sql,
        "result": result,
    }
    prompt = [
        {"role": "system", "content": AI_RESULT_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        },
    ]
    try:
        choices = await _openrouter_chat(prompt, temperature=0.3, max_tokens=400)
    except HTTPException as exc:
        logger.exception("OpenRouter explain_result failed: %s", exc.detail)
        return str(exc.detail or "Не удалось получить пояснение от AI.")
    except Exception:
        logger.exception("OpenRouter explain_result unexpected failure")
        return "Не удалось получить пояснение от AI."
    if not choices:
        return "AI не вернул объяснение."
    reply = choices[0].get("message", {}).get("content", "").strip()
    if not reply:
        return "AI не вернул объяснение."
    return _short_text(reply, AI_MAX_ANSWER_CHARS)


class _RetryableStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"retryable status={status_code} {body[:240]}")
        self.status_code = int(status_code)
        self.body = body


async def _telegram_bot_api_get(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not CONFIG.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured.")

    url = f"https://api.telegram.org/bot{CONFIG.bot_token}/{method}"

    async def _request() -> httpx.Response:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params)
        if response.status_code in {429, 500, 502, 503, 504}:
            raise _RetryableStatusError(response.status_code, response.text)
        return response

    try:
        response = await RETRY_SERVICE.execute(
            _request,
            operation_name=f"telegram.{method}",
            attempts=3,
            retry_for=(httpx.RequestError, _RetryableStatusError),
        )
    except (httpx.RequestError, _RetryableStatusError) as exc:
        logger.warning("Telegram Bot API request failed for %s: %s", method, exc)
        raise HTTPException(status_code=502, detail="Failed to load profile data from Telegram.") from exc

    if response.status_code >= 400:
        logger.warning("Telegram Bot API returned %s for %s: %s", response.status_code, method, response.text[:300])
        raise HTTPException(status_code=502, detail="Telegram Bot API returned an error response.")

    payload = response.json()
    if not payload.get("ok"):
        logger.warning("Telegram Bot API error for %s: %s", method, payload)
        raise HTTPException(status_code=502, detail="Telegram Bot API response is not OK.")
    return payload.get("result") or {}


async def _fetch_avatar_bytes(user_id: int) -> Optional[Tuple[bytes, str]]:
    cached = AI_AVATAR_CACHE.get(user_id)
    now_ts = time.time()
    if cached and now_ts - float(cached.get("ts", 0)) < AI_AVATAR_CACHE_TTL_SEC:
        return cached["content"], cached["media_type"]

    result = await _telegram_bot_api_get("getUserProfilePhotos", {"user_id": user_id, "limit": 1})
    photos = result.get("photos") or []
    if not photos:
        return None

    best_photo = photos[0][-1] if photos[0] else None
    if not best_photo or not best_photo.get("file_id"):
        return None

    file_result = await _telegram_bot_api_get("getFile", {"file_id": best_photo["file_id"]})
    file_path = str(file_result.get("file_path") or "").strip()
    if not file_path:
        return None

    file_url = f"https://api.telegram.org/file/bot{CONFIG.bot_token}/{file_path}"

    async def _download_avatar() -> httpx.Response:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(file_url)
        if response.status_code in {429, 500, 502, 503, 504}:
            raise _RetryableStatusError(response.status_code, response.text)
        return response

    try:
        response = await RETRY_SERVICE.execute(
            _download_avatar,
            operation_name="telegram.avatar.download",
            attempts=3,
            retry_for=(httpx.RequestError, _RetryableStatusError),
        )
    except (httpx.RequestError, _RetryableStatusError) as exc:
        logger.warning("Telegram avatar download failed for user %s: %s", user_id, exc)
        return None

    if response.status_code >= 400 or not response.content:
        logger.warning("Telegram avatar download returned %s for user %s", response.status_code, user_id)
        return None

    content = response.content
    if len(content) > AI_AVATAR_MAX_BYTES:
        logger.warning("Avatar for user %s exceeds limit: %s bytes", user_id, len(content))
        return None

    media_type = response.headers.get("content-type") or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    AI_AVATAR_CACHE[user_id] = {"ts": now_ts, "content": content, "media_type": media_type}
    return content, media_type


def _runtime_flags(user_id: int) -> Tuple[bool, bool]:
    session_active = False
    watcher_active = False
    if BOT_RUNTIME_APP is not None:
        try:
            session_active = bool(BOT_RUNTIME_APP.storage.is_valid(user_id))
        except Exception:
            session_active = False
        try:
            watcher_active = user_id in BOT_RUNTIME_APP.watcher_service.watched_clients
        except Exception:
            watcher_active = False
    return session_active, watcher_active


def _cache_key(namespace: str, user_id: int, *parts: Any) -> Tuple[Any, ...]:
    return (namespace, int(user_id), *parts)


def _invalidate_user_cache(user_id: int) -> None:
    AI_RESPONSE_CACHE.invalidate(lambda key: isinstance(key, tuple) and len(key) > 1 and int(key[1]) == int(user_id))


async def _normalize_media_reference(
    *,
    table_name: str,
    item_id: int,
    owner_id: int,
    media_path: str,
    endpoint: str,
) -> str:
    path = str(media_path or "").strip()
    if not path:
        return ""
    if os.path.exists(path):
        return path

    await ANOMALY_SERVICE.record_anomaly(
        endpoint=endpoint,
        user_id=owner_id,
        category="missing_media_path",
        details={
            "table_name": table_name,
            "item_id": item_id,
            "media_path": path,
        },
        note="Media path points to a missing file",
    )

    current_fix_count = int(REQUEST_CONTEXT_AUTOFIX_COUNT.get(0) or 0)
    if AI_AUTOFIX_ON_READ and current_fix_count < max(1, AI_AUTOFIX_MAX_PER_REQUEST):
        try:
            fixed = await ANOMALY_SERVICE.auto_fix_single_missing_media(
                table_name=table_name,
                item_id=item_id,
                owner_id=owner_id,
                media_path=path,
                reason=f"{endpoint}: media file missing at read time",
            )
            if fixed:
                REQUEST_CONTEXT_AUTOFIX_COUNT.set(current_fix_count + 1)
        except Exception:
            logger.exception(
                "Auto-fix failed for missing media_path table=%s item_id=%s owner=%s",
                table_name,
                item_id,
                owner_id,
            )
    return ""


def _deleted_text_expr(columns: List[str]) -> str:
    if "text_full" in columns:
        return "COALESCE(text_full, text_preview, '')"
    if "text_preview" in columns:
        return "COALESCE(text_preview, '')"
    return "''"


def _deleted_original_text_expr(columns: List[str]) -> str:
    if "original_text_full" in columns:
        return "COALESCE(original_text_full, original_text_preview, '')"
    if "original_text_preview" in columns:
        return "COALESCE(original_text_preview, '')"
    return "''"


async def _archive_user_directory(identity: AIIdentityContext) -> Dict[str, Any]:
    user_id = identity.user_id
    profile = dict(identity.profile)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        columns = await _table_columns(conn, "deleted_messages")
        if not columns:
            session_active, watcher_active = _runtime_flags(user_id)
            return {
                "profile": profile,
                "session_active": session_active,
                "watcher_active": watcher_active,
                "users": [],
            }

        category_sql = _archive_category_sql()
        query = f"""
        WITH archive_rows AS (
            SELECT
                sender_id,
                COALESCE(sender_username, '') AS sender_username,
                COALESCE(saved_at, original_timestamp, '') AS saved_at,
                {category_sql} AS category
            FROM deleted_messages
            WHERE owner_id = ?
        )
        SELECT
            CASE
                WHEN sender_id IS NOT NULL THEN 'id:' || sender_id
                WHEN TRIM(sender_username) <> '' AND lower(TRIM(sender_username)) NOT IN ('удалённый', 'удаленный', 'deleted')
                THEN 'username:' || lower(TRIM(sender_username))
                ELSE 'unknown'
            END AS sender_key,
            MAX(sender_id) AS sender_id,
            MAX(
                CASE
                    WHEN TRIM(sender_username) <> '' AND lower(TRIM(sender_username)) NOT IN ('удалённый', 'удаленный', 'deleted')
                    THEN sender_username
                    ELSE ''
                END
            ) AS sender_username,
            COUNT(*) AS total_deleted,
            SUM(CASE WHEN category = 'text' THEN 1 ELSE 0 END) AS text_count,
            SUM(CASE WHEN category = 'photo' THEN 1 ELSE 0 END) AS photo_count,
            SUM(CASE WHEN category = 'video' THEN 1 ELSE 0 END) AS video_count,
            SUM(CASE WHEN category = 'voice' THEN 1 ELSE 0 END) AS voice_count,
            SUM(CASE WHEN category = 'other' THEN 1 ELSE 0 END) AS other_count,
            MAX(saved_at) AS last_deleted_at
        FROM archive_rows
        GROUP BY sender_key
        ORDER BY total_deleted DESC, last_deleted_at DESC
        """
        async with conn.execute(query, (user_id,)) as cur:
            rows = await cur.fetchall()

    users = [
        {
            "sender_key": str(row["sender_key"] or ""),
            "sender_id": int(row["sender_id"]) if row["sender_id"] is not None else None,
            "username": _clean_profile_text(row["sender_username"], 80),
            "display_name": _archive_sender_label(row["sender_id"], row["sender_username"]),
            "total_deleted": int(row["total_deleted"] or 0),
            "text_count": int(row["text_count"] or 0),
            "photo_count": int(row["photo_count"] or 0),
            "video_count": int(row["video_count"] or 0),
            "voice_count": int(row["voice_count"] or 0),
            "other_count": int(row["other_count"] or 0),
            "last_deleted_at": _format_event_time(row["last_deleted_at"]),
        }
        for row in rows
    ]

    session_active, watcher_active = _runtime_flags(user_id)
    return {
        "profile": profile,
        "session_active": session_active,
        "watcher_active": watcher_active,
        "users": users,
    }


async def _archive_items(identity: AIIdentityContext, payload: AIArchiveListRequest) -> Dict[str, Any]:
    user_id = identity.user_id
    sender_clause, sender_params = _archive_sender_where(payload.sender_key)
    category = (payload.category or "all").strip().lower()
    if category not in {"all", "text", "photo", "video", "voice"}:
        raise HTTPException(status_code=400, detail="Некорректная категория.")

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        columns = await _table_columns(conn, "deleted_messages")
        if not columns:
            return {
                "selected_sender_key": payload.sender_key,
                "category": category,
                "sort": payload.sort,
                "total": 0,
                "offset": payload.offset,
                "limit": payload.limit,
                "has_more": False,
                "items": [],
            }

        text_expr = _deleted_text_expr(columns)
        original_text_expr = _deleted_original_text_expr(columns)
        category_sql = _archive_category_sql()
        base_filters = ["owner_id = ?"]
        params: List[Any] = [user_id]

        search_value = (payload.search or "").strip().lower()
        if search_value:
            like_value = f"%{search_value}%"
            base_filters.append(
                f"(lower({text_expr}) LIKE ? OR lower({original_text_expr}) LIKE ? OR lower(COALESCE(chat_title, '')) LIKE ?)"
            )
            params.extend([like_value, like_value, like_value])

        cte = f"""
        WITH archive_rows AS (
            SELECT
                id,
                sender_id,
                COALESCE(sender_username, '') AS sender_username,
                COALESCE(chat_title, '') AS chat_title,
                COALESCE(content_type, '') AS content_type,
                COALESCE(media_path, '') AS media_path,
                COALESCE(saved_at, original_timestamp, '') AS saved_at,
                {text_expr} AS text_value,
                {original_text_expr} AS original_text_value,
                {category_sql} AS category,
                CASE
                    WHEN sender_id IS NOT NULL THEN 'id:' || sender_id
                    WHEN TRIM(COALESCE(sender_username, '')) <> '' AND lower(TRIM(sender_username)) NOT IN ('удалённый', 'удаленный', 'deleted')
                    THEN 'username:' || lower(TRIM(sender_username))
                    ELSE 'unknown'
                END AS sender_key
            FROM deleted_messages
            WHERE {' AND '.join(base_filters)}
        )
        """

        outer_filters = [sender_clause]
        outer_params = list(sender_params)
        if category != "all":
            outer_filters.append("category = ?")
            outer_params.append(category)
        outer_where = " AND ".join(outer_filters)

        count_query = cte + f"SELECT COUNT(*) AS total_count FROM archive_rows WHERE {outer_where}"
        async with conn.execute(count_query, tuple(params + outer_params)) as cur:
            total_row = await cur.fetchone()
        total = int(total_row["total_count"] or 0) if total_row else 0

        order_sql = _archive_order_sql(payload.sort)
        data_query = (
            cte
            + f"""
            SELECT
                id,
                sender_id,
                sender_username,
                chat_title,
                content_type,
                media_path,
                saved_at,
                text_value,
                original_text_value,
                category,
                sender_key
            FROM archive_rows
            WHERE {outer_where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """
        )
        query_params = params + outer_params + [payload.limit, payload.offset]
        async with conn.execute(data_query, tuple(query_params)) as cur:
            rows = await cur.fetchall()

    items: List[Dict[str, Any]] = []
    for row in rows:
        media_path = await _normalize_media_reference(
            table_name="deleted_messages",
            item_id=int(row["id"]),
            owner_id=user_id,
            media_path=str(row["media_path"] or ""),
            endpoint="/ai/archive/items",
        )
        category_name = str(row["category"] or "other")
        has_media = bool(media_path)
        items.append(
            {
                "item_id": int(row["id"]),
                "sender_key": str(row["sender_key"] or ""),
                "sender_label": _archive_sender_label(row["sender_id"], row["sender_username"]),
                "sender_username": _clean_profile_text(row["sender_username"], 80),
                "chat_title": _clean_profile_text(row["chat_title"], 120) or "Личный чат",
                "text": _clean_profile_text(row["text_value"], 4000),
                "original_text": _clean_profile_text(row["original_text_value"], 4000),
                "deleted_at": _format_event_time(row["saved_at"]),
                "content_type": _clean_profile_text(row["content_type"], 80) or "Сообщение",
                "category": category_name,
                "has_media": has_media,
                "has_preview": has_media and category_name == "photo",
                "can_send_to_chat": _can_send_to_chat(category_name, _clean_profile_text(row["text_value"], 4000), has_media),
                "file_name": os.path.basename(media_path) if media_path else "",
            }
        )

    next_offset = payload.offset + len(items)
    return {
        "selected_sender_key": payload.sender_key,
        "category": category,
        "sort": payload.sort,
        "total": total,
        "offset": payload.offset,
        "limit": payload.limit,
        "has_more": next_offset < total,
        "items": items,
    }


async def _thread_user_directory(identity: AIIdentityContext) -> Dict[str, Any]:
    user_id = identity.user_id
    profile = dict(identity.profile)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        columns = await _table_columns(conn, "chat_thread_messages")
        if not columns:
            session_active, watcher_active = _runtime_flags(user_id)
            return {
                "profile": profile,
                "session_active": session_active,
                "watcher_active": watcher_active,
                "users": [],
            }

        query = """
        WITH thread_rows AS (
            SELECT
                sender_id,
                COALESCE(sender_username, '') AS sender_username,
                lower(trim(COALESCE(status, 'active'))) AS status_norm,
                COALESCE(created_at, updated_at, deleted_at, '') AS event_ts
            FROM chat_thread_messages
            WHERE owner_id = ?
        )
        SELECT
            CASE
                WHEN sender_id IS NOT NULL THEN 'id:' || sender_id
                WHEN TRIM(sender_username) <> '' AND lower(TRIM(sender_username)) NOT IN ('удалённый', 'удаленный', 'deleted')
                THEN 'username:' || lower(TRIM(sender_username))
                ELSE 'unknown'
            END AS sender_key,
            MAX(sender_id) AS sender_id,
            MAX(
                CASE
                    WHEN TRIM(sender_username) <> '' AND lower(TRIM(sender_username)) NOT IN ('удалённый', 'удаленный', 'deleted')
                    THEN sender_username
                    ELSE ''
                END
            ) AS sender_username,
            COUNT(*) AS total_count,
            SUM(CASE WHEN status_norm = 'active' THEN 1 ELSE 0 END) AS active_count,
            SUM(CASE WHEN status_norm = 'deleted' THEN 1 ELSE 0 END) AS deleted_count,
            SUM(CASE WHEN status_norm = 'edited' THEN 1 ELSE 0 END) AS edited_count,
            MAX(event_ts) AS last_message_at
        FROM thread_rows
        GROUP BY sender_key
        ORDER BY total_count DESC, last_message_at DESC
        """
        async with conn.execute(query, (user_id,)) as cur:
            rows = await cur.fetchall()

    users = [
        {
            "sender_key": str(row["sender_key"] or ""),
            "sender_id": int(row["sender_id"]) if row["sender_id"] is not None else None,
            "username": _clean_profile_text(row["sender_username"], 80),
            "display_name": _archive_sender_label(row["sender_id"], row["sender_username"]),
            "total_count": int(row["total_count"] or 0),
            "active_count": int(row["active_count"] or 0),
            "deleted_count": int(row["deleted_count"] or 0),
            "edited_count": int(row["edited_count"] or 0),
            "last_message_at": _format_event_time(row["last_message_at"]),
        }
        for row in rows
    ]

    session_active, watcher_active = _runtime_flags(user_id)
    return {
        "profile": profile,
        "session_active": session_active,
        "watcher_active": watcher_active,
        "users": users,
    }


async def _thread_items(identity: AIIdentityContext, payload: AIThreadListRequest) -> Dict[str, Any]:
    user_id = identity.user_id
    sender_clause, sender_params = _archive_sender_where(payload.sender_key)
    status_filter = _thread_status_value(payload.status)
    search_value = (payload.search or "").strip().lower()

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        columns = await _table_columns(conn, "chat_thread_messages")
        if not columns:
            return {
                "selected_sender_key": payload.sender_key,
                "status": status_filter,
                "sort": payload.sort,
                "total": 0,
                "offset": payload.offset,
                "limit": payload.limit,
                "has_more": False,
                "items": [],
            }

        base_filters = ["owner_id = ?"]
        params: List[Any] = [user_id]
        if search_value:
            like_value = f"%{search_value}%"
            base_filters.append(
                "(lower(COALESCE(text, '')) LIKE ? OR lower(COALESCE(original_text, '')) LIKE ? OR lower(COALESCE(chat_title, '')) LIKE ?)"
            )
            params.extend([like_value, like_value, like_value])

        cte = f"""
        WITH thread_rows AS (
            SELECT
                id,
                sender_id,
                COALESCE(sender_username, '') AS sender_username,
                chat_id,
                COALESCE(chat_title, '') AS chat_title,
                msg_id,
                COALESCE(text, '') AS text_value,
                COALESCE(original_text, '') AS original_text_value,
                lower(trim(COALESCE(status, 'active'))) AS status_norm,
                COALESCE(created_at, updated_at, deleted_at, '') AS event_ts,
                COALESCE(created_at, '') AS created_at,
                COALESCE(updated_at, '') AS updated_at,
                COALESCE(deleted_at, '') AS deleted_at,
                COALESCE(edit_count, 0) AS edit_count,
                COALESCE(content_type, '') AS content_type,
                COALESCE(media_path, '') AS media_path,
                CASE
                    WHEN sender_id IS NOT NULL THEN 'id:' || sender_id
                    WHEN TRIM(COALESCE(sender_username, '')) <> '' AND lower(TRIM(sender_username)) NOT IN ('удалённый', 'удаленный', 'deleted')
                    THEN 'username:' || lower(TRIM(sender_username))
                    ELSE 'unknown'
                END AS sender_key
            FROM chat_thread_messages
            WHERE {' AND '.join(base_filters)}
        )
        """

        outer_filters = [sender_clause]
        outer_params = list(sender_params)
        if status_filter == "changed":
            outer_filters.append("status_norm IN ('deleted', 'edited')")
        elif status_filter != "all":
            outer_filters.append("status_norm = ?")
            outer_params.append(status_filter)
        outer_where = " AND ".join(outer_filters)

        count_query = cte + f"SELECT COUNT(*) AS total_count FROM thread_rows WHERE {outer_where}"
        async with conn.execute(count_query, tuple(params + outer_params)) as cur:
            total_row = await cur.fetchone()
        total = int(total_row["total_count"] or 0) if total_row else 0

        order_sql = _thread_order_sql(payload.sort)
        data_query = (
            cte
            + f"""
            SELECT
                id,
                sender_id,
                sender_username,
                sender_key,
                chat_id,
                chat_title,
                msg_id,
                text_value,
                original_text_value,
                status_norm,
                created_at,
                updated_at,
                deleted_at,
                edit_count,
                content_type,
                media_path
            FROM thread_rows
            WHERE {outer_where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """
        )
        query_params = params + outer_params + [payload.limit, payload.offset]
        async with conn.execute(data_query, tuple(query_params)) as cur:
            rows = await cur.fetchall()

    items: List[Dict[str, Any]] = []
    for row in rows:
        media_path = await _normalize_media_reference(
            table_name="chat_thread_messages",
            item_id=int(row["id"]),
            owner_id=user_id,
            media_path=str(row["media_path"] or "").strip(),
            endpoint="/ai/thread/messages",
        )
        status_norm = _thread_status_value(row["status_norm"])
        category_name = _archive_category_from_values(row["content_type"], media_path)
        has_media = bool(media_path)
        items.append(
            {
                "item_id": int(row["id"]),
                "sender_key": str(row["sender_key"] or ""),
                "sender_label": _archive_sender_label(row["sender_id"], row["sender_username"]),
                "sender_username": _clean_profile_text(row["sender_username"], 80),
                "chat_id": int(row["chat_id"]) if row["chat_id"] is not None else None,
                "chat_title": _clean_profile_text(row["chat_title"], 120) or "Личный чат",
                "msg_id": int(row["msg_id"]) if row["msg_id"] is not None else None,
                "status": status_norm,
                "text": _clean_profile_text(row["text_value"], 4000),
                "original_text": _clean_profile_text(row["original_text_value"], 4000),
                "created_at": _format_event_time(row["created_at"]),
                "updated_at": _format_event_time(row["updated_at"]),
                "deleted_at": _format_event_time(row["deleted_at"]),
                "edit_count": int(row["edit_count"] or 0),
                "content_type": _clean_profile_text(row["content_type"], 80) or "Сообщение",
                "category": category_name,
                "has_media": has_media,
                "has_preview": has_media and category_name in {"photo", "video", "voice"},
                "can_send_to_chat": _can_send_to_chat(category_name, _clean_profile_text(row["text_value"], 4000), has_media),
                "file_name": os.path.basename(media_path) if media_path else "",
            }
        )

    next_offset = payload.offset + len(items)
    return {
        "selected_sender_key": payload.sender_key,
        "status": status_filter,
        "sort": payload.sort,
        "total": total,
        "offset": payload.offset,
        "limit": payload.limit,
        "has_more": next_offset < total,
        "items": items,
    }


async def _thread_all_items(identity: AIIdentityContext, payload: AIThreadAllRequest) -> Dict[str, Any]:
    user_id = identity.user_id
    status_filter = _thread_status_value(payload.status)
    search_value = (payload.search or "").strip().lower()
    category_filter = (payload.category or "all").strip().lower()

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        columns = await _table_columns(conn, "chat_thread_messages")
        if not columns:
            return {
                "selected_sender_key": "all",
                "status": status_filter,
                "sort": payload.sort,
                "total": 0,
                "offset": payload.offset,
                "limit": payload.limit,
                "has_more": False,
                "items": [],
            }

        base_filters = ["owner_id = ?"]
        params: List[Any] = [user_id]

        if status_filter == "changed":
            base_filters.append("lower(trim(COALESCE(status, 'active'))) IN ('deleted', 'edited')")
        elif status_filter != "all":
            base_filters.append("lower(trim(COALESCE(status, 'active'))) = ?")
            params.append(status_filter)

        if category_filter != "all":
            base_filters.append(f"({_archive_category_sql('content_type', 'media_path')}) = ?")
            params.append(category_filter)

        if search_value:
            like_value = f"%{search_value}%"
            base_filters.append("(lower(COALESCE(text, '')) LIKE ? OR lower(COALESCE(original_text, '')) LIKE ? OR lower(COALESCE(chat_title, '')) LIKE ?)")
            params.extend([like_value, like_value, like_value])

        cte = f"""
        WITH thread_rows AS (
            SELECT
                id,
                sender_id,
                COALESCE(sender_username, '') AS sender_username,
                chat_id,
                COALESCE(chat_title, '') AS chat_title,
                msg_id,
                COALESCE(text, '') AS text_value,
                COALESCE(original_text, '') AS original_text_value,
                lower(trim(COALESCE(status, 'active'))) AS status_norm,
                COALESCE(created_at, updated_at, deleted_at, '') AS event_ts,
                COALESCE(created_at, '') AS created_at,
                COALESCE(updated_at, '') AS updated_at,
                COALESCE(deleted_at, '') AS deleted_at,
                COALESCE(edit_count, 0) AS edit_count,
                COALESCE(content_type, '') AS content_type,
                COALESCE(media_path, '') AS media_path
            FROM chat_thread_messages
            WHERE {' AND '.join(base_filters)}
        )
        """

        count_query = cte + "SELECT COUNT(*) AS total_count FROM thread_rows"
        async with conn.execute(count_query, tuple(params)) as cur:
            total_row = await cur.fetchone()
        total = int(total_row["total_count"] or 0) if total_row else 0

        order_sql = _thread_order_sql(payload.sort)
        data_query = (
            cte
            + f"""
            SELECT
                id,
                sender_id,
                sender_username,
                chat_id,
                chat_title,
                msg_id,
                text_value,
                original_text_value,
                status_norm,
                created_at,
                updated_at,
                deleted_at,
                edit_count,
                content_type,
                media_path
            FROM thread_rows
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """
        )
        query_params = params + [payload.limit, payload.offset]
        async with conn.execute(data_query, tuple(query_params)) as cur:
            rows = await cur.fetchall()

    items: List[Dict[str, Any]] = []
    for row in rows:
        media_path = await _normalize_media_reference(
            table_name="chat_thread_messages",
            item_id=int(row["id"]),
            owner_id=user_id,
            media_path=str(row["media_path"] or "").strip(),
            endpoint="/ai/thread/all",
        )
        status_norm = _thread_status_value(row["status_norm"])
        category_name = _archive_category_from_values(row["content_type"], media_path)
        has_media = bool(media_path)
        sender_key_val = "unknown"
        if row["sender_id"] is not None:
            sender_key_val = f"id:{row['sender_id']}"
        elif str(row["sender_username"] or "").strip():
            sender_key_val = f"username:{str(row['sender_username']).strip().lower()}"
        items.append(
            {
                "item_id": int(row["id"]),
                "sender_key": sender_key_val,
                "sender_label": _archive_sender_label(row["sender_id"], row["sender_username"]),
                "sender_username": _clean_profile_text(row["sender_username"], 80),
                "chat_id": int(row["chat_id"]) if row["chat_id"] is not None else None,
                "chat_title": _clean_profile_text(row["chat_title"], 120) or "Личный чат",
                "msg_id": int(row["msg_id"]) if row["msg_id"] is not None else None,
                "status": status_norm,
                "text": _clean_profile_text(row["text_value"], 4000),
                "original_text": _clean_profile_text(row["original_text_value"], 4000),
                "created_at": _format_event_time(row["created_at"]),
                "updated_at": _format_event_time(row["updated_at"]),
                "deleted_at": _format_event_time(row["deleted_at"]),
                "edit_count": int(row["edit_count"] or 0),
                "content_type": _clean_profile_text(row["content_type"], 80) or "Сообщение",
                "category": category_name,
                "has_media": has_media,
                "has_preview": has_media and category_name in {"photo", "video", "voice"},
                "can_send_to_chat": _can_send_to_chat(category_name, _clean_profile_text(row["text_value"], 4000), has_media),
                "file_name": os.path.basename(media_path) if media_path else "",
            }
        )

    next_offset = payload.offset + len(items)
    return {
        "selected_sender_key": "all",
        "status": status_filter,
        "sort": payload.sort,
        "total": total,
        "offset": payload.offset,
        "limit": payload.limit,
        "has_more": next_offset < total,
        "items": items,
    }


async def _thread_history(identity: AIIdentityContext, item_id: int, chat_id: Optional[int] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT id, owner_id, chat_id, msg_id, status, text, original_text, created_at, updated_at, deleted_at, edit_count
            FROM chat_thread_messages
            WHERE owner_id = ? AND id = ?
            LIMIT 1
            """,
            (identity.user_id, item_id),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Сообщение не найдено.")
        if chat_id is not None and int(row["chat_id"] or 0) != int(chat_id):
            raise HTTPException(status_code=404, detail="Сообщение не найдено в выбранном чате.")

        async with conn.execute(
            """
            SELECT event_type, COALESCE(text, '') AS text, COALESCE(previous_text, '') AS previous_text, COALESCE(created_at, '') AS created_at
            FROM chat_thread_revisions
            WHERE owner_id = ? AND chat_id = ? AND msg_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (identity.user_id, row["chat_id"], row["msg_id"]),
        ) as cur:
            revisions = await cur.fetchall()

    events: List[Dict[str, Any]] = []
    if revisions:
        for rev in revisions:
            events.append(
                {
                    "event_type": str(rev["event_type"] or "updated"),
                    "text": _clean_profile_text(rev["text"], 4000),
                    "previous_text": _clean_profile_text(rev["previous_text"], 4000),
                    "created_at": _format_event_time(rev["created_at"]),
                }
            )
    else:
        base_text = _clean_profile_text(row["text"], 4000)
        base_original = _clean_profile_text(row["original_text"], 4000)
        events.append(
            {
                "event_type": "created",
                "text": base_original or base_text,
                "previous_text": "",
                "created_at": _format_event_time(row["created_at"]),
            }
        )
        if int(row["edit_count"] or 0) > 0 and base_text and base_text != (base_original or base_text):
            events.append(
                {
                    "event_type": "edited",
                    "text": base_text,
                    "previous_text": base_original or "",
                    "created_at": _format_event_time(row["updated_at"]),
                }
            )
        if _thread_status_value(row["status"]) == "deleted":
            events.append(
                {
                    "event_type": "deleted",
                    "text": "",
                    "previous_text": "",
                    "created_at": _format_event_time(row["deleted_at"]),
                }
            )

    return {"item_id": int(item_id), "events": events}


async def _chat_dialog_items(identity: AIIdentityContext, payload: AIChatDialogsRequest) -> Dict[str, Any]:
    user_id = identity.user_id
    search_value = (payload.search or '').strip().lower()
    session_active, watcher_active = _runtime_flags(user_id)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        if not await _table_exists(conn, 'chat_dialogs'):
            return {
                'profile': dict(identity.profile),
                'session_active': session_active,
                'watcher_active': watcher_active,
                'dialogs': [],
            }

        where_clauses = ['owner_id = ?']
        params: List[Any] = [user_id]
        if search_value:
            like_value = f'%{search_value}%'
            where_clauses.append(
                "(lower(COALESCE(title, '')) LIKE ? OR lower(COALESCE(username, '')) LIKE ? OR lower(COALESCE(last_message_preview, '')) LIKE ?)"
            )
            params.extend([like_value, like_value, like_value])

        query = f"""
        SELECT
            chat_id,
            COALESCE(title, 'Диалог') AS title,
            COALESCE(username, '') AS username,
            COALESCE(photo_url, '') AS photo_url,
            COALESCE(dialog_type, 'private') AS dialog_type,
            last_message_id,
            COALESCE(last_message_at, '') AS last_message_at,
            COALESCE(last_message_preview, '') AS last_message_preview,
            COALESCE(last_sender_label, '') AS last_sender_label,
            COALESCE(unread_count, 0) AS unread_count,
            COALESCE(history_complete, 0) AS history_complete,
            COALESCE(last_sync_at, '') AS last_sync_at,
            (
                SELECT COUNT(*)
                FROM chat_thread_messages AS m
                WHERE m.owner_id = chat_dialogs.owner_id
                  AND m.chat_id = chat_dialogs.chat_id
            ) AS message_count,
            (
                SELECT COUNT(*)
                FROM chat_thread_messages AS m
                WHERE m.owner_id = chat_dialogs.owner_id
                  AND m.chat_id = chat_dialogs.chat_id
                  AND COALESCE(m.status, 'active') = 'deleted'
            ) AS deleted_count,
            (
                SELECT COUNT(*)
                FROM chat_thread_messages AS m
                WHERE m.owner_id = chat_dialogs.owner_id
                  AND m.chat_id = chat_dialogs.chat_id
                  AND (COALESCE(m.status, 'active') = 'edited' OR COALESCE(m.edit_count, 0) > 0)
            ) AS edited_count
        FROM chat_dialogs
        WHERE {' AND '.join(where_clauses)}
        ORDER BY COALESCE(last_message_at, updated_at, created_at, '') DESC, title ASC
        LIMIT ?
        """
        params.append(payload.limit)
        async with conn.execute(query, tuple(params)) as cur:
            rows = await cur.fetchall()

    dialogs: List[Dict[str, Any]] = []
    for row in rows:
        history_complete = bool(row['history_complete'])
        sync_status = 'complete' if history_complete else ('syncing' if watcher_active else 'paused')
        photo_path = str(row['photo_url'] or '').strip()
        dialogs.append(
            {
                'chat_id': int(row['chat_id']),
                'title': _clean_profile_text(row['title'], 120) or 'Диалог',
                'username': _clean_profile_text(row['username'], 80),
                'dialog_type': _clean_profile_text(row['dialog_type'], 24) or 'private',
                'last_message_id': int(row['last_message_id']) if row['last_message_id'] is not None else None,
                'last_message_at': _format_event_time(row['last_message_at']),
                'last_message_preview': _clean_profile_text(row['last_message_preview'], 220),
                'last_sender_label': _clean_profile_text(row['last_sender_label'], 80),
                'unread_count': int(row['unread_count'] or 0),
                'message_count': int(row['message_count'] or 0),
                'deleted_count': int(row['deleted_count'] or 0),
                'edited_count': int(row['edited_count'] or 0),
                'has_recent_changes': bool(int(row['deleted_count'] or 0) > 0 or int(row['edited_count'] or 0) > 0),
                'has_photo': bool(photo_path and os.path.exists(photo_path)),
                'history_complete': history_complete,
                'sync_status': sync_status,
            }
        )

    return {
        'profile': dict(identity.profile),
        'session_active': session_active,
        'watcher_active': watcher_active,
        'dialogs': dialogs,
    }


async def _chat_message_items(identity: AIIdentityContext, payload: AIChatMessagesRequest) -> Dict[str, Any]:
    user_id = identity.user_id
    session_active, watcher_active = _runtime_flags(user_id)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row

        async with conn.execute(
            """
            SELECT
                chat_id,
                COALESCE(title, 'Диалог') AS title,
                COALESCE(username, '') AS username,
                COALESCE(photo_url, '') AS photo_url,
                COALESCE(dialog_type, 'private') AS dialog_type,
                COALESCE(history_complete, 0) AS history_complete
            FROM chat_dialogs
            WHERE owner_id = ? AND chat_id = ?
            LIMIT 1
            """,
            (user_id, payload.chat_id),
        ) as cur:
            dialog_row = await cur.fetchone()

        if not dialog_row:
            raise HTTPException(status_code=404, detail='Диалог не найден.')

        where_clauses = ['m.owner_id = ?', 'm.chat_id = ?']
        params: List[Any] = [user_id, payload.chat_id]
        if payload.before_msg_id is not None:
            where_clauses.append('COALESCE(m.msg_id, 0) < ?')
            params.append(int(payload.before_msg_id))

        query = f"""
        SELECT
            m.id,
            m.chat_id,
            m.msg_id,
            m.sender_id,
            COALESCE(m.sender_username, '') AS sender_username,
            COALESCE(m.sender_display_name, '') AS sender_display_name,
            COALESCE(m.is_outgoing, 0) AS is_outgoing,
            COALESCE(m.status, 'active') AS status_norm,
            COALESCE(m.text, '') AS text_value,
            COALESCE(m.original_text, '') AS original_text_value,
            COALESCE(m.created_at, '') AS created_at,
            COALESCE(m.updated_at, '') AS updated_at,
            COALESCE(m.deleted_at, '') AS deleted_at,
            COALESCE(m.edit_count, 0) AS edit_count,
            COALESCE(m.content_type, '') AS content_type,
            COALESCE(m.media_path, '') AS media_path,
            m.reply_to_msg_id,
            COALESCE(m.dialog_type, 'private') AS dialog_type,
            COALESCE(r.sender_username, '') AS reply_sender_username,
            COALESCE(r.sender_display_name, '') AS reply_sender_display_name,
            COALESCE(r.is_outgoing, 0) AS reply_is_outgoing,
            COALESCE(r.text, '') AS reply_text_value,
            COALESCE(r.content_type, '') AS reply_content_type,
            COALESCE(r.status, '') AS reply_status_norm
        FROM chat_thread_messages AS m
        LEFT JOIN chat_thread_messages AS r
          ON r.owner_id = m.owner_id
         AND r.chat_id = m.chat_id
         AND r.msg_id = m.reply_to_msg_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY COALESCE(m.msg_id, 0) DESC, COALESCE(m.created_at, '') DESC
        LIMIT ?
        """
        query_params = params + [payload.limit]
        async with conn.execute(query, tuple(query_params)) as cur:
            rows = await cur.fetchall()

        if rows:
            oldest_loaded_msg_id = min(int(row['msg_id']) for row in rows if row['msg_id'] is not None)
            newest_loaded_msg_id = max(int(row['msg_id']) for row in rows if row['msg_id'] is not None)
        else:
            oldest_loaded_msg_id = None
            newest_loaded_msg_id = None

        has_more = False
        if oldest_loaded_msg_id is not None:
            async with conn.execute(
                """
                SELECT 1
                FROM chat_thread_messages
                WHERE owner_id = ? AND chat_id = ? AND COALESCE(msg_id, 0) < ?
                LIMIT 1
                """,
                (user_id, payload.chat_id, oldest_loaded_msg_id),
            ) as cur:
                has_more = bool(await cur.fetchone())

    messages: List[Dict[str, Any]] = []
    for row in reversed(rows):
        media_path = await _normalize_media_reference(
            table_name='chat_thread_messages',
            item_id=int(row['id']),
            owner_id=user_id,
            media_path=str(row['media_path'] or '').strip(),
            endpoint='/ai/chat/messages',
        )
        category_name = _archive_category_from_values(row['content_type'], media_path)
        has_media = bool(media_path)
        sender_username = _clean_profile_text(row['sender_username'], 80)
        sender_display_name = _clean_profile_text(row['sender_display_name'], 120)
        sender_label = sender_display_name or (f'@{sender_username}' if sender_username else 'Неизвестно')
        if bool(row['is_outgoing']):
            sender_label = 'Вы'

        reply_to_msg_id = int(row['reply_to_msg_id']) if row['reply_to_msg_id'] is not None else None
        reply_sender_username = _clean_profile_text(row['reply_sender_username'], 80)
        reply_sender_display_name = _clean_profile_text(row['reply_sender_display_name'], 120)
        reply_sender_label = reply_sender_display_name or (f'@{reply_sender_username}' if reply_sender_username else '')
        if bool(row['reply_is_outgoing']):
            reply_sender_label = 'Вы'
        reply_status = _thread_status_value(row['reply_status_norm']) if reply_to_msg_id else ''
        reply_preview_missing = bool(reply_to_msg_id) and not any(
            [
                reply_sender_label,
                str(row['reply_text_value'] or '').strip(),
                str(row['reply_content_type'] or '').strip(),
                str(row['reply_status_norm'] or '').strip(),
            ]
        )
        reply_preview_text = _clean_profile_text(row['reply_text_value'], 220)
        if not reply_preview_text:
            reply_preview_text = _clean_profile_text(row['reply_content_type'], 80)
        if not reply_preview_text and reply_to_msg_id:
            reply_preview_text = 'Сообщение недоступно' if reply_preview_missing else 'Сообщение'

        messages.append(
            {
                'item_id': int(row['id']),
                'chat_id': int(row['chat_id']),
                'msg_id': int(row['msg_id']) if row['msg_id'] is not None else None,
                'sender_id': int(row['sender_id']) if row['sender_id'] is not None else None,
                'sender_label': sender_label,
                'sender_username': sender_username,
                'sender_display_name': sender_display_name,
                'is_outgoing': bool(row['is_outgoing']),
                'status': _thread_status_value(row['status_norm']),
                'text': _clean_profile_text(row['text_value'], 4000),
                'original_text': _clean_profile_text(row['original_text_value'], 4000),
                'created_at': _format_event_time(row['created_at']),
                'updated_at': _format_event_time(row['updated_at']),
                'deleted_at': _format_event_time(row['deleted_at']),
                'edit_count': int(row['edit_count'] or 0),
                'content_type': _clean_profile_text(row['content_type'], 80) or 'Сообщение',
                'has_media': has_media,
                'has_preview': has_media and category_name in {'photo', 'video', 'voice'},
                'file_name': os.path.basename(media_path) if media_path else '',
                'reply_to_msg_id': reply_to_msg_id,
                'reply_preview_sender_label': reply_sender_label,
                'reply_preview_text': reply_preview_text,
                'reply_preview_status': reply_status,
                'reply_preview_missing': reply_preview_missing,
                'dialog_type': _clean_profile_text(row['dialog_type'], 24) or 'private',
            }
        )

    dialog_photo_path = str(dialog_row['photo_url'] or '').strip()
    return {
        'profile': dict(identity.profile),
        'session_active': session_active,
        'watcher_active': watcher_active,
        'chat_id': int(dialog_row['chat_id']),
        'title': _clean_profile_text(dialog_row['title'], 120) or 'Диалог',
        'username': _clean_profile_text(dialog_row['username'], 80),
        'dialog_type': _clean_profile_text(dialog_row['dialog_type'], 24) or 'private',
        'has_photo': bool(dialog_photo_path and os.path.exists(dialog_photo_path)),
        'history_complete': bool(dialog_row['history_complete']),
        'oldest_loaded_msg_id': oldest_loaded_msg_id,
        'newest_loaded_msg_id': newest_loaded_msg_id,
        'has_more': has_more,
        'messages': messages,
    }


async def _build_archive_status(identity: AIIdentityContext) -> Dict[str, Any]:
    user_id = identity.user_id
    session_active, watcher_active = _runtime_flags(user_id)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row

        async def _count(query: str, params: Tuple[Any, ...]) -> int:
            try:
                async with conn.execute(query, params) as cur:
                    row = await cur.fetchone()
                return int(row[0] or 0) if row else 0
            except Exception:
                return 0

        total_dialogs = await _count(
            "SELECT COUNT(*) FROM chat_dialogs WHERE owner_id = ?",
            (user_id,),
        ) if await _table_exists(conn, "chat_dialogs") else 0

        history_complete_dialogs = await _count(
            "SELECT COUNT(*) FROM chat_dialogs WHERE owner_id = ? AND COALESCE(history_complete, 0) = 1",
            (user_id,),
        ) if await _table_exists(conn, "chat_dialogs") else 0

        synced_dialogs = await _count(
            "SELECT COUNT(*) FROM chat_sync_state WHERE owner_id = ? AND COALESCE(newest_synced_msg_id, 0) > 0",
            (user_id,),
        ) if await _table_exists(conn, "chat_sync_state") else history_complete_dialogs

        archived_messages = await _count(
            "SELECT COUNT(*) FROM chat_thread_messages WHERE owner_id = ?",
            (user_id,),
        ) if await _table_exists(conn, "chat_thread_messages") else 0

        deleted_messages = await _count(
            "SELECT COUNT(*) FROM deleted_messages WHERE owner_id = ?",
            (user_id,),
        ) if await _table_exists(conn, "deleted_messages") else 0

        pending_messages = await _count(
            "SELECT COUNT(*) FROM pending WHERE owner_id = ?",
            (user_id,),
        ) if await _table_exists(conn, "pending") else 0

        risk_events_24h = 0
        if await _table_exists(conn, "risk_events"):
            since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            risk_events_24h = await _count(
                "SELECT COUNT(*) FROM risk_events WHERE owner_id = ? AND COALESCE(event_at, created_at, '') >= ?",
                (user_id, since_iso),
            )

        high_risk_chats = await _count(
            "SELECT COUNT(*) FROM risk_profiles WHERE owner_id = ? AND profile_kind = 'chat' AND risk_score >= 5",
            (user_id,),
        ) if await _table_exists(conn, "risk_profiles") else 0

        high_risk_senders = await _count(
            "SELECT COUNT(*) FROM risk_profiles WHERE owner_id = ? AND profile_kind = 'sender' AND risk_score >= 5",
            (user_id,),
        ) if await _table_exists(conn, "risk_profiles") else 0

        last_sync_at = ""
        if await _table_exists(conn, "chat_sync_state"):
            async with conn.execute(
                "SELECT COALESCE(MAX(updated_at), '') FROM chat_sync_state WHERE owner_id = ?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
            last_sync_at = _format_event_time(row[0]) if row and row[0] else ""

    return {
        "profile": dict(identity.profile),
        "session_active": session_active,
        "watcher_active": watcher_active,
        "total_dialogs": total_dialogs,
        "synced_dialogs": synced_dialogs,
        "pending_dialogs": max(0, total_dialogs - synced_dialogs),
        "history_complete_dialogs": history_complete_dialogs,
        "archived_messages": archived_messages,
        "deleted_messages": deleted_messages,
        "pending_messages": pending_messages,
        "risk_events_24h": risk_events_24h,
        "high_risk_chats": high_risk_chats,
        "high_risk_senders": high_risk_senders,
        "last_sync_at": last_sync_at,
    }


async def _build_risk_summary(identity: AIIdentityContext) -> Dict[str, Any]:
    user_id = identity.user_id
    session_active, watcher_active = _runtime_flags(user_id)
    top_profiles: List[Dict[str, Any]] = []
    recent_events: List[Dict[str, Any]] = []

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row

        if await _table_exists(conn, "risk_profiles"):
            async with conn.execute(
                """
                SELECT
                    profile_kind,
                    profile_id,
                    COALESCE(risk_score, 0) AS risk_score,
                    COALESCE(delete_count, 0) AS delete_count,
                    COALESCE(edit_count, 0) AS edit_count,
                    COALESCE(disappearing_count, 0) AS disappearing_count,
                    COALESCE(night_count, 0) AS night_count,
                    COALESCE(burst_count, 0) AS burst_count,
                    COALESCE(last_event_at, '') AS last_event_at,
                    COALESCE(summary, '') AS summary
                FROM risk_profiles
                WHERE owner_id = ?
                ORDER BY COALESCE(risk_score, 0) DESC, COALESCE(updated_at, created_at, '') DESC
                LIMIT 10
                """,
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()

            for row in rows or []:
                profile_kind = str(row["profile_kind"] or "")
                profile_id = int(row["profile_id"] or 0)
                label = f"Чат {profile_id}" if profile_kind == "chat" else f"Пользователь {profile_id}"
                if profile_kind == "chat" and await _table_exists(conn, "chat_dialogs"):
                    async with conn.execute(
                        "SELECT COALESCE(title, '') FROM chat_dialogs WHERE owner_id = ? AND chat_id = ? LIMIT 1",
                        (user_id, profile_id),
                    ) as cur:
                        dialog_row = await cur.fetchone()
                    if dialog_row and dialog_row[0]:
                        label = str(dialog_row[0])

                top_profiles.append(
                    {
                        "label": _clean_profile_text(label, 120),
                        "profile_kind": profile_kind,
                        "profile_id": profile_id,
                        "risk_score": float(row["risk_score"] or 0),
                        "delete_count": int(row["delete_count"] or 0),
                        "edit_count": int(row["edit_count"] or 0),
                        "disappearing_count": int(row["disappearing_count"] or 0),
                        "night_count": int(row["night_count"] or 0),
                        "burst_count": int(row["burst_count"] or 0),
                        "last_event_at": _format_event_time(row["last_event_at"]),
                        "summary": _clean_profile_text(row["summary"], 220),
                    }
                )

        if await _table_exists(conn, "risk_events"):
            async with conn.execute(
                """
                SELECT
                    COALESCE(signal_type, '') AS signal_type,
                    COALESCE(severity, 'info') AS severity,
                    COALESCE(score, 0) AS score,
                    COALESCE(title, '') AS title,
                    COALESCE(detail, '') AS detail,
                    chat_id,
                    sender_id,
                    msg_id,
                    COALESCE(event_at, created_at, '') AS event_at
                FROM risk_events
                WHERE owner_id = ?
                ORDER BY COALESCE(event_at, created_at, '') DESC
                LIMIT 20
                """,
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()

            for row in rows or []:
                recent_events.append(
                    {
                        "signal_type": _clean_profile_text(row["signal_type"], 60),
                        "severity": _clean_profile_text(row["severity"], 20) or "info",
                        "score": float(row["score"] or 0),
                        "title": _clean_profile_text(row["title"], 120),
                        "detail": _clean_profile_text(row["detail"], 220),
                        "chat_id": int(row["chat_id"]) if row["chat_id"] is not None else None,
                        "sender_id": int(row["sender_id"]) if row["sender_id"] is not None else None,
                        "msg_id": int(row["msg_id"]) if row["msg_id"] is not None else None,
                        "event_at": _format_event_time(row["event_at"]),
                    }
                )

    return {
        "profile": dict(identity.profile),
        "session_active": session_active,
        "watcher_active": watcher_active,
        "top_profiles": top_profiles,
        "recent_events": recent_events,
    }


async def _get_archive_row(user_id: int, item_id: int) -> aiosqlite.Row:
    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT id, owner_id, sender_id, sender_username, content_type, media_path, text, original_text
            FROM deleted_messages
            WHERE owner_id = ? AND id = ?
            LIMIT 1
            """,
            (user_id, item_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Запись архива не найдена.")
    return row


async def _get_thread_row(user_id: int, item_id: int, chat_id: Optional[int] = None) -> aiosqlite.Row:
    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT id, owner_id, chat_id, content_type, media_path, text, original_text
            FROM chat_thread_messages
            WHERE owner_id = ? AND id = ?
            LIMIT 1
            """,
            (user_id, item_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Запись переписки не найдена.")
    if chat_id is not None and int(row["chat_id"] or 0) != int(chat_id):
        raise HTTPException(status_code=404, detail="Сообщение не найдено в выбранном чате.")
    return row


async def _read_media_file(path: str) -> Tuple[bytes, str]:
    if not path or not os.path.exists(path):
        await ANOMALY_SERVICE.record_anomaly(
            endpoint="/ai/media/read",
            user_id=int(REQUEST_CONTEXT_USER_ID.get(0) or 0),
            category="missing_media_file",
            details={"media_path": str(path or "")},
            note="Media file is missing on disk while requested by API",
        )
        raise HTTPException(status_code=404, detail="Archive file is no longer available.")

    loop = asyncio.get_running_loop()

    def _read() -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    content = await loop.run_in_executor(None, _read)
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return content, media_type


async def _telegram_bot_api_send_media(
    chat_id: int,
    method: str,
    field_name: str,
    file_name: str,
    content: bytes,
    media_type: str,
) -> Dict[str, Any]:
    if not CONFIG.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured.")

    url = f"https://api.telegram.org/bot{CONFIG.bot_token}/{method}"
    data: Dict[str, Any] = {"chat_id": str(chat_id)}
    if method == "sendVideo":
        data["supports_streaming"] = "true"

    async def _request() -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url,
                data=data,
                files={field_name: (file_name, content, media_type)},
            )
        if response.status_code in {429, 500, 502, 503, 504}:
            raise _RetryableStatusError(response.status_code, response.text)
        return response

    try:
        response = await RETRY_SERVICE.execute(
            _request,
            operation_name=f"telegram.{method}",
            attempts=3,
            retry_for=(httpx.RequestError, _RetryableStatusError),
        )
    except (httpx.RequestError, _RetryableStatusError) as exc:
        raise HTTPException(status_code=502, detail=f"Unable to send file to Telegram chat: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"ok": False, "description": response.text[:300]}

    if response.status_code >= 400 or not payload.get("ok"):
        detail = payload.get("description") or f"Telegram Bot API error ({response.status_code})"
        raise HTTPException(status_code=502, detail=f"Telegram failed to send the file: {detail}")

    return payload.get("result") or {}


async def _telegram_bot_api_send_text(chat_id: int, text: str) -> Dict[str, Any]:
    if not CONFIG.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured.")

    clean_text = str(text or "").strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="No text available for this item.")
    if len(clean_text) > 4000:
        clean_text = clean_text[:3997].rstrip() + "..."

    url = f"https://api.telegram.org/bot{CONFIG.bot_token}/sendMessage"
    data: Dict[str, Any] = {"chat_id": str(chat_id), "text": clean_text}

    async def _request() -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, data=data)
        if response.status_code in {429, 500, 502, 503, 504}:
            raise _RetryableStatusError(response.status_code, response.text)
        return response

    try:
        response = await RETRY_SERVICE.execute(
            _request,
            operation_name="telegram.sendMessage",
            attempts=3,
            retry_for=(httpx.RequestError, _RetryableStatusError),
        )
    except (httpx.RequestError, _RetryableStatusError) as exc:
        raise HTTPException(status_code=502, detail=f"Unable to send text to Telegram chat: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"ok": False, "description": response.text[:300]}

    if response.status_code >= 400 or not payload.get("ok"):
        detail = payload.get("description") or f"Telegram Bot API error ({response.status_code})"
        raise HTTPException(status_code=502, detail=f"Telegram failed to send text: {detail}")

    return payload.get("result") or {}


async def _send_archive_item_to_chat(identity: AIIdentityContext, item_id: int) -> Dict[str, Any]:
    row = await _get_archive_row(identity.user_id, item_id)
    media_path = await _normalize_media_reference(
        table_name="deleted_messages",
        item_id=int(row["id"]),
        owner_id=identity.user_id,
        media_path=str(row["media_path"] or "").strip(),
        endpoint="/ai/archive/send-to-chat",
    )
    if not media_path:
        text_value = str(row["text"] or row["original_text"] or "").strip()
        await _telegram_bot_api_send_text(identity.user_id, text_value)
        return {"ok": True, "message": "Text sent to Telegram chat."}

    content, media_type = await _read_media_file(media_path)
    method, field_name = _archive_send_method(row["content_type"], media_path)
    await _telegram_bot_api_send_media(
        identity.user_id,
        method,
        field_name,
        os.path.basename(media_path) or "archive.bin",
        content,
        media_type,
    )
    return {"ok": True, "message": "File sent to Telegram chat."}


async def _send_thread_item_to_chat(identity: AIIdentityContext, item_id: int) -> Dict[str, Any]:
    row = await _get_thread_row(identity.user_id, item_id)
    media_path = await _normalize_media_reference(
        table_name="chat_thread_messages",
        item_id=int(row["id"]),
        owner_id=identity.user_id,
        media_path=str(row["media_path"] or "").strip(),
        endpoint="/ai/thread/send-to-chat",
    )
    if not media_path:
        text_value = str(row["text"] or row["original_text"] or "").strip()
        await _telegram_bot_api_send_text(identity.user_id, text_value)
        return {"ok": True, "message": "Text sent to Telegram chat."}

    content, media_type = await _read_media_file(media_path)
    method, field_name = _archive_send_method(row["content_type"], media_path)
    await _telegram_bot_api_send_media(
        identity.user_id,
        method,
        field_name,
        os.path.basename(media_path) or "thread.bin",
        content,
        media_type,
    )
    return {"ok": True, "message": "File sent to Telegram chat."}


async def _build_overview(identity: AIIdentityContext) -> Dict[str, Any]:
    user_id = identity.user_id
    profile = dict(identity.profile)

    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await _prepare_user_views(conn, user_id)

        async with conn.execute("SELECT COUNT(*) FROM deleted_messages") as cur:
            total_deleted = int((await cur.fetchone())[0] or 0)

        async with conn.execute("SELECT COUNT(*) FROM messages") as cur:
            total_messages = int((await cur.fetchone())[0] or 0)

        local_now = datetime.now(CONFIG.tz)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        week_start = (local_now - timedelta(days=7)).astimezone(timezone.utc).isoformat()
        async with conn.execute(
            "SELECT COUNT(*) FROM deleted_messages WHERE date >= ?",
            (day_start,),
        ) as cur:
            deleted_today = int((await cur.fetchone())[0] or 0)
        async with conn.execute(
            "SELECT COUNT(*) FROM deleted_messages WHERE date >= ?",
            (week_start,),
        ) as cur:
            deleted_last_7_days = int((await cur.fetchone())[0] or 0)

        async with conn.execute(
            """
            SELECT COALESCE(chat_title, CAST(chat_id AS TEXT), 'Неизвестный чат') AS chat_name,
                   COUNT(*) AS cnt
            FROM deleted_messages
            GROUP BY chat_name
            ORDER BY cnt DESC
            LIMIT 1
            """
        ) as cur:
            top_chat = await cur.fetchone()

        async with conn.execute(
            """
            SELECT COALESCE(chat_title, CAST(chat_id AS TEXT), 'Неизвестный чат') AS chat_name,
                   COUNT(*) AS cnt
            FROM deleted_messages
            GROUP BY chat_name
            ORDER BY cnt DESC
            LIMIT 3
            """
        ) as cur:
            top_chat_rows = await cur.fetchall()

        async with conn.execute("SELECT date FROM deleted_messages ORDER BY date DESC LIMIT 1") as cur:
            last_row = await cur.fetchone()

        async with conn.execute(
            """
            SELECT COALESCE(chat_title, CAST(chat_id AS TEXT), 'Неизвестный чат') AS chat_name,
                   COALESCE(text, '') AS text,
                   COALESCE(date, '') AS deleted_at,
                   COALESCE(sender_username, '') AS sender_username,
                   COALESCE(content_type, '') AS content_type
            FROM deleted_messages
            ORDER BY date DESC
            LIMIT 5
            """
        ) as cur:
            latest_deleted_rows = await cur.fetchall()

    top_chat_name = str(top_chat[0]) if top_chat else "нет данных"
    top_chat_count = int(top_chat[1]) if top_chat else 0
    last_event_raw = str(last_row[0]) if last_row and last_row[0] else ""
    last_event = _format_event_time(last_event_raw)
    top_chats = [
        {"name": str(row[0]), "count": int(row[1] or 0)}
        for row in (top_chat_rows or [])
    ]
    latest_deleted = [
        {
            "chat_name": str(row[0]),
            "text_preview": _short_text(_clean_profile_text(row[1], max_len=140) or "Без текста", 140),
            "deleted_at": _format_event_time(str(row[2] or "")),
            "sender_username": _clean_profile_text(row[3], max_len=80),
            "content_type": _clean_profile_text(row[4], max_len=40),
        }
        for row in (latest_deleted_rows or [])
    ]

    session_active = False
    watcher_active = False
    if BOT_RUNTIME_APP is not None:
        try:
            session_active = bool(BOT_RUNTIME_APP.storage.is_valid(user_id))
        except Exception:
            session_active = False
        try:
            watcher_active = user_id in BOT_RUNTIME_APP.watcher_service.watched_clients
        except Exception:
            watcher_active = False

    summary = (
        f"{profile['display_name']}, в вашем архиве {total_deleted} удаленных сообщений и {total_messages} сохраненных записей. "
        f"Сегодня удалено {deleted_today}, за последние 7 дней — {deleted_last_7_days}. "
        f"Самый активный чат: {top_chat_name} ({top_chat_count}). "
        f"Последнее удаление зафиксировано: {last_event}."
    )
    return {
        "profile": profile,
        "total_deleted": total_deleted,
        "deleted_today": deleted_today,
        "deleted_last_7_days": deleted_last_7_days,
        "total_messages": total_messages,
        "top_chat": {"name": top_chat_name, "count": top_chat_count},
        "top_chats": top_chats,
        "latest_deleted": latest_deleted,
        "last_event": last_event,
        "session_active": session_active,
        "watcher_active": watcher_active,
        "summary": summary,
    }


def _close_user_session(user_id: int) -> Dict[str, Any]:
    if BOT_RUNTIME_APP is None:
        return {"message": "Сервис сессий пока не инициализирован.", "session_closed": False}

    file_removed = False
    watcher_stopped = False
    state_reset = False

    try:
        BOT_RUNTIME_APP.storage.delete(user_id)
        file_removed = True
    except Exception:
        logger.exception("Failed to delete session zip for user %s", user_id)

    if BOT_RUNTIME_LOOP is not None:
        try:
            stop_future = asyncio.run_coroutine_threadsafe(
                BOT_RUNTIME_APP.watcher_service.stop(user_id),
                BOT_RUNTIME_LOOP,
            )
            stop_future.result(timeout=20)
            watcher_stopped = True
        except Exception:
            logger.exception("Failed to stop watcher for user %s", user_id)

        try:
            state_future = asyncio.run_coroutine_threadsafe(
                set_state(
                    BOT_RUNTIME_APP.db,
                    user_id,
                    "IDLE",
                    phone=None,
                    tmp_prefix=None,
                    awaiting_2fa=0,
                    auth_fail_count=0,
                    banned_until=None,
                ),
                BOT_RUNTIME_LOOP,
            )
            state_future.result(timeout=20)
            state_reset = True
        except Exception:
            logger.exception("Failed to reset auth state for user %s", user_id)

    if file_removed or watcher_stopped:
        msg = "Сессия завершена: watcher остановлен и данные входа удалены."
    elif state_reset:
        msg = "Сессия частично завершена: состояние входа обновлено."
    else:
        msg = "Не удалось полностью завершить сессию. Попробуйте повторить позже."

    return {
        "message": msg,
        "session_closed": bool(file_removed or watcher_stopped),
        "file_removed": file_removed,
        "watcher_stopped": watcher_stopped,
        "state_reset": state_reset,
    }


def _run_ai_server() -> None:
    logger.info("Запускаю FastAPI на %s:%s", AI_APP_HOST, AI_APP_PORT)
    uvicorn.run(
        ai_app,
        host=AI_APP_HOST,
        port=AI_APP_PORT,
        log_level="warning",
        access_log=False,
    )


def start_ai_daemon() -> None:
    global AI_SERVER_THREAD

    if AI_SERVER_THREAD is not None and AI_SERVER_THREAD.is_alive():
        logger.debug("AI daemon is already running")
        return

    AI_SERVER_THREAD = Thread(target=_run_ai_server, daemon=True, name="savedbot-ai-server")
    AI_SERVER_THREAD.start()


@ai_app.get("/", response_class=HTMLResponse)
async def ai_root() -> HTMLResponse:
    return HTMLResponse(
        "<h1>AI-assistant Telegram Mini App</h1><p>Перейдите на <a href='/miniapp'>Mini App</a>.</p>",
        headers={"Cache-Control": "no-store"},
    )


@ai_app.get("/miniapp", response_class=HTMLResponse)
async def serve_mini_app() -> HTMLResponse:
    return HTMLResponse(_render_archive_mini_app(), headers={"Cache-Control": "no-store"})


@ai_app.get("/miniapp.css")
async def serve_mini_app_css() -> Response:
    try:
        with open(ARCHIVE_MINIAPP_CSS_PATH, "r", encoding="utf-8") as handle:
            content = handle.read()
        return Response(content=content, media_type="text/css; charset=utf-8", headers={"Cache-Control": "no-store"})
    except Exception:
        logger.exception("Failed to load mini app CSS from %s", ARCHIVE_MINIAPP_CSS_PATH)
        raise HTTPException(status_code=500, detail="Mini app CSS is unavailable.")


@ai_app.get("/miniapp.js")
async def serve_mini_app_js() -> Response:
    try:
        with open(ARCHIVE_MINIAPP_JS_PATH, "r", encoding="utf-8") as handle:
            content = handle.read()
        return Response(content=content, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "no-store"})
    except Exception:
        logger.exception("Failed to load mini app JS from %s", ARCHIVE_MINIAPP_JS_PATH)
        raise HTTPException(status_code=500, detail="Mini app JS is unavailable.")


@ai_app.get("/ai/health")
@ai_app.get("/health")
async def ai_health() -> Dict[str, Any]:
    tracked_events = 0
    try:
        tracked_events = await DIAGNOSTICS_REPOSITORY.get_event_count(since_hours=24)
    except Exception:
        tracked_events = 0
    return {
        "status": "ok",
        "db_exists": os.path.exists(CONFIG.db_path),
        "openrouter_key_configured": bool(OPENROUTER_API_KEY),
        "local_user_fallback": AI_ALLOW_LOCAL_USER_ID,
        "observability_enabled": True,
        "tracked_events_last_24h": tracked_events,
        "cache_entries": len(AI_RESPONSE_CACHE),
    }


@ai_app.post("/ai", response_model=AIQueryResponse)
async def ai_endpoint(payload: AIQuestionPayload) -> AIQueryResponse:
    payload_data = payload.model_dump()
    identity = _resolve_identity(payload_data)
    user_id = identity.user_id
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required.")
    sql = await generate_sql(question)
    result = await run_sql(sql, user_id=user_id)
    answer = await explain_result(question, sql, result)
    if not answer.strip():
        answer = "Could not build a natural-language explanation, but SQL result is available."
    if int(result.get("row_count") or 0) == 0:
        await ANOMALY_SERVICE.record_anomaly(
            endpoint="/ai",
            user_id=user_id,
            category="empty_result",
            details={"sql": sql[:300]},
            note="Query returned no rows",
        )
    if bool(result.get("truncated")):
        await ANOMALY_SERVICE.record_anomaly(
            endpoint="/ai",
            user_id=user_id,
            category="result_truncated",
            details={"limit": int(result.get("limit") or 0)},
            note="Result is larger than response limit",
        )
    logger.info(
        "AI query user=%s rows=%s truncated=%s",
        user_id,
        result["row_count"],
        result["truncated"],
    )
    return AIQueryResponse(answer=answer, sql=sql, result=AIQueryResult(**result))


@ai_app.post("/ai/archive/users", response_model=AIArchiveUsersResponse)
async def ai_archive_users(payload: AIArchiveUsersRequest) -> AIArchiveUsersResponse:
    identity = _resolve_identity(payload.model_dump())
    cache_key = _cache_key("archive_users", identity.user_id)
    cached = AI_RESPONSE_CACHE.get(cache_key)
    if cached:
        return AIArchiveUsersResponse(**cached)
    directory = await _archive_user_directory(identity)
    AI_RESPONSE_CACHE.set(cache_key, directory, ttl=AI_DIRECTORY_CACHE_TTL_SEC)
    return AIArchiveUsersResponse(**directory)


@ai_app.post("/ai/archive/items", response_model=AIArchiveListResponse)
async def ai_archive_items(payload: AIArchiveListRequest) -> AIArchiveListResponse:
    identity = _resolve_identity(payload.model_dump())
    data = await _archive_items(identity, payload)
    return AIArchiveListResponse(**data)


@ai_app.post("/ai/thread/users", response_model=AIThreadUsersResponse)
async def ai_thread_users(payload: AIThreadUsersRequest) -> AIThreadUsersResponse:
    identity = _resolve_identity(payload.model_dump())
    cache_key = _cache_key("thread_users", identity.user_id)
    cached = AI_RESPONSE_CACHE.get(cache_key)
    if cached:
        return AIThreadUsersResponse(**cached)
    directory = await _thread_user_directory(identity)
    AI_RESPONSE_CACHE.set(cache_key, directory, ttl=AI_DIRECTORY_CACHE_TTL_SEC)
    return AIThreadUsersResponse(**directory)


@ai_app.post("/ai/thread/messages", response_model=AIThreadListResponse)
async def ai_thread_messages(payload: AIThreadListRequest) -> AIThreadListResponse:
    identity = _resolve_identity(payload.model_dump())
    data = await _thread_items(identity, payload)
    return AIThreadListResponse(**data)


@ai_app.post("/ai/chat/dialogs", response_model=AIChatDialogsResponse)
async def ai_chat_dialogs(payload: AIChatDialogsRequest) -> AIChatDialogsResponse:
    identity = _resolve_identity(payload.model_dump())
    cache_key = _cache_key("chat_dialogs", identity.user_id, (payload.search or "").strip().lower(), int(payload.limit))
    cached = AI_RESPONSE_CACHE.get(cache_key)
    if cached:
        return AIChatDialogsResponse(**cached)
    data = await _chat_dialog_items(identity, payload)
    AI_RESPONSE_CACHE.set(cache_key, data, ttl=AI_DIRECTORY_CACHE_TTL_SEC)
    return AIChatDialogsResponse(**data)


@ai_app.post("/ai/chat/messages", response_model=AIChatMessagesResponse)
async def ai_chat_messages(payload: AIChatMessagesRequest) -> AIChatMessagesResponse:
    identity = _resolve_identity(payload.model_dump())
    data = await _chat_message_items(identity, payload)
    return AIChatMessagesResponse(**data)


@ai_app.post("/ai/thread/all", response_model=AIThreadListResponse)
async def ai_thread_all(payload: AIThreadAllRequest) -> AIThreadListResponse:
    identity = _resolve_identity(payload.model_dump())
    data = await _thread_all_items(identity, payload)
    return AIThreadListResponse(**data)


@ai_app.post("/ai/thread/history", response_model=AIThreadHistoryResponse)
async def ai_thread_history(payload: AIThreadHistoryRequest) -> AIThreadHistoryResponse:
    identity = _resolve_identity(payload.model_dump())
    data = await _thread_history(identity, payload.item_id, payload.chat_id)
    return AIThreadHistoryResponse(**data)


@ai_app.post("/ai/archive/media")
async def ai_archive_media(payload: AIArchiveMediaRequest) -> Response:
    identity = _resolve_identity(payload.model_dump())
    row = await _get_archive_row(identity.user_id, payload.item_id)
    media_path = await _normalize_media_reference(
        table_name="deleted_messages",
        item_id=int(row["id"]),
        owner_id=identity.user_id,
        media_path=str(row["media_path"] or "").strip(),
        endpoint="/ai/archive/media",
    )
    category = _archive_category_from_values(row["content_type"], media_path)
    if category != "photo":
        raise HTTPException(status_code=400, detail="Preview is available only for photo media.")
    content, media_type = await _read_media_file(media_path)
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


@ai_app.post("/ai/thread/media")
async def ai_thread_media(payload: AIArchiveMediaRequest) -> Response:
    identity = _resolve_identity(payload.model_dump())
    row = await _get_thread_row(identity.user_id, payload.item_id, payload.chat_id)
    media_path = await _normalize_media_reference(
        table_name="chat_thread_messages",
        item_id=int(row["id"]),
        owner_id=identity.user_id,
        media_path=str(row["media_path"] or "").strip(),
        endpoint="/ai/thread/media",
    )
    if not media_path:
        raise HTTPException(status_code=400, detail="Media file not found for this message.")
    content, media_type = await _read_media_file(media_path)
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


@ai_app.post("/ai/chat/avatar")
async def ai_chat_avatar(payload: AIChatAvatarRequest) -> Response:
    identity = _resolve_identity(payload.model_dump())
    async with aiosqlite.connect(CONFIG.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT COALESCE(photo_url, '') AS photo_url
            FROM chat_dialogs
            WHERE owner_id = ? AND chat_id = ?
            LIMIT 1
            """,
            (identity.user_id, payload.chat_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Dialog not found.")
    photo_path = str(row["photo_url"] or "").strip()
    if not photo_path or not os.path.exists(photo_path):
        raise HTTPException(status_code=404, detail="Avatar is not available for this dialog.")
    content, media_type = await _read_media_file(photo_path)
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=900"})


@ai_app.post("/ai/archive/send-to-chat", response_model=AIArchiveSendResponse)
async def ai_archive_send_to_chat(payload: AIArchiveMediaRequest) -> AIArchiveSendResponse:
    identity = _resolve_identity(payload.model_dump())
    result = await _send_archive_item_to_chat(identity, payload.item_id)
    return AIArchiveSendResponse(**result)


@ai_app.post("/ai/thread/send-to-chat", response_model=AIArchiveSendResponse)
async def ai_thread_send_to_chat(payload: AIArchiveMediaRequest) -> AIArchiveSendResponse:
    identity = _resolve_identity(payload.model_dump())
    result = await _send_thread_item_to_chat(identity, payload.item_id)
    return AIArchiveSendResponse(**result)


@ai_app.post("/ai/overview", response_model=AIOverviewResponse)
async def ai_overview(payload: AIIdentityPayload) -> AIOverviewResponse:
    identity = _resolve_identity(payload.model_dump())
    cache_key = _cache_key("overview", identity.user_id)
    cached = AI_RESPONSE_CACHE.get(cache_key)
    if cached:
        return AIOverviewResponse(**cached)
    overview = await _build_overview(identity)
    AI_RESPONSE_CACHE.set(cache_key, overview, ttl=AI_OVERVIEW_CACHE_TTL_SEC)
    return AIOverviewResponse(**overview)


@ai_app.post("/ai/archive/status", response_model=AIArchiveStatusResponse)
async def ai_archive_status(payload: AIIdentityPayload) -> AIArchiveStatusResponse:
    identity = _resolve_identity(payload.model_dump())
    cache_key = _cache_key("archive_status", identity.user_id)
    cached = AI_RESPONSE_CACHE.get(cache_key)
    if cached:
        return AIArchiveStatusResponse(**cached)
    status_payload = await _build_archive_status(identity)
    AI_RESPONSE_CACHE.set(cache_key, status_payload, ttl=10)
    return AIArchiveStatusResponse(**status_payload)


@ai_app.post("/ai/risk/summary", response_model=AIRiskSummaryResponse)
async def ai_risk_summary(payload: AIIdentityPayload) -> AIRiskSummaryResponse:
    identity = _resolve_identity(payload.model_dump())
    cache_key = _cache_key("risk_summary", identity.user_id)
    cached = AI_RESPONSE_CACHE.get(cache_key)
    if cached:
        return AIRiskSummaryResponse(**cached)
    summary = await _build_risk_summary(identity)
    AI_RESPONSE_CACHE.set(cache_key, summary, ttl=10)
    return AIRiskSummaryResponse(**summary)


@ai_app.post("/ai/profile/avatar")
async def ai_profile_avatar(payload: AIIdentityPayload) -> Response:
    identity = _resolve_identity(payload.model_dump())
    avatar = await _fetch_avatar_bytes(identity.user_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Аватар недоступен.")
    content, media_type = avatar
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=900"})


@ai_app.post("/ai/session/logout", response_model=AISessionLogoutResponse)
async def ai_session_logout(payload: AIIdentityPayload) -> AISessionLogoutResponse:
    identity = _resolve_identity(payload.model_dump())
    user_id = identity.user_id
    result = _close_user_session(user_id)
    _invalidate_user_cache(user_id)
    logger.info("Session close request user=%s closed=%s", user_id, result.get("session_closed"))
    return AISessionLogoutResponse(**result)
