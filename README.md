# Prometheus Relay

<p align="center">
  <img src="docs/images/prometheus-relay-banner.svg" alt="Prometheus Relay — Keep the fire alive" width="100%">
</p>

<p align="center">
  <strong>Keep the fire alive.</strong><br>
  一套面向个人自托管场景的抖音火花自动维护工具。
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-f59e0b">
</p>

Prometheus Relay 使用 Playwright 驱动无头 Chromium，通过抖音创作者中心模拟正常的网页操作，按计划向指定好友发送续火花消息。它提供一个适合 VPS 长期运行的网页控制台，账号、Cookie、目标好友、消息模板和每日计划均可在浏览器中管理。

## 功能

- 多账号、多目标好友
- 默认按抖音号匹配，也支持昵称匹配
- Cookie JSON 文件上传与安全存储
- 每日定时执行、手动执行和并发锁
- 运行状态、历史记录与日志查看
- Basic Auth 网页访问保护
- Docker Compose、systemd 与 GitHub Actions
- 可选“一言”消息内容

## 快速部署

推荐使用 Docker Compose，并通过 Caddy、Nginx 或其他反向代理提供 HTTPS。

```bash
git clone https://github.com/MinG-98/prometheus-relay.git
cd prometheus-relay

sudo mkdir -p /etc/prometheus-relay
sudo install -m 600 .env.web.example /etc/prometheus-relay/web.env
sudo editor /etc/prometheus-relay/web.env

PROMETHEUS_RELAY_ENV_FILE=/etc/prometheus-relay/web.env \
  docker compose up -d --build web scheduler
```

控制台默认只监听 `127.0.0.1:18081`。详细部署、systemd 托管和升级步骤见 [Docker 部署说明](docs/docker-deployment.md)。

## 使用流程

1. 登录抖音创作者中心。
2. 使用 Cookie-Editor 等工具导出当前账号的 Cookie JSON 文件。
3. 打开 Prometheus Relay 控制台，添加账号并上传 Cookie。
4. 填写目标好友抖音号，保存配置。
5. 先手动运行一次确认可用，再开启每日定时任务。

Cookie 等同于登录凭证。请勿提交到 Git、粘贴到 Issue，或发送给他人。若 Cookie 曾经泄露，请立即在抖音退出相关登录会话并重新登录。

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

启动网页服务：

```bash
AUTH_ENABLED=false PROMETHEUS_RELAY_DATA_DIR=./data \
  uvicorn webapp.main:app --reload
```

## 项目来源

Prometheus Relay 派生自 [2061360308/DouYinSparkFlow](https://github.com/2061360308/DouYinSparkFlow)，并在其 MIT 许可下继续开发。当前项目新增了 VPS 网页控制台、Cookie 文件上传、账号管理、每日调度、运行历史、Docker/systemd 部署和安全加固，并以独立项目持续维护。

完整版权和来源说明见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

## 使用边界

本项目仅用于技术研究和个人自用，不得用于批量骚扰、恶意刷量或规避平台限制。自动化操作可能触发平台风控，使用者应遵守抖音服务协议及所在地法律，并自行承担账号限制、封禁或数据丢失等风险。建议仅配置少量真实好友，并使用合理的每日运行频率。

## License

[MIT](LICENSE)
