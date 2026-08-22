(() => {
  "use strict";

  const HITOKOTO_TYPES = [
    "动画", "漫画", "游戏", "文学", "原创", "来自网络",
    "其他", "影视", "诗词", "哲学", "抖机灵",
  ];
  const TIMEZONE_LABELS = {
    "Asia/Shanghai": "北京时间",
    "America/New_York": "美国东部时间",
    "America/Los_Angeles": "美国西部时间",
    UTC: "协调世界时",
  };
  const ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
  const MAX_COOKIE_FILE_SIZE = 2 * 1024 * 1024;
  const POLL_INTERVAL = 7000;

  const $ = (id) => document.getElementById(id);
  const basePath = location.pathname.endsWith("/") ? location.pathname : `${location.pathname}/`;

  let appState = null;
  let baselineFingerprint = "";
  let dirty = false;
  let busyAction = null;
  let actionError = "";
  let remoteConfigChanged = false;
  let requestInFlight = false;
  let logText = "";
  let toastTimer = null;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function configFingerprint(config) {
    return JSON.stringify(config || {});
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(`${basePath}${path}`, {
      cache: "no-store",
      credentials: "same-origin",
      ...options,
      headers,
    });
  }

  async function responseError(response) {
    try {
      const payload = await response.json();
      if (typeof payload === "string") return payload;
      return payload.detail || payload.message || `请求失败（HTTP ${response.status}）`;
    } catch (_error) {
      return `请求失败（HTTP ${response.status}）`;
    }
  }

  function showToast(message, type = "success") {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast visible ${type}`;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      toast.className = "toast";
    }, type === "error" ? 6500 : 4200);
  }

  function setActionError(message = "") {
    actionError = message;
    updateActionState();
  }

  function setDirty(value = true) {
    dirty = value;
    if (value) actionError = "";
    updateActionState();
    renderRuntime();
  }

  function parseTargets(value) {
    return [...new Set(
      String(value || "")
        .split(/[\n,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    )];
  }

  function formCounts() {
    const cards = [...document.querySelectorAll(".account-card")];
    if (!cards.length && appState?.config?.accounts?.length) {
      const accounts = appState.config.accounts;
      return {
        accounts: accounts.length,
        targets: accounts.reduce((total, account) => total + (account.targets || []).length, 0),
      };
    }
    return {
      accounts: cards.length,
      targets: cards.reduce((total, card) => {
        const targetField = card.querySelector(".targets");
        return total + parseTargets(targetField?.value).length;
      }, 0),
    };
  }

  function formatDateTime(value) {
    if (!value) return "时间未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(date);
  }

  function formatDuration(startedAt, finishedAt = null) {
    if (!startedAt) return "";
    const start = new Date(startedAt).getTime();
    const finish = finishedAt ? new Date(finishedAt).getTime() : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) return "";
    const seconds = Math.max(0, Math.round((finish - start) / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
    const hours = Math.floor(minutes / 60);
    return `${hours} 小时 ${minutes % 60} 分`;
  }

  function triggerLabel(trigger) {
    return ({ manual: "手动", schedule: "定时", system: "系统" })[trigger] || "任务";
  }

  function timezoneLabel(timezone) {
    return TIMEZONE_LABELS[timezone] || timezone || "未设置时区";
  }

  function nextScheduleLabel(time, timezone) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time || "")) return "时间待修正";
    try {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: timezone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }).formatToParts(new Date());
      const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
      const currentMinutes = Number(values.hour) * 60 + Number(values.minute);
      const [hour, minute] = time.split(":").map(Number);
      return `${currentMinutes < hour * 60 + minute ? "今天" : "明天"} ${time}`;
    } catch (_error) {
      return `每天 ${time}`;
    }
  }

  function schedulerHealth() {
    const scheduler = appState?.scheduler || {};
    if (scheduler.state === "running") return { online: true, label: "调度器正在执行" };
    const heartbeat = new Date(scheduler.heartbeatAt || "").getTime();
    if (Number.isFinite(heartbeat) && Date.now() - heartbeat < 180000) {
      return { online: true, label: "调度器在线" };
    }
    if (scheduler.heartbeatAt) return { online: false, label: "调度器心跳已中断" };
    return { online: false, label: "等待调度器心跳" };
  }

  function currentSchedule() {
    if ($("scheduleEnabled")) {
      return {
        enabled: $("scheduleEnabled").checked,
        time: $("scheduleTime").value.trim(),
        timezone: $("scheduleTimezone").value.trim(),
      };
    }
    return {
      enabled: false,
      time: "09:00",
      timezone: "Asia/Shanghai",
      ...(appState?.config?.settings?.schedule || {}),
    };
  }

  function latestCompletedRun() {
    const history = (appState?.history || []).filter((item) => item && !item.running);
    if (history.length) return history[history.length - 1];
    const status = appState?.status || {};
    return !status.running && status.startedAt ? status : null;
  }

  function renderRuntime() {
    if (!appState) return;

    const status = appState.status || {};
    const running = Boolean(status.running);
    const counts = formCounts();
    const schedule = currentSchedule();
    const health = schedulerHealth();
    const latest = latestCompletedRun();

    if (running) {
      $("taskMetric").textContent = "正在运行";
      $("taskMetricMeta").textContent = `${triggerLabel(status.trigger)}启动 · 已运行 ${formatDuration(status.startedAt) || "片刻"}`;
    } else {
      $("taskMetric").textContent = "空闲";
      $("taskMetricMeta").textContent = latest
        ? `上次结束于 ${formatDateTime(latest.finishedAt)}`
        : "可以启动第一次任务";
    }

    if (schedule.enabled) {
      $("scheduleMetric").textContent = nextScheduleLabel(schedule.time, schedule.timezone);
      $("scheduleMetricMeta").textContent = `${timezoneLabel(schedule.timezone)} · ${health.label}${dirty ? " · 待保存" : ""}`;
    } else {
      $("scheduleMetric").textContent = "未启用";
      $("scheduleMetricMeta").textContent = `${health.label} · 可在下方开启`;
    }

    $("accountsMetric").textContent = `${counts.accounts} 个账号`;
    $("accountsMetricMeta").textContent = counts.targets ? `共 ${counts.targets} 位目标好友` : "尚未设置目标好友";

    if (!latest) {
      $("lastRunMetric").textContent = "尚未运行";
      $("lastRunMetricMeta").textContent = "保存配置后即可执行";
    } else if (latest.exitCode === 0) {
      $("lastRunMetric").textContent = "运行成功";
      $("lastRunMetricMeta").textContent = `${formatDateTime(latest.finishedAt)} · ${formatDuration(latest.startedAt, latest.finishedAt) || "已完成"}`;
    } else {
      $("lastRunMetric").textContent = "运行失败";
      $("lastRunMetricMeta").textContent = `${formatDateTime(latest.finishedAt)} · 退出码 ${latest.exitCode ?? "未知"}`;
    }

    const headerStatus = $("headerStatus");
    if (running) {
      headerStatus.className = "status-pill running";
      headerStatus.lastElementChild.textContent = "任务运行中";
    } else if (latest && latest.exitCode !== 0) {
      headerStatus.className = "status-pill error";
      headerStatus.lastElementChild.textContent = "上次运行失败";
    } else {
      headerStatus.className = "status-pill success";
      headerStatus.lastElementChild.textContent = "服务正常";
    }

    renderHistory();
    updateScheduleSummary();
    updateActionState();
  }

  function renderHistory() {
    if (!appState) return;
    const records = [...(appState.history || [])].filter((item) => item && item.startedAt);
    const status = appState.status || {};

    if (status.running) {
      records.push(status);
    } else if (status.startedAt && !records.some((item) => item.startedAt === status.startedAt)) {
      records.push(status);
    }

    const newest = records.reverse().slice(0, 8);
    if (!newest.length) {
      $("runHistory").innerHTML = '<div class="empty-compact">尚无运行记录</div>';
      return;
    }

    $("runHistory").innerHTML = newest.map((record) => {
      const running = Boolean(record.running);
      const succeeded = !running && record.exitCode === 0;
      const stateClass = running ? "running" : succeeded ? "success" : "error";
      const result = running ? "运行中" : succeeded ? "成功" : "失败";
      const time = running ? record.startedAt : record.finishedAt || record.startedAt;
      const duration = formatDuration(record.startedAt, running ? null : record.finishedAt);
      const scope = Number.isFinite(Number(record.targetCount))
        ? `${record.accountCount || 0} 个账号 · ${record.targetCount || 0} 位好友`
        : `${triggerLabel(record.trigger)}任务`;
      return `
        <div class="history-item ${stateClass}">
          <span class="history-dot" aria-hidden="true"></span>
          <div>
            <div class="history-main">
              <strong>${triggerLabel(record.trigger)}运行${result}</strong>
              <time datetime="${escapeHtml(time || "")}">${escapeHtml(formatDateTime(time))}</time>
            </div>
            <div class="history-meta">
              <span>${escapeHtml(scope)}</span>
              <span>${escapeHtml(duration || (running ? "刚刚启动" : `退出码 ${record.exitCode ?? "未知"}`))}</span>
            </div>
          </div>
        </div>`;
    }).join("");
  }

  function renderSettings() {
    const settings = appState?.config?.settings || {};
    const schedule = {
      enabled: false,
      time: "09:00",
      timezone: "Asia/Shanghai",
      ...(settings.schedule || {}),
    };

    $("messageTemplate").value = settings.messageTemplate ?? "续火花";
    $("scheduleEnabled").checked = Boolean(schedule.enabled);
    $("scheduleTime").value = schedule.time;
    $("scheduleTimezone").value = schedule.timezone;
    $("matchMode").value = settings.matchMode || "short_id";
    $("logLevel").value = settings.logLevel || "Info";
    $("proxyAddress").value = settings.proxyAddress || "";
    $("browserTimeout").value = formatSeconds(settings.browserTimeout, 120);
    $("friendListTimeout").value = formatSeconds(settings.friendListTimeout, 2);
    $("taskRetryTimes").value = settings.taskRetryTimes ?? 3;

    const selectedTypes = new Set(settings.hitokotoTypes || []);
    $("hitokotoTypes").innerHTML = HITOKOTO_TYPES.map((type, index) => `
      <label class="check-chip" for="hitokoto-${index}">
        <input id="hitokoto-${index}" type="checkbox" value="${escapeHtml(type)}"${selectedTypes.has(type) ? " checked" : ""}>
        <span>${escapeHtml(type)}</span>
      </label>`).join("");

    toggleScheduleFields();
    updateMessagePreview();
    updateTargetLabels();
  }

  function formatSeconds(milliseconds, fallback) {
    const value = Number(milliseconds);
    if (!Number.isFinite(value)) return fallback;
    return Math.round((value / 1000) * 10) / 10;
  }

  function captureAccountDrafts() {
    return [...document.querySelectorAll(".account-card")].map((card) => ({
      username: card.querySelector(".username")?.value || "",
      uniqueId: card.querySelector(".unique-id")?.value || "",
      targets: card.querySelector(".targets")?.value || "",
      cookies: card.querySelector(".cookies")?.value || "",
      editorOpen: !card.querySelector(".account-form")?.hidden,
      cookieJson: card.dataset.cookieJson || "",
      cookieFileError: card.dataset.cookieFileError || "",
      cookieFilePending: card.dataset.cookieFilePending || "",
      cookieFileStatus: card.querySelector(".cookie-status")?.textContent || "",
      cookieFileStatusType: card.querySelector(".cookie-status")?.dataset.type || "",
    }));
  }

  function renderAccounts(drafts = []) {
    const accounts = appState?.config?.accounts || [];
    if (!accounts.length) {
      $("accounts").innerHTML = `
        <div class="empty-state">
          <div><strong>还没有发送账号</strong><span>添加账号后，填写抖音号、目标好友和 Cookie。</span></div>
        </div>`;
      updateActionState();
      return;
    }

    $("accounts").innerHTML = accounts.map((account, index) => {
      const draft = drafts[index];
      const username = draft?.username ?? account.username ?? "";
      const uniqueId = draft?.uniqueId ?? account.unique_id ?? "";
      const targetText = draft?.targets ?? (account.targets || []).join("\n");
      const targetCount = parseTargets(targetText).length;
      const hasCookies = Boolean(account.hasCookies);
      const editorOpen = draft?.editorOpen ?? !hasCookies;
      const displayName = username || `新账号 ${index + 1}`;
      const avatar = [...displayName][0]?.toUpperCase() || String(index + 1);
      const cookieStatus = draft?.cookieFileStatus
        || (hasCookies
          ? `留空即可保留已保存的 ${account.cookieCount || 0} 条 Cookie`
          : "请选择 Cookie-Editor 导出的 JSON 文件（最大 2 MB）");
      const cookieStatusType = draft?.cookieFileStatusType || (hasCookies ? "success" : "");
      return `
        <article class="account-card${editorOpen ? " editing" : ""}" data-index="${index}" data-original-unique-id="${escapeHtml(account.unique_id || "")}" data-has-cookies="${hasCookies}">
          <div class="account-summary">
            <span class="account-avatar" aria-hidden="true">${escapeHtml(avatar)}</span>
            <div class="account-info">
              <h3 class="account-name">${escapeHtml(displayName)}</h3>
              <div class="account-meta">
                <span class="account-id">${uniqueId ? `抖音号 ${escapeHtml(uniqueId)}` : "抖音号待填写"}</span>
                <span class="account-target-count">${targetCount} 位目标好友</span>
                <span>${hasCookies ? `Cookie ${account.cookieCount || 0} 条` : "Cookie 待配置"}</span>
              </div>
            </div>
            <div class="account-actions">
              <button class="icon-button edit-account" type="button" aria-expanded="${editorOpen}" aria-controls="account-form-${index}">${editorOpen ? "收起" : "编辑"}</button>
              <button class="icon-button danger remove-account" type="button" aria-label="删除账号 ${escapeHtml(displayName)}">删除</button>
            </div>
          </div>
          <div id="account-form-${index}" class="account-form"${editorOpen ? "" : " hidden"}>
            <p class="form-section-title">账号资料</p>
            <div class="form-grid two-columns">
              <div class="field">
                <label for="account-${index}-unique-id">登录账号的抖音号</label>
                <input id="account-${index}-unique-id" class="unique-id" type="text" maxlength="64" value="${escapeHtml(uniqueId)}" placeholder="例如 1234567890" autocomplete="off">
                <p class="field-help">修改已保存的抖音号后，需要重新上传 Cookie。</p>
              </div>
              <div class="field">
                <label for="account-${index}-username">显示名称</label>
                <input id="account-${index}-username" class="username" type="text" maxlength="120" value="${escapeHtml(username)}" placeholder="例如 MinG" autocomplete="off">
                <p class="field-help">仅用于控制台识别，不影响好友匹配。</p>
              </div>
            </div>
            <div class="field">
              <label class="target-label" for="account-${index}-targets">目标好友抖音号</label>
              <textarea id="account-${index}-targets" class="targets" rows="4" placeholder="每行填写一个好友抖音号">${escapeHtml(targetText)}</textarea>
              <p class="target-help field-help">支持换行、英文逗号或中文逗号分隔；重复项会自动去除。</p>
            </div>

            <div class="cookie-area">
              <p class="form-section-title">登录 Cookie</p>
              <div class="cookie-callout"><span aria-hidden="true">●</span><span>Cookie 相当于登录凭证。网页不会回显已保存内容，也不要把文件发送给别人。</span></div>
              <div class="field">
                <label for="account-${index}-cookie-file">上传 Cookie JSON 文件</label>
                <input id="account-${index}-cookie-file" class="cookie-file" type="file" accept=".json,application/json,text/json">
                <span class="cookie-status ${escapeHtml(cookieStatusType)}" data-type="${escapeHtml(cookieStatusType)}">${escapeHtml(cookieStatus)}</span>
              </div>
              <details class="manual-cookie"${draft?.cookies ? " open" : ""}>
                <summary>也可以手动粘贴 Cookie JSON</summary>
                <div class="field">
                  <label class="visually-hidden" for="account-${index}-cookies">Cookie JSON</label>
                  <textarea id="account-${index}-cookies" class="cookies" rows="5" spellcheck="false" placeholder="粘贴 Cookie-Editor 导出的 JSON 数组">${escapeHtml(draft?.cookies || "")}</textarea>
                </div>
              </details>
            </div>
            <div class="account-form-footer">
              <button class="button ghost compact close-account" type="button">收起编辑</button>
            </div>
          </div>
        </article>`;
    }).join("");

    drafts.forEach((draft, index) => {
      const card = document.querySelector(`.account-card[data-index="${index}"]`);
      if (!card) return;
      if (draft.cookieJson) card.dataset.cookieJson = draft.cookieJson;
      if (draft.cookieFileError) card.dataset.cookieFileError = draft.cookieFileError;
      if (draft.cookieFilePending) card.dataset.cookieFilePending = draft.cookieFilePending;
    });

    bindAccountEvents();
    bindCookieInputs();
    updateTargetLabels();
    updateActionState();
  }

  function setEditorOpen(card, open) {
    const form = card.querySelector(".account-form");
    const button = card.querySelector(".edit-account");
    form.hidden = !open;
    card.classList.toggle("editing", open);
    button.setAttribute("aria-expanded", String(open));
    button.textContent = open ? "收起" : "编辑";
  }

  function bindAccountEvents() {
    document.querySelectorAll(".account-card").forEach((card) => {
      card.querySelector(".edit-account").addEventListener("click", () => {
        setEditorOpen(card, card.querySelector(".account-form").hidden);
      });
      card.querySelector(".close-account").addEventListener("click", () => {
        setEditorOpen(card, false);
        card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
      card.querySelector(".remove-account").addEventListener("click", () => removeAccount(card));
    });
  }

  function parseCookieArray(text) {
    let cookies;
    try {
      cookies = JSON.parse(text);
    } catch (_error) {
      throw new Error("不是有效的 JSON 文件");
    }
    if (!Array.isArray(cookies) || cookies.length === 0) {
      throw new Error("Cookie 文件必须是非空 JSON 数组");
    }
    if (cookies.some((cookie) => !cookie || typeof cookie !== "object" || Array.isArray(cookie))) {
      throw new Error("Cookie 数组中的每一项都必须是对象");
    }
    return cookies;
  }

  function bindCookieInputs() {
    document.querySelectorAll(".cookie-file").forEach((input) => {
      input.addEventListener("change", async () => {
        const card = input.closest(".account-card");
        const status = card.querySelector(".cookie-status");
        const file = input.files?.[0];
        delete card.dataset.cookieJson;
        delete card.dataset.cookieFileError;
        status.dataset.type = "";
        status.className = "cookie-status";
        setDirty(true);

        if (!file) {
          status.textContent = card.dataset.hasCookies === "true"
            ? "未选择新文件；保存时会保留原 Cookie"
            : "尚未选择 Cookie 文件";
          return;
        }
        if (file.size > MAX_COOKIE_FILE_SIZE) {
          const message = "文件不能超过 2 MB";
          card.dataset.cookieFileError = message;
          status.textContent = message;
          status.dataset.type = "error";
          status.className = "cookie-status error";
          showToast(message, "error");
          updateActionState();
          return;
        }

        card.dataset.cookieFilePending = "1";
        status.textContent = `正在读取 ${file.name}…`;
        updateActionState();
        try {
          const text = await file.text();
          const cookies = parseCookieArray(text);
          card.dataset.cookieJson = JSON.stringify(cookies);
          const manualInput = card.querySelector(".cookies");
          if (manualInput.value.trim()) manualInput.value = "";
          status.textContent = `已读取 ${file.name}（${cookies.length} 条 Cookie），保存后生效`;
          status.dataset.type = "success";
          status.className = "cookie-status success";
          showToast(`账号 ${Number(card.dataset.index) + 1} 的 Cookie 已读取，请保存更改`);
        } catch (error) {
          const message = error.message || "Cookie 文件无效";
          card.dataset.cookieFileError = message;
          status.textContent = message;
          status.dataset.type = "error";
          status.className = "cookie-status error";
          showToast(message, "error");
        } finally {
          delete card.dataset.cookieFilePending;
          updateActionState();
        }
      });
    });
  }

  function updateAccountSummary(card) {
    const username = card.querySelector(".username").value.trim();
    const uniqueId = card.querySelector(".unique-id").value.trim();
    const index = Number(card.dataset.index);
    const displayName = username || `新账号 ${index + 1}`;
    card.querySelector(".account-name").textContent = displayName;
    card.querySelector(".account-avatar").textContent = [...displayName][0]?.toUpperCase() || String(index + 1);
    card.querySelector(".account-id").textContent = uniqueId ? `抖音号 ${uniqueId}` : "抖音号待填写";
    card.querySelector(".account-target-count").textContent = `${parseTargets(card.querySelector(".targets").value).length} 位目标好友`;
  }

  function updateTargetLabels() {
    const shortIdMode = $("matchMode").value === "short_id";
    document.querySelectorAll(".account-card").forEach((card) => {
      const label = card.querySelector(".target-label");
      const textarea = card.querySelector(".targets");
      const help = card.querySelector(".target-help");
      if (!label || !textarea || !help) return;
      label.textContent = shortIdMode ? "目标好友抖音号" : "目标好友原始昵称";
      textarea.placeholder = shortIdMode ? "每行填写一个好友抖音号" : "每行填写一个好友原始昵称";
      help.textContent = shortIdMode
        ? "支持换行、英文逗号或中文逗号分隔；重复抖音号会自动去除。"
        : "昵称可能重名或变化，只有无法取得抖音号时再使用。";
    });
  }

  function updateMessagePreview() {
    const value = $("messageTemplate").value;
    $("messageCount").textContent = `${value.length} / 2000`;
    $("messagePreview").textContent = value.trim()
      ? value.replaceAll("[API]", "这里会替换成每日一言 —— 来源（作者）")
      : "（发送内容不能为空）";
  }

  function toggleScheduleFields() {
    const enabled = $("scheduleEnabled").checked;
    $("scheduleFields").classList.toggle("disabled", !enabled);
    $("scheduleTime").disabled = !enabled;
    $("scheduleTimezone").disabled = !enabled;
    updateScheduleSummary();
  }

  function updateScheduleSummary() {
    if (!appState || !$("scheduleEnabled")) return;
    const schedule = currentSchedule();
    if (!schedule.enabled) {
      $("scheduleSummary").textContent = "自动运行尚未启用；仍可随时点击“立即运行”。";
      return;
    }
    const next = nextScheduleLabel(schedule.time, schedule.timezone);
    $("scheduleSummary").textContent = `${next}（${timezoneLabel(schedule.timezone)}）执行；保存后生效。`;
  }

  function updateActionState() {
    if (!appState) return;
    const running = Boolean(appState.status?.running);
    const pendingCookie = Boolean(document.querySelector("[data-cookie-file-pending]"));
    const counts = formCounts();
    const saveButton = $("save");
    const runButton = $("run");
    const addButton = $("addAccount");
    const saveState = document.querySelector(".save-state");

    saveButton.disabled = !dirty || running || Boolean(busyAction) || pendingCookie;
    runButton.disabled = dirty || running || Boolean(busyAction) || counts.accounts === 0 || counts.targets === 0;
    addButton.disabled = running || Boolean(busyAction) || counts.accounts >= 20 || pendingCookie;

    saveButton.classList.toggle("loading", busyAction === "save");
    runButton.classList.toggle("loading", busyAction === "run");
    $("refresh").classList.toggle("loading", busyAction === "refresh");
    saveButton.textContent = busyAction === "save" ? "保存中…" : "保存更改";
    runButton.textContent = busyAction === "run" ? "启动中…" : running ? "任务运行中" : "立即运行";

    saveState.className = "save-state";
    if (actionError) {
      saveState.classList.add("error");
      $("saveStateTitle").textContent = "操作未完成";
      $("saveStateHint").textContent = actionError;
    } else if (remoteConfigChanged) {
      saveState.classList.add("error");
      $("saveStateTitle").textContent = "服务器配置已变化";
      $("saveStateHint").textContent = "当前草稿仍保留；重新加载页面后再编辑更安全";
    } else if (dirty) {
      saveState.classList.add("dirty");
      $("saveStateTitle").textContent = "有未保存的更改";
      $("saveStateHint").textContent = pendingCookie ? "Cookie 文件仍在读取" : "保存后才能运行任务";
    } else if (running) {
      $("saveStateTitle").textContent = "任务正在运行";
      $("saveStateHint").textContent = "运行结束后可继续修改配置";
    } else {
      $("saveStateTitle").textContent = "配置已保存";
      $("saveStateHint").textContent = counts.targets ? "可以直接运行任务" : "添加至少一位目标好友后才能运行";
    }

    if (pendingCookie) saveButton.title = "Cookie 文件读取完成后才能保存";
    else if (running) saveButton.title = "任务运行时不能修改配置";
    else if (!dirty) saveButton.title = "当前没有未保存的更改";
    else saveButton.removeAttribute("title");

    if (dirty) runButton.title = "请先保存更改";
    else if (running) runButton.title = "已有任务正在运行";
    else if (!counts.accounts) runButton.title = "请先添加账号";
    else if (!counts.targets) runButton.title = "请先添加目标好友";
    else runButton.removeAttribute("title");
  }

  class FormValidationError extends Error {
    constructor(message, element = null) {
      super(message);
      this.element = element;
    }
  }

  function requireNumber(id, minimum, maximum, label) {
    const element = $(id);
    const value = Number(element.value);
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      throw new FormValidationError(`${label}必须在 ${minimum} 到 ${maximum} 之间`, element);
    }
    return value;
  }

  function collectConfig() {
    const messageTemplate = $("messageTemplate").value;
    if (!messageTemplate.trim()) {
      throw new FormValidationError("发送内容不能为空", $("messageTemplate"));
    }

    const scheduleTime = $("scheduleTime").value.trim();
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(scheduleTime)) {
      throw new FormValidationError("运行时间必须是 24 小时制 HH:MM，例如 07:40", $("scheduleTime"));
    }
    const scheduleTimezone = $("scheduleTimezone").value.trim();
    if (!scheduleTimezone) {
      throw new FormValidationError("请填写定时运行时区", $("scheduleTimezone"));
    }

    const browserTimeout = requireNumber("browserTimeout", 1, 600, "浏览器超时");
    const friendListTimeout = requireNumber("friendListTimeout", 0, 60, "好友加载等待");
    const taskRetryTimes = requireNumber("taskRetryTimes", 1, 10, "失败重试次数");
    if (!Number.isInteger(taskRetryTimes)) {
      throw new FormValidationError("失败重试次数必须是整数", $("taskRetryTimes"));
    }

    const seenIds = new Set();
    const matchMode = $("matchMode").value;
    const accounts = [...document.querySelectorAll(".account-card")].map((card, index) => {
      const uniqueIdInput = card.querySelector(".unique-id");
      const usernameInput = card.querySelector(".username");
      const targetsInput = card.querySelector(".targets");
      const uniqueId = uniqueIdInput.value.trim();
      const username = usernameInput.value.trim();
      const targets = parseTargets(targetsInput.value);

      if (!ID_PATTERN.test(uniqueId)) {
        throw new FormValidationError(`账号 ${index + 1}：抖音号只能包含数字、字母、下划线或短横线`, uniqueIdInput);
      }
      if (seenIds.has(uniqueId)) {
        throw new FormValidationError(`账号 ${index + 1}：抖音号 ${uniqueId} 重复`, uniqueIdInput);
      }
      seenIds.add(uniqueId);
      if (!username) {
        throw new FormValidationError(`账号 ${index + 1}：请填写显示名称`, usernameInput);
      }
      if (matchMode === "short_id") {
        const invalidTarget = targets.find((target) => !ID_PATTERN.test(target));
        if (invalidTarget) {
          throw new FormValidationError(`账号 ${index + 1}：目标抖音号“${invalidTarget}”格式不正确`, targetsInput);
        }
      }

      const account = { unique_id: uniqueId, username, targets };
      const pastedCookies = card.querySelector(".cookies").value.trim();
      if (card.dataset.cookieFilePending) {
        throw new FormValidationError(`账号 ${index + 1}：Cookie 文件仍在读取`, card.querySelector(".cookie-file"));
      }
      if (card.dataset.cookieFileError) {
        throw new FormValidationError(`账号 ${index + 1}：${card.dataset.cookieFileError}`, card.querySelector(".cookie-file"));
      }
      if (card.dataset.cookieJson && pastedCookies) {
        throw new FormValidationError(`账号 ${index + 1}：上传文件和手动粘贴只能选择一种`, card.querySelector(".cookies"));
      }
      if (card.dataset.cookieJson) {
        account.cookies = parseCookieArray(card.dataset.cookieJson);
      } else if (pastedCookies) {
        try {
          account.cookies = parseCookieArray(pastedCookies);
        } catch (error) {
          throw new FormValidationError(`账号 ${index + 1}：${error.message}`, card.querySelector(".cookies"));
        }
      } else {
        const canKeepExisting = card.dataset.hasCookies === "true"
          && card.dataset.originalUniqueId === uniqueId;
        if (!canKeepExisting) {
          throw new FormValidationError(`账号 ${index + 1}：请上传 Cookie JSON 文件`, card.querySelector(".cookie-file"));
        }
      }
      return account;
    });

    return {
      settings: {
        proxyAddress: $("proxyAddress").value.trim(),
        messageTemplate,
        hitokotoTypes: [...$("hitokotoTypes").querySelectorAll("input:checked")].map((input) => input.value),
        matchMode,
        browserTimeout: Math.round(browserTimeout * 1000),
        friendListTimeout: Math.round(friendListTimeout * 1000),
        taskRetryTimes,
        logLevel: $("logLevel").value,
        schedule: {
          enabled: $("scheduleEnabled").checked,
          time: scheduleTime,
          timezone: scheduleTimezone,
        },
      },
      accounts,
    };
  }

  function focusValidationError(error) {
    const element = error.element;
    if (!element) return;
    const details = element.closest("details");
    if (details) details.open = true;
    const card = element.closest(".account-card");
    if (card) setEditorOpen(card, true);
    element.classList.add("invalid");
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => element.focus(), 250);
  }

  function showConfirm({ title, body, confirmText = "确认", danger = false }) {
    const dialog = $("confirmDialog");
    if (typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(`${title}\n\n${body}`));
    }
    $("confirmTitle").textContent = title;
    $("confirmBody").textContent = body;
    $("confirmAction").textContent = confirmText;
    $("confirmAction").className = `button ${danger ? "danger" : "primary"}`;
    dialog.classList.toggle("danger", danger);
    dialog.showModal();

    return new Promise((resolve) => {
      const finish = (result) => {
        dialog.close();
        $("confirmAction").onclick = null;
        $("confirmCancel").onclick = null;
        dialog.oncancel = null;
        resolve(result);
      };
      $("confirmAction").onclick = () => finish(true);
      $("confirmCancel").onclick = () => finish(false);
      dialog.oncancel = (event) => {
        event.preventDefault();
        finish(false);
      };
    });
  }

  async function removeAccount(card) {
    if (card.dataset.cookieFilePending) {
      showToast("Cookie 文件仍在读取，请稍候再删除", "error");
      return;
    }
    const index = Number(card.dataset.index);
    const drafts = captureAccountDrafts();
    const name = drafts[index]?.username || drafts[index]?.uniqueId || `账号 ${index + 1}`;
    const confirmed = await showConfirm({
      title: `删除 ${name}？`,
      body: "保存更改后，这个账号的目标好友和已保存 Cookie 会一起删除。此操作不会删除抖音账号本身。",
      confirmText: "删除账号",
      danger: true,
    });
    if (!confirmed) return;
    drafts.splice(index, 1);
    appState.config.accounts.splice(index, 1);
    renderAccounts(drafts);
    setDirty(true);
    showToast("账号已从当前草稿移除，保存后生效");
  }

  function addAccount() {
    const accounts = appState?.config?.accounts || [];
    if (accounts.length >= 20) {
      showToast("最多支持 20 个账号", "error");
      return;
    }
    if (document.querySelector("[data-cookie-file-pending]")) {
      showToast("Cookie 文件仍在读取，请稍候再添加账号", "error");
      return;
    }
    const drafts = captureAccountDrafts();
    accounts.push({
      unique_id: "",
      username: "",
      targets: [],
      hasCookies: false,
      cookieCount: 0,
    });
    drafts.push({
      username: "",
      uniqueId: "",
      targets: "",
      cookies: "",
      editorOpen: true,
      cookieJson: "",
      cookieFileError: "",
      cookieFilePending: "",
      cookieFileStatus: "请选择 Cookie-Editor 导出的 JSON 文件（最大 2 MB）",
      cookieFileStatusType: "",
    });
    renderAccounts(drafts);
    setDirty(true);
    const card = document.querySelector(".account-card:last-child");
    card?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => card?.querySelector(".unique-id")?.focus(), 300);
    showToast("新账号已加入草稿，填写后请保存更改");
  }

  async function saveConfig() {
    if (!dirty || busyAction || appState?.status?.running) return;
    let payload;
    try {
      payload = collectConfig();
    } catch (error) {
      const message = error.message || "请检查配置内容";
      setActionError(message);
      focusValidationError(error);
      showToast(message, "error");
      return;
    }

    busyAction = "save";
    setActionError("");
    updateActionState();
    try {
      const response = await api("api/config", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json();
      appState.config = result.config;
      baselineFingerprint = configFingerprint(result.config);
      dirty = false;
      remoteConfigChanged = false;
      actionError = "";
      renderSettings();
      renderAccounts();
      renderRuntime();
      showToast("配置已保存，账号已收起为简洁摘要");
    } catch (error) {
      setActionError(error.message || "保存配置失败");
      showToast(error.message || "保存配置失败", "error");
    } finally {
      busyAction = null;
      updateActionState();
    }
  }

  async function runTask() {
    const counts = formCounts();
    if (dirty) {
      showToast("请先保存更改，再运行任务", "error");
      return;
    }
    if (appState?.status?.running || busyAction || !counts.accounts || !counts.targets) return;

    const confirmed = await showConfirm({
      title: "立即运行续火花任务？",
      body: `将使用 ${counts.accounts} 个账号，向共 ${counts.targets} 位目标好友发送当前消息。任务启动后请在运行记录中查看结果。`,
      confirmText: "确认运行",
    });
    if (!confirmed) return;

    busyAction = "run";
    setActionError("");
    updateActionState();
    try {
      const response = await api("api/run", { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response));
      appState.status = {
        running: true,
        startedAt: new Date().toISOString(),
        finishedAt: null,
        exitCode: null,
        trigger: "manual",
        accountCount: counts.accounts,
        targetCount: counts.targets,
      };
      renderRuntime();
      showToast("任务已启动，页面会自动更新运行状态");
      window.setTimeout(() => refreshState(), 900);
    } catch (error) {
      setActionError(error.message || "任务启动失败");
      showToast(error.message || "任务启动失败", "error");
    } finally {
      busyAction = null;
      updateActionState();
    }
  }

  async function refreshLog() {
    try {
      const response = await api("api/log");
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      logText = payload.log || "";
      $("logs").textContent = logText || "暂无日志";
      $("logUpdatedAt").textContent = `更新于 ${new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }).format(new Date())}`;
      $("copyLog").disabled = !logText;
    } catch (_error) {
      $("logUpdatedAt").textContent = "日志读取失败";
    }
  }

  async function refreshState({ notify = false } = {}) {
    if (requestInFlight) return;
    requestInFlight = true;
    try {
      const response = await api("api/state");
      if (!response.ok) throw new Error(await responseError(response));
      const nextState = await response.json();
      const nextFingerprint = configFingerprint(nextState.config);

      if (!appState) {
        appState = nextState;
        baselineFingerprint = nextFingerprint;
        renderSettings();
        renderAccounts();
      } else {
        appState.status = nextState.status || {};
        appState.history = nextState.history || [];
        appState.scheduler = nextState.scheduler || {};
        if (nextFingerprint !== baselineFingerprint) {
          if (dirty) {
            remoteConfigChanged = true;
          } else {
            appState.config = nextState.config;
            baselineFingerprint = nextFingerprint;
            renderSettings();
            renderAccounts();
          }
        }
      }

      renderRuntime();
      await refreshLog();
      if (notify) showToast(dirty ? "状态已刷新，未保存的草稿仍然保留" : "状态已刷新");
    } catch (error) {
      if (!appState) {
        $("headerStatus").className = "status-pill error";
        $("headerStatus").lastElementChild.textContent = "连接失败";
      }
      if (notify || !appState) showToast(error.message || "状态读取失败", "error");
    } finally {
      requestInFlight = false;
    }
  }

  function normaliseTimeField() {
    const input = $("scheduleTime");
    const original = input.value.trim();
    let compact = original.replace("：", ":");
    if (/^\d{3,4}$/.test(compact)) {
      compact = compact.padStart(4, "0");
      compact = `${compact.slice(0, 2)}:${compact.slice(2)}`;
    } else {
      const match = compact.match(/^(\d{1,2}):(\d{2})$/);
      if (match) compact = `${match[1].padStart(2, "0")}:${match[2]}`;
    }
    if (compact !== original) {
      input.value = compact;
      setDirty(true);
    }
    updateScheduleSummary();
  }

  function bindStaticEvents() {
    document.addEventListener("input", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement)) return;
      if (target.type === "file") return;
      target.classList.remove("invalid");
      setDirty(true);
      if (target.id === "messageTemplate") updateMessagePreview();
      if (target.id === "scheduleTime" || target.id === "scheduleTimezone") updateScheduleSummary();
      const card = target.closest(".account-card");
      if (card) updateAccountSummary(card);
    });

    document.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) return;
      if (target.type === "file") return;
      target.classList.remove("invalid");
      setDirty(true);
      if (target.id === "scheduleEnabled") toggleScheduleFields();
      if (target.id === "matchMode") updateTargetLabels();
    });

    $("scheduleTime").addEventListener("blur", normaliseTimeField);
    $("addAccount").addEventListener("click", addAccount);
    $("save").addEventListener("click", saveConfig);
    $("run").addEventListener("click", runTask);
    $("refresh").addEventListener("click", async () => {
      if (busyAction) return;
      busyAction = "refresh";
      updateActionState();
      await refreshState({ notify: true });
      busyAction = null;
      updateActionState();
    });
    $("logDetails").addEventListener("toggle", () => {
      if ($("logDetails").open) refreshLog();
    });
    $("copyLog").addEventListener("click", async () => {
      if (!logText) return;
      try {
        await navigator.clipboard.writeText(logText);
        showToast("日志已复制");
      } catch (_error) {
        showToast("浏览器不允许复制，请在日志框中手动选择", "error");
      }
    });
    window.addEventListener("beforeunload", (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  async function bootstrap() {
    bindStaticEvents();
    await refreshState();
    window.setInterval(() => {
      if (!document.hidden) refreshState();
    }, POLL_INTERVAL);
  }

  bootstrap();
})();
