(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let selectedUserId = null;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("登录已过期");
    }
    return response;
  }

  async function errorText(response) {
    try {
      const payload = await response.json();
      return payload.detail || payload.message || `请求失败（HTTP ${response.status}）`;
    } catch (_error) {
      return `请求失败（HTTP ${response.status}）`;
    }
  }

  function formatDate(value) {
    if (!value) return "从未";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "时间未知" : new Intl.DateTimeFormat("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(date);
  }

  function showNotice(message, type = "success") {
    const notice = $("adminNotice");
    notice.textContent = message;
    notice.className = `admin-notice${type === "error" ? " error" : ""}`;
    notice.hidden = false;
    window.setTimeout(() => { notice.hidden = true; }, 6500);
  }

  function showCredentials(username, password, label = "初始密码") {
    const notice = $("credentialNotice");
    notice.innerHTML = `<strong>${escapeHtml(username)}</strong> 已创建。${escapeHtml(label)}：<code>${escapeHtml(password)}</code><br><small>请只通过安全方式交给客户；关闭或刷新页面后不会再次显示。</small>`;
    notice.hidden = false;
  }

  function renderOverview(data) {
    $("customerMetric").textContent = `${data.customerCount || 0} 人`;
    $("customerMetricMeta").textContent = `${data.enabledCustomerCount || 0} 人已启用`;
    $("accountMetric").textContent = `${data.accountCount || 0} 个`;
    $("runningMetric").textContent = `${data.runningCount || 0} 个`;
    const last = data.lastRun;
    $("lastRunMetric").textContent = !last ? "尚未运行" : last.exitCode === 0 ? "成功" : "失败";
    $("lastRunMetricMeta").textContent = last ? formatDate(last.finishedAt || last.startedAt) : "暂无记录";
  }

  function renderUsers(users) {
    const rows = $("customerRows");
    const customers = users.filter((user) => user.role !== "platform_admin");
    $("customerTableMeta").textContent = `${customers.length} 个客户`;
    if (!customers.length) {
      rows.innerHTML = '<tr><td colspan="6" class="table-empty">还没有普通客户，请先创建。</td></tr>';
      return;
    }
    rows.innerHTML = customers.map((user) => {
      const lastRun = user.lastRun;
      return `<tr>
        <td><strong>${escapeHtml(user.displayName)}</strong><span>${escapeHtml(user.username)}</span></td>
        <td><span class="state-badge${user.enabled ? "" : " disabled"}">${user.enabled ? "已启用" : "已禁用"}</span></td>
        <td>${user.accountCount || 0} / ${user.maxAccounts || "—"}</td>
        <td>${escapeHtml(formatDate(user.lastLoginAt))}</td>
        <td>${lastRun ? `${lastRun.status === "running" ? "运行中" : lastRun.exitCode === 0 ? "成功" : "失败"} · ${escapeHtml(formatDate(lastRun.finishedAt || lastRun.startedAt))}` : "尚未运行"}</td>
        <td><div class="table-actions">
          <button class="table-action" data-action="view" data-id="${user.id}">查看</button>
          <button class="table-action" data-action="toggle" data-id="${user.id}" data-enabled="${user.enabled}">${user.enabled ? "禁用" : "启用"}</button>
          <button class="table-action" data-action="reset" data-id="${user.id}">重置密码</button>
          <button class="table-action danger" data-action="delete" data-id="${user.id}">删除</button>
        </div></td>
      </tr>`;
    }).join("");
  }

  function renderAccounts(accounts) {
    const rows = $("accountRows");
    $("accountTableMeta").textContent = `${accounts.length} 个账号`;
    if (!accounts.length) {
      rows.innerHTML = '<tr><td colspan="5" class="table-empty">还没有客户账号。</td></tr>';
      return;
    }
    rows.innerHTML = accounts.map((account) => `<tr>
      <td><strong>${escapeHtml(account.username)}</strong><span>抖音号 ${escapeHtml(account.unique_id)}</span></td>
      <td>${escapeHtml(account.ownerDisplayName || account.ownerUsername || "未知客户")}</td>
      <td>${account.targets?.length || 0} 位</td>
      <td><span class="state-badge">已保存（内容隐藏）</span></td>
      <td>${escapeHtml(formatDate(account.cookieUpdatedAt))}</td>
    </tr>`).join("");
  }

  function renderAudit(events) {
    const rows = $("auditRows");
    if (!events.length) {
      rows.innerHTML = '<tr><td colspan="4" class="table-empty">暂无管理操作。</td></tr>';
      return;
    }
    rows.innerHTML = events.slice(0, 30).map((event) => `<tr>
      <td>${escapeHtml(formatDate(event.createdAt))}</td>
      <td>${escapeHtml(event.username)}</td>
      <td>${escapeHtml(event.action)}</td>
      <td>${escapeHtml(event.detail || "—")}</td>
    </tr>`).join("");
  }

  function setupAdminNavigation() {
    const links = Array.from(document.querySelectorAll("[data-admin-nav]"));
    const setActive = (sectionId) => {
      links.forEach((link) => {
        link.classList.toggle("is-active", link.getAttribute("href") === `#${sectionId}`);
      });
    };

    links.forEach((link) => {
      link.addEventListener("click", () => {
        const target = document.getElementById(link.getAttribute("href")?.slice(1));
        if (target instanceof HTMLDetailsElement) target.open = true;
        if (target) setActive(target.id);
      });
    });

    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver((entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
        if (visible) setActive(visible.target.id);
      }, { rootMargin: "-18% 0px -68% 0px", threshold: [0, 0.25, 0.6] });
      document.querySelectorAll(".admin-anchor-section").forEach((section) => observer.observe(section));
    }
  }

  async function loadAll() {
    const [sessionResponse, overviewResponse, usersResponse, accountsResponse, auditResponse] = await Promise.all([
      api("/api/auth/session"),
      api("/api/admin/overview"), api("/api/admin/users"), api("/api/admin/accounts"), api("/api/admin/audit"),
    ]);
    for (const response of [sessionResponse, overviewResponse, usersResponse, accountsResponse, auditResponse]) {
      if (!response.ok) throw new Error(await errorText(response));
    }
    const session = await sessionResponse.json();
    if (session.user) $("adminIdentity").textContent = session.user.displayName || session.user.username;
    const overview = await overviewResponse.json();
    const users = (await usersResponse.json()).users || [];
    const accounts = (await accountsResponse.json()).accounts || [];
    const events = (await auditResponse.json()).events || [];
    renderOverview(overview);
    renderUsers(users);
    renderAccounts(accounts);
    renderAudit(events);
  }

  async function userState(userId) {
    const response = await api(`/api/admin/users/${userId}/state`);
    if (!response.ok) throw new Error(await errorText(response));
    return response.json();
  }

  async function openCustomer(userId) {
    selectedUserId = Number(userId);
    const state = await userState(selectedUserId);
    const accounts = state.config?.accounts || [];
    const status = state.status || {};
    const schedule = state.config?.settings?.schedule || {};
    $("customerDialogTitle").textContent = "客户详情";
    $("customerDialogBody").innerHTML = `
      <div class="customer-summary">
        <div class="customer-summary-item"><small>账号</small><strong>${accounts.length}</strong></div>
        <div class="customer-summary-item"><small>目标好友</small><strong>${accounts.reduce((sum, account) => sum + (account.targets?.length || 0), 0)}</strong></div>
        <div class="customer-summary-item"><small>任务状态</small><strong>${status.running ? "运行中" : status.exitCode === 0 ? "最近成功" : status.startedAt ? "最近失败" : "尚未运行"}</strong></div>
      </div>
      <p class="field-help">定时：${schedule.enabled ? `${escapeHtml(schedule.time)} · ${escapeHtml(schedule.timezone)}` : "未启用"}</p>
      <ul class="dialog-account-list">${accounts.length ? accounts.map((account) => `<li><strong>${escapeHtml(account.username)}</strong><span>${escapeHtml(account.unique_id)} · ${account.targets?.length || 0} 位目标 · Cookie 内容隐藏</span></li>`).join("") : "<li><span>该客户还没有抖音账号。</span></li>"}</ul>`;
    $("customerDialog").showModal();
  }

  async function handleUserAction(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const userId = Number(button.dataset.id);
    const action = button.dataset.action;
    try {
      if (action === "view") {
        await openCustomer(userId);
        return;
      }
      if (action === "toggle") {
        const enabled = button.dataset.enabled !== "true";
        const response = await api(`/api/admin/users/${userId}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
        if (!response.ok) throw new Error(await errorText(response));
        showNotice(enabled ? "客户已启用" : "客户已禁用");
      } else if (action === "reset") {
        if (!window.confirm("重置后客户现有登录会话会立即失效，确定继续吗？")) return;
        const response = await api(`/api/admin/users/${userId}/reset-password`, { method: "POST" });
        if (!response.ok) throw new Error(await errorText(response));
        const result = await response.json();
        showCredentials(`客户 ${userId}`, result.temporaryPassword, "新临时密码");
      } else if (action === "delete") {
        if (!window.confirm("删除客户会同时删除其账号、目标和运行记录，确定继续吗？")) return;
        const response = await api(`/api/admin/users/${userId}`, { method: "DELETE" });
        if (!response.ok) throw new Error(await errorText(response));
        showNotice("客户及其数据已删除");
      }
      await loadAll();
    } catch (error) {
      showNotice(error.message || "操作失败", "error");
    }
  }

  $("createCustomerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const response = await api("/api/admin/users", {
        method: "POST",
        body: JSON.stringify({
          username: $("customerUsername").value.trim(),
          displayName: $("customerDisplayName").value.trim(),
          password: $("customerPassword").value,
          maxAccounts: Number($("customerMaxAccounts").value),
          maxTargets: Number($("customerMaxTargets").value),
        }),
      });
      if (!response.ok) throw new Error(await errorText(response));
      const result = await response.json();
      if (result.temporaryPassword) showCredentials(result.user.username, result.temporaryPassword);
      else showNotice(`客户 ${result.user.username} 已创建`);
      event.target.reset();
      $("customerMaxAccounts").value = 3;
      $("customerMaxTargets").value = 50;
      await loadAll();
    } catch (error) {
      showNotice(error.message || "创建失败", "error");
    } finally {
      button.disabled = false;
    }
  });

  $("customerRows").addEventListener("click", handleUserAction);
  $("adminRefresh").addEventListener("click", async () => {
    try { await loadAll(); showNotice("状态已刷新"); } catch (error) { showNotice(error.message || "刷新失败", "error"); }
  });
  $("customerDialogRun").addEventListener("click", async () => {
    if (!selectedUserId) return;
    try {
      const response = await api(`/api/admin/users/${selectedUserId}/run`, { method: "POST" });
      if (!response.ok) throw new Error(await errorText(response));
      showNotice("客户任务已启动");
      $("customerDialog").close();
      await loadAll();
    } catch (error) { showNotice(error.message || "任务启动失败", "error"); }
  });
  $("customerDialogClose").addEventListener("click", () => $("customerDialog").close());
  $("customerDialogCloseBottom").addEventListener("click", () => $("customerDialog").close());
  $("adminLogout").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    window.location.assign("/login");
  });

  setupAdminNavigation();
  loadAll().catch((error) => showNotice(error.message || "管理数据读取失败", "error"));
})();
