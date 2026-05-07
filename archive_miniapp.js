(() => {
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const PAGE = Number(document.body.dataset.pageSize || "40") || 40;
  const HISTORY_PAGE = Math.max(PAGE, 120);
  const POLL_MS = 8000;
  const AUTO_HISTORY_PAUSE_MS = 120;
  const AUTO_HISTORY_MAX_PAGES = 120;
  const CACHE_KEY = "savedbot-miniapp-cache-v2";
  const THEME_KEY = "savedbot-miniapp-theme-mode";

  const state = {
    identity: null,
    profile: null,
    sessionActive: false,
    watcherActive: false,
    dialogs: [],
    currentChatId: null,
    currentChatMeta: null,
    messages: [],
    oldestMsgId: null,
    newestMsgId: null,
    hasMore: false,
    searchText: "",
    filter: "all",
    loadingDialogs: false,
    loadingMessages: false,
    loadingOlder: false,
    searchTimer: null,
    pollTimer: null,
    mediaUrlCache: new Map(),
    dialogAvatarCache: new Map(),
    dialogAvatarInflight: new Map(),
    profileAvatarUrl: "",
    infoOpen: false,
    historyEvents: [],
    historyTitle: "",
    themeMode: "auto",
    themeResolved: "dark",
    observer: null,
    view: "dialogs",
    lockedByError: false,
    messagesRequestId: 0,
    messagesAbortController: null,
    autoHistoryLoading: false,
    autoHistoryChatId: null,
  };

  const el = {
    appSubtitle: document.getElementById("appSubtitle"),
    mobileBackBtn: document.getElementById("mobileBackBtn"),
    themeOpenBtn: document.getElementById("themeOpenBtn"),
    refreshAllBtn: document.getElementById("refreshAllBtn"),
    openInfoBtn: document.getElementById("openInfoBtn"),
    profileAvatar: document.getElementById("profileAvatar"),
    profileName: document.getElementById("profileName"),
    profileMeta: document.getElementById("profileMeta"),
    dialogSearchInput: document.getElementById("dialogSearchInput"),
    dialogList: document.getElementById("dialogList"),
    chatAvatar: document.getElementById("chatAvatar"),
    chatTitle: document.getElementById("chatTitle"),
    chatMeta: document.getElementById("chatMeta"),
    chatRefreshBtn: document.getElementById("chatRefreshBtn"),
    chatInfoBtn: document.getElementById("chatInfoBtn"),
    syncStrip: document.getElementById("syncStrip"),
    messageFilters: document.getElementById("messageFilters"),
    timeline: document.getElementById("timeline"),
    loadOlderBtn: document.getElementById("loadOlderBtn"),
    infoPanel: document.getElementById("infoPanel"),
    closeInfoBtn: document.getElementById("closeInfoBtn"),
    infoSyncState: document.getElementById("infoSyncState"),
    infoCounters: document.getElementById("infoCounters"),
    infoMediaList: document.getElementById("infoMediaList"),
    infoHistoryList: document.getElementById("infoHistoryList"),
    logoutBtn: document.getElementById("logoutBtn"),
    panelBackdrop: document.getElementById("panelBackdrop"),
    themeSheet: document.getElementById("themeSheet"),
    themeCloseBtn: document.getElementById("themeCloseBtn"),
    historyModal: document.getElementById("historyModal"),
    historyTitle: document.getElementById("historyTitle"),
    historyBody: document.getElementById("historyBody"),
    historyCloseBtn: document.getElementById("historyCloseBtn"),
    toasts: document.getElementById("toasts"),
  };

  const THEME_VAR_KEYS = ["--bg", "--panel", "--panel-2", "--text", "--muted", "--accent"];

  function esc(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function toInt(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.trunc(n) : 0;
  }

  function toPositiveInt(value) {
    const n = toInt(value);
    return n > 0 ? n : null;
  }

  function messageKey(msg) {
    if (msg && msg.chat_id != null && msg.msg_id != null) return `m:${msg.chat_id}:${msg.msg_id}`;
    if (msg && msg.item_id != null) return `i:${msg.item_id}`;
    const createdAt = msg && msg.created_at ? String(msg.created_at) : "";
    const sender = msg && msg.sender_label ? String(msg.sender_label) : "";
    const text = msg && (msg.text || msg.original_text) ? String(msg.text || msg.original_text) : "";
    return `x:${createdAt}:${sender}:${text.slice(0, 80)}`;
  }

  function initials(label) {
    const source = String(label || "").trim();
    if (!source) return "SB";
    const words = source.split(/\s+/).filter(Boolean);
    if (words.length > 1) return (words[0][0] + words[1][0]).toUpperCase();
    return source.slice(0, 2).toUpperCase();
  }

  function setAvatar(container, label, imageUrl) {
    if (!container) return;
    const span = container.querySelector("span");
    const img = container.querySelector("img");
    if (span) span.textContent = initials(label);
    if (img && imageUrl) {
      img.src = imageUrl;
      container.classList.add("has-photo");
    } else if (img) {
      img.removeAttribute("src");
      container.classList.remove("has-photo");
    }
  }

  function formatClock(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function formatDateTitle(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "Дата неизвестна";
    return date.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
  }

  function dateKey(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "unknown";
    return date.toISOString().slice(0, 10);
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function messageBounds(messages) {
    const ids = filterMessagesByChat(messages, state.currentChatId)
      .map((msg) => toPositiveInt(msg && msg.msg_id))
      .filter(Boolean);
    if (!ids.length) return { oldest: null, newest: null };
    return {
      oldest: Math.min(...ids),
      newest: Math.max(...ids),
    };
  }

  function updateLoadOlderButton() {
    if (!el.loadOlderBtn) return;
    const showFallback = Boolean(state.currentChatId && state.hasMore && !state.autoHistoryLoading);
    el.loadOlderBtn.hidden = !showFallback;
    el.loadOlderBtn.textContent = (state.loadingOlder || state.autoHistoryLoading)
      ? "Загружаю историю..."
      : "Загрузить старые сообщения";
  }

  function resolveSyncStatusLine() {
    if (!state.currentChatMeta) return "Выберите чат.";
    if (!state.sessionActive) return "Сессия неактивна.";
    if (!state.watcherActive) return "Синхронизация на паузе.";
    if (state.currentChatMeta.history_complete) return "Архив готов.";
    return state.autoHistoryLoading ? "Загружаю историю..." : "Архив загружен.";
  }

  function syncBadgeText(syncStatus) {
    if (syncStatus === "complete") return "Архив готов";
    if (syncStatus === "syncing") return "Синхронизация";
    return "Пауза";
  }

  function dialogLabel(dialog) {
    if (dialog.username) return `@${dialog.username}`;
    if (dialog.dialog_type === "channel") return "Канал";
    if (dialog.dialog_type === "group") return "Группа";
    return "Личный чат";
  }

  function apiPayload(extra) {
    return Object.assign({}, state.identity || {}, extra || {});
  }

  async function api(path, payload, options = {}) {
    const signal = options && options.signal ? options.signal : undefined;
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(apiPayload(payload)),
      signal,
    });
    const contentType = String(response.headers.get("content-type") || "");
    const parsed = contentType.includes("application/json")
      ? await response.json().catch(() => ({}))
      : await response.text().catch(() => "");
    if (!response.ok) {
      const detail = parsed && typeof parsed === "object" ? parsed.detail || parsed.message : parsed;
      const err = new Error(String(detail || "Ошибка запроса."));
      err.status = response.status;
      throw err;
    }
    return parsed;
  }

  async function apiBlob(path, payload, options = {}) {
    const signal = options && options.signal ? options.signal : undefined;
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(apiPayload(payload)),
      signal,
    });
    if (!response.ok) {
      let detail = "Не удалось загрузить вложение.";
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (_) {
        // ignore
      }
      const err = new Error(detail);
      err.status = response.status;
      throw err;
    }
    return response.blob();
  }

  function toast(text, type = "info") {
    if (!el.toasts) return;
    const node = document.createElement("div");
    node.className = `toast ${type === "error" ? "error" : ""}`.trim();
    node.textContent = String(text || "");
    el.toasts.appendChild(node);
    setTimeout(() => node.remove(), 3400);
  }

  function buildIdentity() {
    const params = new URLSearchParams(window.location.search);
    const queryUserId = toPositiveInt(params.get("user_id"));
    const telegramUser = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user || {} : {};
    const initData = tg && typeof tg.initData === "string" ? tg.initData.trim() : "";
    
    const payload = {};
    if (initData) {
      payload.init_data = initData;
      console.log("[Mini App] ✅ initData получен из Telegram WebApp API");
    } else if (queryUserId) {
      payload.user_id = queryUserId;
      console.warn("[Mini App] ⚠️ initData отсутствует, используется fallback: user_id из URL", queryUserId);
    } else {
      console.error("[Mini App] ❌ КРИТИЧНО: initData и user_id оба отсутствуют!");
      console.error("[Mini App] tg.initData:", tg && tg.initData ? "присутствует" : "null");
      console.error("[Mini App] tg.initDataUnsafe.user:", telegramUser ? JSON.stringify(telegramUser) : "null");
      console.error("[Mini App] Убедитесь, что Mini App открывается через web_app кнопку в Telegram боте");
    }
    
    ["first_name", "last_name", "username", "language_code", "photo_url"].forEach((key) => {
      if (telegramUser && telegramUser[key]) payload[key] = String(telegramUser[key]);
    });
    
    console.log("[Mini App] Payload для первого запроса:", payload);
    return payload;
  }

  function saveCache() {
    try {
      const cache = {
        profile: state.profile,
        dialogs: state.dialogs.slice(0, 120),
      };
      localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
    } catch (_) {
      // ignore
    }
  }

  function loadCache() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return;
      const cache = JSON.parse(raw);
      if (cache && Array.isArray(cache.dialogs)) state.dialogs = cache.dialogs;
      state.currentChatId = null;
      state.currentChatMeta = null;
      clearMessagesState();
      state.profile = cache.profile || null;
    } catch (_) {
      // ignore
    }
  }

  function clearMessagesState() {
    state.messages = [];
    state.oldestMsgId = null;
    state.newestMsgId = null;
    state.hasMore = false;
    state.autoHistoryLoading = false;
    state.autoHistoryChatId = null;
    updateLoadOlderButton();
  }

  function clearCurrentChatSelection() {
    state.currentChatId = null;
    state.currentChatMeta = null;
    clearMessagesState();
    renderDialogs();
    renderChatHeader();
    renderTimeline();
    renderInfoPanel();
    updateAppSubtitle();
  }

  function filterMessagesByChat(messages, chatId) {
    const numericChatId = toInt(chatId);
    if (!Array.isArray(messages) || !numericChatId) return [];
    return messages.filter((msg) => toInt(msg && msg.chat_id) === numericChatId);
  }

  function resolveAutoTheme() {
    const tgScheme = tg && tg.colorScheme ? String(tg.colorScheme).toLowerCase() : "";
    if (tgScheme === "dark" || tgScheme === "light") return tgScheme;
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    return "light";
  }

  function applyTelegramThemeVars(mode, resolved) {
    const rootStyle = document.documentElement.style;
    THEME_VAR_KEYS.forEach((key) => rootStyle.removeProperty(key));
    const p = tg && tg.themeParams ? tg.themeParams : {};
    if (mode === "auto" && p) {
      if (p.bg_color) rootStyle.setProperty("--bg", p.bg_color);
      if (p.secondary_bg_color) rootStyle.setProperty("--panel", p.secondary_bg_color);
      if (p.section_bg_color) rootStyle.setProperty("--panel-2", p.section_bg_color);
      if (p.text_color) rootStyle.setProperty("--text", p.text_color);
      if (p.hint_color) rootStyle.setProperty("--muted", p.hint_color);
      if (p.link_color || p.button_color) rootStyle.setProperty("--accent", p.link_color || p.button_color);
    }

    if (!tg) return;
    try {
      const fallbackHeader = resolved === "dark" ? "#18222d" : "#ffffff";
      const fallbackBg = resolved === "dark" ? "#0f141a" : "#eef4fa";
      const header = (mode === "auto" && p && p.secondary_bg_color) ? p.secondary_bg_color : fallbackHeader;
      const bg = (mode === "auto" && p && p.bg_color) ? p.bg_color : fallbackBg;
      if (typeof tg.setHeaderColor === "function") tg.setHeaderColor(header);
      if (typeof tg.setBackgroundColor === "function") tg.setBackgroundColor(bg);
    } catch (_) {
      // optional
    }
  }

  function renderThemeOptions() {
    const options = document.querySelectorAll(".theme-option");
    options.forEach((btn) => {
      const mode = String(btn.dataset.themeMode || "");
      btn.classList.toggle("is-active", mode === state.themeMode);
    });
  }

  function setThemeMode(mode) {
    const normalized = mode === "light" || mode === "dark" || mode === "auto" ? mode : "auto";
    state.themeMode = normalized;
    state.themeResolved = normalized === "auto" ? resolveAutoTheme() : normalized;
    document.body.dataset.themeMode = normalized;
    document.body.dataset.theme = state.themeResolved;
    localStorage.setItem(THEME_KEY, normalized);
    applyTelegramThemeVars(normalized, state.themeResolved);
    renderThemeOptions();
  }

  function setView(view) {
    state.view = view === "chat" ? "chat" : "dialogs";
    document.body.dataset.view = state.view;
  }

  function toggleInfoPanel(open) {
    state.infoOpen = Boolean(open);
    document.body.classList.toggle("info-open", state.infoOpen);
    if (el.infoPanel) el.infoPanel.setAttribute("aria-hidden", state.infoOpen ? "false" : "true");
    if (el.panelBackdrop) el.panelBackdrop.hidden = !state.infoOpen;
    renderInfoPanel();
  }

  function toggleThemeSheet(open) {
    if (!el.themeSheet) return;
    el.themeSheet.hidden = !open;
    if (el.panelBackdrop) el.panelBackdrop.hidden = !open && !state.infoOpen;
    document.body.classList.toggle("theme-open", open);
  }

  function openHistoryModal(title, events) {
    state.historyTitle = title || "История сообщения";
    state.historyEvents = Array.isArray(events) ? events : [];
    if (!el.historyModal || !el.historyBody || !el.historyTitle) return;
    el.historyTitle.textContent = state.historyTitle;
    if (!state.historyEvents.length) {
      el.historyBody.innerHTML = "<div class='compact-item'>История отсутствует.</div>";
    } else {
      el.historyBody.innerHTML = state.historyEvents
        .map(
          (ev) => `
            <article class="history-event">
              <div class="event-type">${esc(ev.event_type || "событие")}</div>
              <div>${esc(ev.text || ev.previous_text || "—")}</div>
              ${ev.previous_text ? `<div><small>Было: ${esc(ev.previous_text)}</small></div>` : ""}
              <div class="event-time">${esc(ev.created_at || "—")}</div>
            </article>
          `
        )
        .join("");
    }
    el.historyModal.hidden = false;
    document.body.classList.add("history-open");
  }

  function closeHistoryModal() {
    if (!el.historyModal) return;
    el.historyModal.hidden = true;
    document.body.classList.remove("history-open");
  }

  function applyProfile(profile) {
    state.profile = profile || null;
    if (!profile) return;
    const username = profile.username ? `@${profile.username}` : "без username";
    if (el.profileName) el.profileName.textContent = profile.display_name || "Пользователь";
    if (el.profileMeta) el.profileMeta.textContent = `${username} · ID ${profile.user_id}`;
    setAvatar(el.profileAvatar, profile.display_name || profile.username || "User", profile.photo_url || state.profileAvatarUrl || "");
  }

  function setFatalState(message) {
    state.lockedByError = true;
    const text = String(message || "Доступ к Mini App ограничен.");
    if (el.appSubtitle) el.appSubtitle.textContent = text;
    if (el.timeline) {
      el.timeline.innerHTML = `
        <div class="empty-state">
          <strong>Доступ ограничен</strong>
          <p>${esc(text)}</p>
        </div>
      `;
    }
    if (el.dialogList) {
      el.dialogList.innerHTML = `
        <div class="empty-state">
          <strong>Чаты недоступны</strong>
          <p>${esc(text)}</p>
        </div>
      `;
    }
  }

  function updateAppSubtitle() {
    if (state.lockedByError) return;
    if (!state.sessionActive) {
      el.appSubtitle.textContent = "Сессия неактивна";
      return;
    }
    if (!state.watcherActive) {
      el.appSubtitle.textContent = "Архив доступен, синхронизация на паузе";
      return;
    }
    el.appSubtitle.textContent = "Синхронизация активна";
  }

  function renderDialogs() {
    if (!el.dialogList) return;
    if (!state.dialogs.length) {
      el.dialogList.innerHTML = `
        <div class="empty-state">
          <strong>Чаты пока не найдены</strong>
          <p>Подключаю историю… список появится автоматически.</p>
        </div>
      `;
      return;
    }

    el.dialogList.innerHTML = state.dialogs
      .map((dialog) => {
        const chatId = toInt(dialog.chat_id);
        const isActive = chatId === state.currentChatId;
        const unread = toInt(dialog.unread_count);
        const deletedCount = toInt(dialog.deleted_count);
        const editedCount = toInt(dialog.edited_count);
        const indicators = [];
        indicators.push(`<span class="badge sync">${esc(syncBadgeText(dialog.sync_status))}</span>`);
        if (unread > 0) indicators.push(`<span class="badge unread">Новых: ${unread}</span>`);
        if (deletedCount > 0) indicators.push(`<span class="badge deleted">Удалений: ${deletedCount}</span>`);
        if (editedCount > 0) indicators.push(`<span class="badge edited">Изменений: ${editedCount}</span>`);

        return `
          <button class="dialog-item ${isActive ? "is-active" : ""}" type="button" data-chat-id="${chatId}">
            <div class="avatar js-dialog-avatar" data-chat-id="${chatId}"><span>${esc(initials(dialog.title || "CH"))}</span><img alt="avatar" /></div>
            <div class="dialog-main">
              <div class="dialog-top">
                <strong class="dialog-name">${esc(dialog.title || "Диалог")}</strong>
                <span class="dialog-time">${esc(formatClock(dialog.last_message_at))}</span>
              </div>
              <div class="dialog-preview">${esc(dialog.last_message_preview || dialogLabel(dialog))}</div>
              <div class="dialog-indicators">${indicators.join("")}</div>
            </div>
          </button>
        `;
      })
      .join("");

    document.querySelectorAll(".js-dialog-avatar").forEach((node) => {
      const chatId = toInt(node.dataset.chatId);
      const dialog = state.dialogs.find((d) => toInt(d.chat_id) === chatId);
      if (!dialog) return;
      const cached = state.dialogAvatarCache.get(chatId) || "";
      setAvatar(node, dialog.title || "CH", cached);
      if (!cached) requestDialogAvatar(chatId, dialog.title || "CH", node);
    });
  }

  function filteredMessages() {
    const scoped = filterMessagesByChat(state.messages, state.currentChatId);
    if (state.filter === "deleted") {
      return scoped.filter((msg) => String(msg.status || "active") === "deleted");
    }
    if (state.filter === "edited") {
      return scoped.filter((msg) => {
        const status = String(msg.status || "active");
        return status === "edited" || toInt(msg.edit_count) > 0;
      });
    }
    return scoped;
  }

  function renderChatHeader() {
    const chat = state.currentChatMeta;
    if (!chat) {
      if (el.chatTitle) el.chatTitle.textContent = "Выберите чат";
      if (el.chatMeta) el.chatMeta.textContent = "Здесь будет лента сообщений.";
      setAvatar(el.chatAvatar, "CH", "");
      if (el.syncStrip) el.syncStrip.textContent = "Откройте диалог, чтобы загрузить архив.";
      return;
    }
    if (el.chatTitle) el.chatTitle.textContent = chat.title || "Диалог";
    if (el.chatMeta) el.chatMeta.textContent = dialogLabel(chat);
    setAvatar(el.chatAvatar, chat.title || "CH", state.dialogAvatarCache.get(toInt(chat.chat_id)) || "");
    if (el.syncStrip) el.syncStrip.textContent = resolveSyncStatusLine();
  }

  function ensureMediaObserver() {
    if (state.observer || typeof IntersectionObserver !== "function") return;
    state.observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const host = entry.target;
        const itemId = toInt(host.dataset.itemId);
        if (!itemId || host.dataset.loaded === "1") return;
        loadMediaPreview(itemId, host).catch(() => {
          // ignore per-item errors
        });
      });
    }, { root: el.timeline, rootMargin: "120px 0px 120px 0px" });
  }

  function observeMediaPlaceholders() {
    ensureMediaObserver();
    if (!state.observer) return;
    const placeholders = el.timeline ? el.timeline.querySelectorAll(".js-media-preview[data-item-id]") : [];
    placeholders.forEach((node) => state.observer.observe(node));
  }

  async function loadMediaPreview(itemId, host) {
    if (!host || host.dataset.loaded === "1") return;
    host.dataset.loaded = "1";
    host.innerHTML = "<span>Загрузка медиа…</span>";
    try {
      let url = state.mediaUrlCache.get(itemId);
      let mime = host.dataset.mime || "";
      if (!url) {
        const blob = await apiBlob("/ai/thread/media", {
          item_id: itemId,
          chat_id: state.currentChatId || 0,
        });
        mime = blob.type || mime || "application/octet-stream";
        url = URL.createObjectURL(blob);
        state.mediaUrlCache.set(itemId, url);
      }
      if (mime.startsWith("image/")) {
        host.innerHTML = `<img src="${url}" alt="media" loading="lazy" />`;
      } else if (mime.startsWith("video/")) {
        host.innerHTML = `<video src="${url}" controls preload="metadata"></video>`;
      } else if (mime.startsWith("audio/")) {
        host.innerHTML = `<audio src="${url}" controls></audio>`;
      } else {
        host.innerHTML = `<a class="history-link" href="${url}" download>Скачать файл</a>`;
      }
    } catch (err) {
      host.dataset.loaded = "0";
      host.innerHTML = `
        <span>Не удалось загрузить медиа</span>
        <button class="history-link js-media-retry" type="button" data-item-id="${itemId}">Повторить</button>
      `;
      throw err;
    }
  }

  function createMessageMarkup(msg, dayMarkerChanged) {
    const isOutgoing = Boolean(msg.is_outgoing);
    const rowClass = `message-row ${isOutgoing ? "outgoing" : "incoming"}`;
    const bubbleClass = `bubble ${msg.reply_to_msg_id ? "reply" : ""}`.trim();
    const timeText = formatClock(msg.created_at || msg.updated_at || msg.deleted_at);
    const status = String(msg.status || "active");
    const labels = [];
    if (status === "deleted") labels.push('<span class="state-pill deleted">Удалено, но сохранено</span>');
    if (status === "edited" || toInt(msg.edit_count) > 0) labels.push('<span class="state-pill edited">Изменено</span>');

    const hasHistory = status !== "active" || toInt(msg.edit_count) > 0;
    const historyBtn = hasHistory
      ? `<button class="history-link js-history-link" type="button" data-item-id="${toInt(msg.item_id)}">История</button>`
      : "";

    const replyPreview = msg.reply_to_msg_id
      ? `
      <div class="reply-preview">
        <strong>${esc(msg.reply_preview_sender_label || "Ответ")}</strong>
        <span>${esc(msg.reply_preview_text || "Сообщение")}</span>
      </div>`
      : "";

    const rawText = String(msg.text || msg.original_text || "").trim();
    const text = esc(rawText);
    const contentType = String(msg.content_type || "").toLowerCase();
    const mediaLabel = msg.has_media
      ? (contentType.includes("photo") ? "Фото"
        : contentType.includes("video") ? "Видео"
        : contentType.includes("voice") || contentType.includes("audio") ? "Аудио"
        : "Файл")
      : "Сообщение";
    const textBlock = text
      ? `<div class="bubble-text">${text}</div>`
      : `<div class="bubble-text muted">${esc(mediaLabel)}</div>`;
    const mediaPlaceholder = msg.has_preview
      ? `<div class="media-preview js-media-preview" data-item-id="${toInt(msg.item_id)}" data-mime=""><span>Медиа появится при прокрутке</span></div>`
      : "";

    return `
      ${dayMarkerChanged}
      <article class="${rowClass}">
        <div class="${bubbleClass}">
          ${replyPreview}
          ${textBlock}
          ${mediaPlaceholder}
          <div class="bubble-meta">
            <span>${esc(timeText)}</span>
            ${labels.join("")}
            ${historyBtn}
          </div>
        </div>
      </article>
    `;
  }

  function renderTimeline() {
    if (!el.timeline) return;
    const list = filteredMessages();
    if (!list.length) {
      const text = state.filter === "all" ? "Сообщений пока нет." : "Нет сообщений для выбранного фильтра.";
      el.timeline.innerHTML = `
        <div class="empty-state">
          <strong>Пусто</strong>
          <p>${esc(text)}</p>
        </div>
      `;
      return;
    }

    let lastDay = "";
    const html = list
      .map((msg) => {
        const currentDay = dateKey(msg.created_at || msg.updated_at || msg.deleted_at);
        let dayMarker = "";
        if (currentDay !== lastDay) {
          dayMarker = `<div class="day-separator">${esc(formatDateTitle(msg.created_at || msg.updated_at || msg.deleted_at))}</div>`;
          lastDay = currentDay;
        }
        return createMessageMarkup(msg, dayMarker);
      })
      .join("");

    const prevHeight = el.timeline.scrollHeight;
    const wasAtBottom = el.timeline.scrollTop + el.timeline.clientHeight + 40 >= prevHeight;
    el.timeline.innerHTML = html;
    observeMediaPlaceholders();
    if (wasAtBottom && !state.loadingOlder) {
      el.timeline.scrollTop = el.timeline.scrollHeight;
    }
  }

  function renderInfoPanel() {
    if (!el.infoSyncState || !el.infoCounters || !el.infoMediaList || !el.infoHistoryList) return;
    if (!state.currentChatMeta) {
      el.infoSyncState.textContent = "Откройте чат, чтобы показать детали.";
      el.infoCounters.innerHTML = "";
      el.infoMediaList.innerHTML = "<div class='compact-item'>Нет данных.</div>";
      el.infoHistoryList.innerHTML = "<div class='compact-item'>Нет данных.</div>";
      return;
    }

    el.infoSyncState.textContent = resolveSyncStatusLine();
    const loaded = state.messages.length;
    const deletedLoaded = state.messages.filter((msg) => String(msg.status || "active") === "deleted").length;
    const editedLoaded = state.messages.filter((msg) => String(msg.status || "active") === "edited" || toInt(msg.edit_count) > 0).length;
    const mediaLoaded = state.messages.filter((msg) => Boolean(msg.has_media)).length;
    const counters = [
      { value: toInt(state.currentChatMeta.message_count), label: "Всего в архиве" },
      { value: loaded, label: "Загружено сейчас" },
      { value: deletedLoaded, label: "Удалённых" },
      { value: editedLoaded, label: "Изменённых" },
      { value: mediaLoaded, label: "С медиа" },
      { value: state.hasMore ? "Да" : "Нет", label: "Есть старее" },
    ];
    el.infoCounters.innerHTML = counters
      .map((item) => `<div class="counter-card"><strong>${esc(String(item.value))}</strong><span>${esc(item.label)}</span></div>`)
      .join("");

    const mediaItems = state.messages.filter((msg) => Boolean(msg.has_preview)).slice(-12).reverse();
    if (!mediaItems.length) {
      el.infoMediaList.innerHTML = "<div class='compact-item'>В текущей выборке нет медиа.</div>";
    } else {
      el.infoMediaList.innerHTML = mediaItems
        .map(
          (msg) => `
          <div class="compact-item">
            <div>${esc(msg.sender_label || "Пользователь")} · ${esc(formatClock(msg.created_at))}</div>
            <button class="history-link js-info-media" type="button" data-item-id="${toInt(msg.item_id)}">Открыть медиа</button>
          </div>`
        )
        .join("");
    }

    const changeItems = state.messages
      .filter((msg) => String(msg.status || "active") !== "active" || toInt(msg.edit_count) > 0)
      .slice(-12)
      .reverse();
    if (!changeItems.length) {
      el.infoHistoryList.innerHTML = "<div class='compact-item'>Нет изменений в текущей выборке.</div>";
    } else {
      el.infoHistoryList.innerHTML = changeItems
        .map(
          (msg) => `
          <div class="compact-item">
            <div>${esc(msg.sender_label || "Пользователь")} · ${esc(formatClock(msg.updated_at || msg.created_at))}</div>
            <button class="history-link js-info-history" type="button" data-item-id="${toInt(msg.item_id)}">Показать историю</button>
          </div>`
        )
        .join("");
    }
  }

  async function requestProfileAvatar() {
    if (!state.profile || state.profileAvatarUrl) return;
    if (state.profile.photo_url) {
      state.profileAvatarUrl = state.profile.photo_url;
      setAvatar(el.profileAvatar, state.profile.display_name || "User", state.profileAvatarUrl);
      return;
    }
    try {
      const blob = await apiBlob("/ai/profile/avatar", {});
      if (state.profileAvatarUrl && state.profileAvatarUrl.startsWith("blob:")) {
        try {
          URL.revokeObjectURL(state.profileAvatarUrl);
        } catch (_) {
          // ignore
        }
      }
      state.profileAvatarUrl = URL.createObjectURL(blob);
      setAvatar(el.profileAvatar, state.profile.display_name || "User", state.profileAvatarUrl);
    } catch (_) {
      // optional
    }
  }

  async function requestDialogAvatar(chatId, label, node) {
    if (!chatId) return;
    if (state.dialogAvatarInflight.has(chatId)) return;
    const pending = apiBlob("/ai/chat/avatar", { chat_id: chatId })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const previous = state.dialogAvatarCache.get(chatId);
        if (previous && previous !== url && String(previous).startsWith("blob:")) {
          try {
            URL.revokeObjectURL(previous);
          } catch (_) {
            // ignore
          }
        }
        state.dialogAvatarCache.set(chatId, url);
        setAvatar(node, label, url);
      })
      .catch(() => {
        // optional
      })
      .finally(() => {
        state.dialogAvatarInflight.delete(chatId);
      });
    state.dialogAvatarInflight.set(chatId, pending);
  }

  function mergeMessages(incoming, isOlderChunk, chatId) {
    const scopedIncoming = filterMessagesByChat(incoming, chatId);
    const current = filterMessagesByChat(state.messages, chatId).slice();
    if (isOlderChunk) {
      const existingKeys = new Set(current.map(messageKey));
      const prepend = scopedIncoming.filter((msg) => !existingKeys.has(messageKey(msg)));
      return prepend.concat(current);
    }
    const map = new Map();
    current.forEach((msg) => map.set(messageKey(msg), msg));
    scopedIncoming.forEach((msg) => map.set(messageKey(msg), msg));
    const list = Array.from(map.values());
    list.sort((a, b) => {
      const aMsg = a.msg_id != null ? Number(a.msg_id) : Number.MAX_SAFE_INTEGER;
      const bMsg = b.msg_id != null ? Number(b.msg_id) : Number.MAX_SAFE_INTEGER;
      if (aMsg !== bMsg) return aMsg - bMsg;
      const aTime = new Date(a.created_at || a.updated_at || 0).getTime();
      const bTime = new Date(b.created_at || b.updated_at || 0).getTime();
      return aTime - bTime;
    });
    return list;
  }

  async function fetchDialogs({ silent = false } = {}) {
    if (state.loadingDialogs || state.lockedByError) return;
    state.loadingDialogs = true;
    try {
      const data = await api("/ai/chat/dialogs", {
        search: state.searchText,
        limit: 180,
      });
      state.profile = data.profile || state.profile;
      state.sessionActive = Boolean(data.session_active);
      state.watcherActive = Boolean(data.watcher_active);
      state.dialogs = Array.isArray(data.dialogs) ? data.dialogs : [];
      applyProfile(state.profile);
      requestProfileAvatar().catch(() => {});

      if (state.currentChatId && !state.dialogs.some((d) => toInt(d.chat_id) === state.currentChatId)) {
        clearCurrentChatSelection();
      } else if (state.currentChatId) {
        state.currentChatMeta = state.dialogs.find((d) => toInt(d.chat_id) === state.currentChatId) || null;
      }

      renderDialogs();
      renderChatHeader();
      updateAppSubtitle();
      renderInfoPanel();
      saveCache();
    } catch (err) {
      console.error("[Mini App] fetchDialogs error:", err);
      const errMsg = err && err.message ? String(err.message) : "Не удалось загрузить диалоги.";
      const errStatus = err && err.status ? ` (${err.status})` : "";
      
      if (err && (err.status === 401 || err.status === 402)) {
        // Authentication or subscription error - show to user and lock
        console.error("[Mini App] CRITICAL: Auth/subscription error:", errStatus, errMsg);
        setFatalState(errMsg + errStatus);
      } else if (err && err.status === 503) {
        console.warn("[Mini App] Backend temporarily unavailable, retrying...");
        if (!silent) toast("Сервер временно недоступен. Повторяю попытку...", "error");
      } else if (!silent) {
        console.warn("[Mini App] Non-fatal error in fetchDialogs:", errStatus, errMsg);
        toast(errMsg + errStatus, "error");
      }
    } finally {
      state.loadingDialogs = false;
    }
  }

  // Legacy implementation kept only for diff safety; current one is below with abort/request guards.
  async function fetchMessagesLegacyUnused(chatId, { beforeMsgId = null, silent = false } = {}) {
    const numericChatId = toInt(chatId);
    if (!numericChatId || state.lockedByError) return;
    if (beforeMsgId && state.loadingOlder) return;
    if (!beforeMsgId && state.loadingMessages) return;

    if (beforeMsgId) {
      state.loadingOlder = true;
      if (el.loadOlderBtn) el.loadOlderBtn.textContent = "Загрузка...";
    } else {
      state.loadingMessages = true;
      const switchingChat = state.currentChatId !== numericChatId;
      if (switchingChat) {
        state.currentChatId = numericChatId;
        state.currentChatMeta = state.dialogs.find((d) => toInt(d.chat_id) === numericChatId) || null;
        state.messages = [];
        state.oldestMsgId = null;
        state.newestMsgId = null;
        state.hasMore = false;
        renderDialogs();
        renderChatHeader();
        renderTimeline();
        renderInfoPanel();
      }
    }

    try {
      const requestId = ++state.messagesRequestId;
      const payload = { chat_id: numericChatId, limit: PAGE };
      if (beforeMsgId) payload.before_msg_id = toInt(beforeMsgId);
      const data = await api("/ai/chat/messages", payload);
      if (requestId !== state.messagesRequestId) return;
      const incoming = Array.isArray(data.messages) ? data.messages : [];
      const isOlderChunk = Boolean(beforeMsgId);
      state.messages = mergeMessages(incoming, isOlderChunk);
      state.currentChatId = numericChatId;
      state.currentChatMeta = {
        chat_id: numericChatId,
        title: data.title || "Диалог",
        username: data.username || "",
        dialog_type: data.dialog_type || "private",
        history_complete: Boolean(data.history_complete),
        has_photo: Boolean(data.has_photo),
        message_count: toInt(
          (state.dialogs.find((d) => toInt(d.chat_id) === numericChatId) || {}).message_count ||
          state.messages.length
        ),
      };
      state.sessionActive = Boolean(data.session_active);
      state.watcherActive = Boolean(data.watcher_active);
      state.oldestMsgId = data.oldest_loaded_msg_id != null ? toInt(data.oldest_loaded_msg_id) : null;
      state.newestMsgId = data.newest_loaded_msg_id != null ? toInt(data.newest_loaded_msg_id) : null;
      state.hasMore = Boolean(data.has_more);

      renderChatHeader();
      renderTimeline();
      renderInfoPanel();
      updateAppSubtitle();
      if (!beforeMsgId && window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
        setView("chat");
      }
      saveCache();
    } catch (err) {
      if (err && (err.status === 401 || err.status === 402)) {
        setFatalState(err.message);
      } else if (!silent) {
        toast(err.message || "Не удалось загрузить сообщения.", "error");
      }
    } finally {
      if (beforeMsgId) {
        state.loadingOlder = false;
        if (el.loadOlderBtn) el.loadOlderBtn.textContent = state.hasMore ? "Загрузить старые сообщения" : "Старых сообщений больше нет";
      } else {
        state.loadingMessages = false;
      }
    }
  }

  async function fetchMessages(chatId, { beforeMsgId = null, silent = false } = {}) {
    const numericChatId = toInt(chatId);
    if (!numericChatId || state.lockedByError) return;
    const isOlderChunk = Boolean(beforeMsgId);
    if (isOlderChunk) {
      if (state.loadingOlder) return;
      if (state.currentChatId !== numericChatId) return;
      state.loadingOlder = true;
      updateLoadOlderButton();
    } else {
      if (state.loadingMessages && state.currentChatId === numericChatId) return;
      state.loadingMessages = true;
      if (state.messagesAbortController) {
        try {
          state.messagesAbortController.abort();
        } catch (_) {
          // ignore
        }
      }
      state.messagesAbortController = new AbortController();
      if (state.currentChatId !== numericChatId) {
        state.currentChatId = numericChatId;
        state.currentChatMeta = state.dialogs.find((d) => toInt(d.chat_id) === numericChatId) || null;
        clearMessagesState();
        renderDialogs();
        renderChatHeader();
        renderTimeline();
        renderInfoPanel();
      }
    }

    const requestId = isOlderChunk ? state.messagesRequestId : ++state.messagesRequestId;
    const activeController = !isOlderChunk ? state.messagesAbortController : null;
    try {
      const payload = { chat_id: numericChatId, limit: isOlderChunk ? HISTORY_PAGE : PAGE };
      if (beforeMsgId) payload.before_msg_id = toInt(beforeMsgId);
      const data = await api(
        "/ai/chat/messages",
        payload,
        activeController ? { signal: activeController.signal } : undefined,
      );
      if (requestId !== state.messagesRequestId) return;
      if (state.currentChatId !== numericChatId) return;
      const incoming = filterMessagesByChat(Array.isArray(data.messages) ? data.messages : [], numericChatId);
      state.messages = mergeMessages(incoming, isOlderChunk, numericChatId);
      state.currentChatMeta = {
        chat_id: numericChatId,
        title: data.title || "Диалог",
        username: data.username || "",
        dialog_type: data.dialog_type || "private",
        history_complete: Boolean(data.history_complete),
        has_photo: Boolean(data.has_photo),
        message_count: toInt(
          (state.dialogs.find((d) => toInt(d.chat_id) === numericChatId) || {}).message_count ||
          state.messages.length
        ),
      };
      state.sessionActive = Boolean(data.session_active);
      state.watcherActive = Boolean(data.watcher_active);
      const bounds = messageBounds(state.messages);
      state.oldestMsgId = bounds.oldest || (data.oldest_loaded_msg_id != null ? toInt(data.oldest_loaded_msg_id) : null);
      state.newestMsgId = bounds.newest || (data.newest_loaded_msg_id != null ? toInt(data.newest_loaded_msg_id) : null);
      state.hasMore = Boolean(data.has_more);
      updateLoadOlderButton();

      renderDialogs();
      renderChatHeader();
      renderTimeline();
      renderInfoPanel();
      updateAppSubtitle();
      if (!isOlderChunk && window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
        setView("chat");
      }
      saveCache();
      if (!isOlderChunk) {
        startBackgroundHistoryLoad(numericChatId);
      }
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (err && (err.status === 401 || err.status === 402)) {
        setFatalState(err.message);
      } else if (!silent) {
        toast(err.message || "Не удалось загрузить сообщения.", "error");
      }
    } finally {
      if (!isOlderChunk && activeController && state.messagesAbortController === activeController) {
        state.messagesAbortController = null;
      }
      if (isOlderChunk) {
        state.loadingOlder = false;
        updateLoadOlderButton();
      } else {
        state.loadingMessages = false;
      }
    }
  }

  async function startBackgroundHistoryLoad(chatId) {
    const numericChatId = toInt(chatId);
    if (!numericChatId || state.lockedByError) return;
    if (state.autoHistoryLoading && state.autoHistoryChatId === numericChatId) return;
    state.autoHistoryChatId = numericChatId;
    state.autoHistoryLoading = true;
    updateLoadOlderButton();
    let pages = 0;
    try {
      while (
        state.currentChatId === numericChatId &&
        state.hasMore &&
        state.oldestMsgId &&
        !state.lockedByError &&
        pages < AUTO_HISTORY_MAX_PAGES
      ) {
        const before = state.oldestMsgId;
        await fetchMessages(numericChatId, { beforeMsgId: before, silent: true });
        pages += 1;
        if (state.currentChatId !== numericChatId || state.oldestMsgId === before) break;
        await sleep(AUTO_HISTORY_PAUSE_MS);
      }
    } finally {
      if (state.autoHistoryChatId === numericChatId) {
        state.autoHistoryLoading = false;
        state.autoHistoryChatId = null;
        updateLoadOlderButton();
      }
    }
  }

  async function selectChat(chatId) {
    const numericChatId = toInt(chatId);
    if (!numericChatId || state.lockedByError) return;
    if (state.currentChatId === numericChatId && state.messages.length) {
      if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
        setView("chat");
      }
      startBackgroundHistoryLoad(numericChatId);
      return;
    }
    await fetchMessages(numericChatId);
  }

  async function loadHistoryForItem(itemId) {
    const numericId = toInt(itemId);
    if (!numericId || !state.currentChatId) return;
    try {
      const data = await api("/ai/thread/history", {
        item_id: numericId,
        chat_id: state.currentChatId,
      });
      const events = Array.isArray(data.events) ? data.events : [];
      openHistoryModal(`История #${numericId}`, events);
      renderInfoPanel();
    } catch (err) {
      toast(err.message || "Не удалось загрузить историю.", "error");
    }
  }

  async function logoutSession() {
    try {
      const data = await api("/ai/session/logout", {});
      toast(data.message || "Сессия завершена.");
      state.sessionActive = false;
      state.watcherActive = false;
      updateAppSubtitle();
      renderChatHeader();
      renderInfoPanel();
    } catch (err) {
      toast(err.message || "Ошибка завершения сессии.", "error");
    }
  }

  function bindEvents() {
    if (el.dialogSearchInput) {
      el.dialogSearchInput.addEventListener("input", () => {
        state.searchText = String(el.dialogSearchInput.value || "").trim();
        if (state.searchTimer) clearTimeout(state.searchTimer);
        state.searchTimer = setTimeout(() => fetchDialogs(), 280);
      });
    }

    if (el.dialogList) {
      el.dialogList.addEventListener("click", (event) => {
        const button = event.target.closest(".dialog-item");
        if (!button) return;
        const chatId = toInt(button.dataset.chatId);
        if (!chatId) return;
        selectChat(chatId).catch(() => {});
        renderDialogs();
      });
    }

    if (el.messageFilters) {
      el.messageFilters.addEventListener("click", (event) => {
        const chip = event.target.closest(".chip[data-filter]");
        if (!chip) return;
        const filter = String(chip.dataset.filter || "all");
        state.filter = filter;
        document.querySelectorAll(".chip[data-filter]").forEach((node) => {
          node.classList.toggle("is-active", node === chip);
        });
        renderTimeline();
      });
    }

    if (el.timeline) {
      el.timeline.addEventListener("click", (event) => {
        const historyBtn = event.target.closest(".js-history-link");
        if (historyBtn) {
          loadHistoryForItem(historyBtn.dataset.itemId).catch(() => {});
          return;
        }
        const mediaBtn = event.target.closest(".js-media-load");
        if (mediaBtn) {
          const host = mediaBtn.closest(".js-media-preview");
          loadMediaPreview(toInt(mediaBtn.dataset.itemId), host).catch(() => {});
          return;
        }
        const retryBtn = event.target.closest(".js-media-retry");
        if (retryBtn) {
          const host = retryBtn.closest(".js-media-preview");
          loadMediaPreview(toInt(retryBtn.dataset.itemId), host).catch(() => {});
        }
      });
    }

    if (el.infoMediaList) {
      el.infoMediaList.addEventListener("click", (event) => {
        const btn = event.target.closest(".js-info-media");
        if (!btn) return;
        const itemId = toInt(btn.dataset.itemId);
        const host = el.timeline ? el.timeline.querySelector(`.js-media-preview[data-item-id="${itemId}"]`) : null;
        if (host) {
          loadMediaPreview(itemId, host).catch(() => {});
          host.scrollIntoView({ block: "center", behavior: "smooth" });
        } else {
          toast("Медиа не загружено в текущей ленте.", "error");
        }
      });
    }

    if (el.infoHistoryList) {
      el.infoHistoryList.addEventListener("click", (event) => {
        const btn = event.target.closest(".js-info-history");
        if (!btn) return;
        loadHistoryForItem(btn.dataset.itemId).catch(() => {});
      });
    }

    if (el.refreshAllBtn) {
      el.refreshAllBtn.addEventListener("click", () => {
        fetchDialogs().catch(() => {});
        if (state.currentChatId) fetchMessages(state.currentChatId, { silent: true }).catch(() => {});
      });
    }

    if (el.chatRefreshBtn) {
      el.chatRefreshBtn.addEventListener("click", () => {
        if (!state.currentChatId) return;
        fetchMessages(state.currentChatId).catch(() => {});
      });
    }

    if (el.loadOlderBtn) {
      el.loadOlderBtn.addEventListener("click", () => {
        if (!state.currentChatId || !state.hasMore || !state.oldestMsgId) return;
        fetchMessages(state.currentChatId, { beforeMsgId: state.oldestMsgId }).catch(() => {});
      });
    }

    if (el.openInfoBtn) el.openInfoBtn.addEventListener("click", () => toggleInfoPanel(true));
    if (el.chatInfoBtn) el.chatInfoBtn.addEventListener("click", () => toggleInfoPanel(true));
    if (el.closeInfoBtn) el.closeInfoBtn.addEventListener("click", () => toggleInfoPanel(false));
    if (el.panelBackdrop) {
      el.panelBackdrop.addEventListener("click", () => {
        toggleInfoPanel(false);
        toggleThemeSheet(false);
      });
    }

    if (el.mobileBackBtn) {
      el.mobileBackBtn.addEventListener("click", () => setView("dialogs"));
    }

    if (el.themeOpenBtn) {
      el.themeOpenBtn.addEventListener("click", () => {
        toggleThemeSheet(true);
        renderThemeOptions();
      });
    }
    if (el.themeCloseBtn) el.themeCloseBtn.addEventListener("click", () => toggleThemeSheet(false));
    document.querySelectorAll(".theme-option[data-theme-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        setThemeMode(String(btn.dataset.themeMode || "auto"));
        toggleThemeSheet(false);
      });
    });

    if (el.historyCloseBtn) {
      const onClose = (event) => {
        if (event) {
          event.preventDefault();
          event.stopPropagation();
        }
        closeHistoryModal();
      };
      el.historyCloseBtn.addEventListener("pointerdown", onClose, { passive: false });
      el.historyCloseBtn.addEventListener("click", onClose);
      el.historyCloseBtn.addEventListener("touchend", onClose, { passive: false });
    }
    if (el.historyModal) {
      el.historyModal.addEventListener("click", (event) => {
        if (event.target === el.historyModal) closeHistoryModal();
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && el.historyModal && !el.historyModal.hidden) {
        closeHistoryModal();
      }
    });
    document.addEventListener("click", (event) => {
      const target = event.target;
      if (target && target.id === "historyCloseBtn") {
        event.preventDefault();
        event.stopPropagation();
        closeHistoryModal();
      }
    });

    if (el.logoutBtn) el.logoutBtn.addEventListener("click", () => logoutSession().catch(() => {}));
  }

  function startPolling() {
    if (state.pollTimer || state.lockedByError) return;
    state.pollTimer = setInterval(() => {
      fetchDialogs({ silent: true }).catch(() => {});
      if (state.currentChatId) {
        fetchMessages(state.currentChatId, { silent: true }).catch(() => {});
      }
    }, POLL_MS);
  }

  function stopPolling() {
    if (!state.pollTimer) return;
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function initTelegram() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      if (typeof tg.onEvent === "function") {
        tg.onEvent("themeChanged", () => {
          if (state.themeMode === "auto") setThemeMode("auto");
        });
      }
    } catch (_) {
      // ignore
    }
  }

  async function init() {
    initTelegram();
    state.identity = buildIdentity();
    
    // CRITICAL: Verify that we have valid identity before proceeding
    if (!state.identity || (typeof state.identity !== 'object') || Object.keys(state.identity).length === 0) {
      throw new Error(
        "❌ Mini App не смог получить аутентификацию Telegram. " +
        "Убедитесь что:\n" +
        "1. Приложение открывается через кнопку 'Открыть архив' в Telegram боте\n" +
        "2. window.Telegram.WebApp доступен (не открайте через браузер напрямую)\n" +
        "3. Бот корректно настроен с Mini App URL"
      );
    }
    
    loadCache();

    const savedTheme = String(localStorage.getItem(THEME_KEY) || "auto");
    setThemeMode(savedTheme);
    bindEvents();

    applyProfile(state.profile);
    renderDialogs();
    renderChatHeader();
    renderTimeline();
    renderInfoPanel();
    updateAppSubtitle();

    try {
      console.log("[Mini App] Загружаю диалоги...");
      await fetchDialogs();
      console.log("[Mini App] ✅ Диалоги успешно загружены");
    } catch (err) {
      console.error("[Mini App] ❌ Ошибка загрузки диалогов:", err);
      toast("Ошибка: " + (err.message || "Не удалось загрузить диалоги"), "error");
    }
    
    startPolling();
  }

  window.addEventListener("beforeunload", () => {
    stopPolling();
    state.mediaUrlCache.forEach((url) => {
      try {
        URL.revokeObjectURL(url);
      } catch (_) {
        // ignore
      }
    });
    state.dialogAvatarCache.forEach((url) => {
      if (!String(url).startsWith("blob:")) return;
      try {
        URL.revokeObjectURL(url);
      } catch (_) {
        // ignore
      }
    });
    if (state.profileAvatarUrl && state.profileAvatarUrl.startsWith("blob:")) {
      try {
        URL.revokeObjectURL(state.profileAvatarUrl);
      } catch (_) {
        // ignore
      }
    }
  });

  init().catch((err) => {
    setFatalState(err && err.message ? err.message : "Ошибка запуска Mini App.");
  });
})();
