(() => {
  "use strict";

  const form = document.getElementById("loginForm");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const passwordToggle = document.getElementById("passwordToggle");
  const button = document.getElementById("loginButton");
  const buttonLabel = document.getElementById("loginButtonLabel");
  const buttonSpinner = document.getElementById("loginButtonSpinner");
  const buttonArrow = document.getElementById("loginButtonArrow");
  const error = document.getElementById("loginError");
  const errorText = document.getElementById("loginErrorText");

  function showError(message) {
    errorText.textContent = message;
    error.hidden = false;
    password.classList.add("invalid");
  }

  function clearError() {
    error.hidden = true;
    errorText.textContent = "";
    username.classList.remove("invalid");
    password.classList.remove("invalid");
  }

  function setBusy(busy) {
    button.disabled = busy;
    button.classList.toggle("loading", busy);
    buttonLabel.textContent = busy ? "正在验证…" : "进入控制台";
    buttonSpinner.hidden = !busy;
    buttonArrow.hidden = busy;
  }

  async function readError(response) {
    try {
      const payload = await response.json();
      if (response.status === 429) return "尝试次数过多，请稍后再试";
      return payload.detail || payload.message || `登录失败（HTTP ${response.status}）`;
    } catch (_error) {
      return `登录失败（HTTP ${response.status}）`;
    }
  }

  passwordToggle.addEventListener("click", () => {
    const showing = password.type === "text";
    password.type = showing ? "password" : "text";
    passwordToggle.textContent = showing ? "显示" : "隐藏";
    passwordToggle.setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
    passwordToggle.setAttribute("aria-pressed", String(!showing));
    password.focus();
  });

  username.addEventListener("input", clearError);
  password.addEventListener("input", clearError);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();

    const usernameValue = username.value.trim();
    if (!usernameValue) {
      username.classList.add("invalid");
      showError("请输入用户名");
      username.focus();
      return;
    }
    if (!password.value) {
      password.classList.add("invalid");
      showError("请输入密码");
      password.focus();
      return;
    }

    setBusy(true);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          username: usernameValue,
          password: password.value,
        }),
      });
      if (!response.ok) throw new Error(await readError(response));
      const result = await response.json();
      buttonLabel.textContent = "登录成功，正在进入…";
      window.location.assign(result.user?.role === "platform_admin" ? "/admin" : "/");
    } catch (loginError) {
      setBusy(false);
      showError(loginError.message || "登录失败，请稍后重试");
      password.select();
    }
  });
})();
