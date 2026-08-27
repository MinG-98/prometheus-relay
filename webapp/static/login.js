(() => {
  "use strict";

  const form = document.getElementById("loginForm");
  const button = document.getElementById("loginButton");
  const error = document.getElementById("loginError");

  async function readError(response) {
    try {
      const payload = await response.json();
      return payload.detail || payload.message || `登录失败（HTTP ${response.status}）`;
    } catch (_error) {
      return `登录失败（HTTP ${response.status}）`;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    button.textContent = "登录中…";
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("username").value.trim(),
          password: document.getElementById("password").value,
        }),
      });
      if (!response.ok) throw new Error(await readError(response));
      const result = await response.json();
      window.location.assign(result.user?.role === "platform_admin" ? "/admin" : "/");
    } catch (loginError) {
      error.textContent = loginError.message || "登录失败，请稍后重试";
      error.hidden = false;
      document.getElementById("password").select();
    } finally {
      button.disabled = false;
      button.textContent = "登录";
    }
  });
})();
