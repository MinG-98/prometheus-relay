# GitHub Actions 运行说明

Prometheus Relay 不再提供通过 GitHub Actions 执行真实账号任务的工作流。

抖音 Cookie 属于完整登录凭证。将所有仓库 Secrets 动态导出到公共仓库的 runner 文件，会扩大凭证出现在临时文件、调试输出或第三方 Action 中的风险；GitHub 托管 runner 的网络位置和执行时机也不适合长期维持账号会话。

请使用 [Docker 部署说明](docker-deployment.md) 在自己的 VPS 上运行：

- 扫码会话仅存在于 VPS 的 Web 进程内存；
- 登录成功后的 Cookie 仅写入 VPS 私有数据卷；
- 每日任务由 VPS 本机调度器执行；
- GitHub Actions 只用于不接触真实账号凭证的测试与安全扫描。

旧版纯前端环境变量生成器仅为源码模式兼容保留，不应部署到公共网站，也不要把生成结果提交到仓库。
