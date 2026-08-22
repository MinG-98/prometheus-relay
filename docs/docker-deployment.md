# Docker 网页部署

Prometheus Relay 由三个 Compose 服务组成：`web` 提供管理控制台，`scheduler` 负责每日计划，`worker` 执行单次浏览器任务。三者共享私有数据卷，Cookie 不会写入镜像。

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

控制台默认监听 `127.0.0.1:18081`，应通过 Caddy、Nginx 或其他反向代理提供 HTTPS，不应直接暴露该端口。默认启用 Basic Auth；请在 `web.env` 中设置长且唯一的密码，并保持文件权限为 `600`。

## 网页配置

打开网页后填写账号和目标好友。Cookie 可以直接选择 Cookie-Editor 导出的 JSON 文件（最大 2 MB），也可以手动粘贴。保存后的 Cookie 只显示数量，不会回显。

任务设置中可以启用每日定时运行，时间采用 `HH:MM`，时区采用 IANA 名称，例如 `Asia/Shanghai`。网页“立即运行”和调度器共用文件锁，避免任务并发。

## 手动运行与日志

```bash
sudo systemctl start prometheus-relay-worker.service
sudo journalctl -u prometheus-relay-worker.service -n 200 --no-pager
```

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
  -v prometheus-relay_prometheus_relay_data:/data:ro \
  -v "$PWD":/backup \
  alpine tar -czf /backup/prometheus-relay-data.tar.gz -C /data .
```

备份文件包含 Cookie，不要上传到公共位置。

## 反向代理示例

Caddy：

```caddyfile
relay.example.com {
    reverse_proxy 127.0.0.1:18081
}
```

反向代理完成后访问 `https://relay.example.com`。`/healthz` 可用于本机健康检查，但外部访问仍应经过统一的访问控制策略。
