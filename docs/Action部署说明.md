# GitHub Actions 运行说明

Prometheus Relay 内置 `.github/workflows/manual-run.yml`，默认只允许手动触发，不会在新仓库中未经确认就自动运行。长期使用更推荐 VPS Docker 调度器。

## 准备 Environment

在 GitHub 仓库中进入 `Settings` → `Environments`，创建名为 `user-data` 的 Environment。

然后分别添加：

- Variables：`PROXY_ADDRESS`、`MESSAGE_TEMPLATE`、`HITOKOTO_TYPES`、`MATCH_MODE`、`BROWSER_TIMEOUT`、`FRIEND_LIST_WAIT_TIME`、`TASK_RETRY_TIMES`、`LOG_LEVEL` 和 `TASKS`
- Secrets：每个账号对应一个 `COOKIES_<抖音号>`

仓库内的 `docs/index.html` 是旧式纯前端环境变量生成器，可在本地打开辅助生成这些内容。Cookie 只在当前浏览器页面中处理，不要将生成器部署到不受信任的网站。

## 手动运行

进入仓库 `Actions` → `Prometheus Relay Manual Run` → `Run workflow`。运行结束后可下载保留 7 天的日志 artifact。

## 可选定时触发

若确定使用 GitHub Actions 定时运行，可在 `manual-run.yml` 的 `on` 下自行加入 `schedule`：

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "0 1 * * *"
```

GitHub Actions 的 cron 使用 UTC；示例为每天 UTC 01:00。定时工作流可能延迟，且平台规则、网络位置和 GitHub Actions 限制都可能影响稳定性。
