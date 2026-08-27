# Docker 网页部署

Prometheus Relay 由三个 Compose 服务组成：`web` 提供登录、客户控制台和管理后台，`scheduler` 负责每日计划，`worker` 执行单次浏览器任务。三者共享私有数据卷，Cookie 不会写入镜像。

## 首次部署

以下示例将项目放在 `/opt/prometheus-relay`，配置放在 `/etc/prometheus-relay`：

```bash
sudo git clone https://github.com/MinG-98/prometheus-relay.git /opt/prometheus-relay
cd /opt/prometheus-relay

sudo mkdir -p /etc/prometheus-relay
sudo install -m 600 .env.web.example /etc/prometheus-relay/web.env
sudo editor /etc/prometheus-relay/web.env

sudo docker compose build --pull
sudo install -m 644 deploy/prometheus-relay-web.service /etc/systemd/system/
sudo install -m 644 deploy/prometheus-relay-worker.service /etc/systemd/system/
sudo install -m 644 deploy/prometheus-relay-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prometheus-relay-web.service
sudo systemctl enable --now prometheus-relay-scheduler.service
```

`web` 默认监听 `127.0.0.1:18081`，应通过 Caddy、Nginx 或其他反向代理提供 HTTPS，不应直接暴露该端口。默认启用会话登录：管理员账号来自 `web.env`，客户账号由管理员在后台创建。`PROMETHEUS_RELAY_COOKIE_KEY` 必须是有效的 Fernet 密钥并保持私密；它用于加密 SQLite 中的 Cookie。

首次进入后，管理员访问管理后台创建客户。客户使用管理员交付的客户账号登录，只能看到自己的工作区。禁用客户会同时阻止其登录和后续定时任务；删除客户会删除该客户工作区中的账号、Cookie、目标和运行记录。

## 网页配置

打开网页后优先点击“扫码添加”：VPS 无头浏览器生成二维码，手机确认后自动抓取 Cookie、昵称和抖音号。

如果抖音要求短信安全验证，VPS 会在官方验证页面中选择短信验证方式。验证码发送到绑定手机后，将验证码填入网页显示的输入框并点击“提交验证码”。提交后请保持窗口打开，等待验证完成；成功后账号会自动保存。若页面重新显示输入框，说明验证码未通过，应使用最新验证码重新提交，避免连续高频尝试。

扫码会话、二维码和短信验证码只临时保存在 Web 进程内存中，最终 Cookie 写入私有数据卷且不会通过网页接口回显。账号保存后请核对网页显示的抖音号和昵称，确认登录的是预期账号。

如果扫码暂时不可用，也可以点击“手动添加”，选择 Cookie-Editor 导出的 JSON 文件（最大 2 MB）或手动粘贴。

任务设置中可以启用每日定时运行，时间采用 `HH:MM`，时区采用 IANA 名称，例如 `Asia/Shanghai`。网页“立即运行”和调度器共用文件锁，避免任务并发。

## 手动运行与日志

```bash
sudo systemctl start prometheus-relay-worker.service
sudo journalctl -u prometheus-relay-worker.service -n 200 --no-pager
```

该兼容性 one-shot 单元默认执行迁移后的管理员工作区。客户任务应从客户控制台点击“立即运行”，或交给 `scheduler`；这样每次任务都会带有明确的工作区范围。

网页内也可以直接运行任务并查看最近日志。

## 更新

```bash
cd /opt/prometheus-relay
sudo git pull --ff-only
sudo docker compose build --pull
sudo systemctl restart prometheus-relay-web.service
sudo systemctl restart prometheus-relay-scheduler.service
```

更新前建议备份数据卷：

```bash
sudo docker run --rm \
  -v prometheus-relay-data:/data:ro \
  -v "$PWD":/backup \
  alpine tar -czf /backup/prometheus-relay-data.tar.gz -C /data .
```

备份文件包含 Cookie，不要上传到公共位置。

## 并行验收新版本

升级多用户门户时，建议使用独立目录、Compose 项目、端口和数据卷，先验证再切换现有服务：

```bash
sudo git clone --branch feature/customer-portal https://github.com/MinG-98/prometheus-relay.git /opt/prometheus-relay-next
sudo mkdir -p /etc/prometheus-relay-next
sudo install -m 600 /opt/prometheus-relay-next/.env.web.example /etc/prometheus-relay-next/web.env
sudo editor /etc/prometheus-relay-next/web.env

cd /opt/prometheus-relay-next
sudo PROMETHEUS_RELAY_ENV_FILE=/etc/prometheus-relay-next/web.env \
  PROMETHEUS_RELAY_WEB_PORT=18082 \
  PROMETHEUS_RELAY_DATA_VOLUME=prometheus-relay-next-data \
  PROMETHEUS_RELAY_LOGS_VOLUME=prometheus-relay-next-logs \
  docker compose -p prometheus-relay-next up -d --build web
```

验收期间不要启动新版本的 `scheduler`，也不要让新版本使用现网数据卷。确认管理员登录、创建客户、客户登录、扫码/上传 Cookie、客户数据隔离和手动任务都正常后，再单独启动新版本调度器。切换时保留旧目录和旧数据卷，出现问题可停止新项目并恢复原服务。

## 反向代理示例

Caddy：

```caddyfile
relay.example.com {
    reverse_proxy 127.0.0.1:18081
}
```

反向代理完成后访问 `https://relay.example.com`。`/healthz` 可用于本机健康检查，但外部访问仍应经过统一的访问控制策略。

若必须部署在子路径，务必将不带尾部斜杠的入口重定向到带斜杠的入口，否则浏览器会把相对静态资源解析到站点根目录：

```caddyfile
example.com {
    @relay_without_slash path /relay
    redir @relay_without_slash /relay/ 308

    handle_path /relay/* {
        reverse_proxy 127.0.0.1:18081
    }
}
```
