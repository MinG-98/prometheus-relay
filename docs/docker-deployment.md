# Docker 网页部署

项目在 VPS 上运行一个受 Basic Auth 保护的网页管理面板，后台仍使用 Playwright 执行任务。网页服务和每日任务共用 Docker 数据卷，Cookie 不会写进镜像。

## 首次部署

```bash
mkdir -p /etc/douyin-fire
install -m 600 .env.web.example /etc/douyin-fire/web.env
vim /etc/douyin-fire/web.env
docker compose build --pull
install -m 644 deploy/douyin-fire-web.service /etc/systemd/system/douyin-fire-web.service
install -m 644 deploy/douyin-fire.service /etc/systemd/system/douyin-fire.service
install -m 644 deploy/douyin-fire.timer /etc/systemd/system/douyin-fire.timer
systemctl daemon-reload
systemctl enable --now douyin-fire-web.service
systemctl enable --now douyin-fire.timer
```

网页面板默认使用抖音号匹配目标好友，目标好友昵称可能改名或重名；也可以切换为昵称匹配。面板默认监听 `127.0.0.1:18081`，应通过 Caddy 或其他反向代理访问，不要直接暴露到公网。默认启用 Basic Auth；只有在入口已经由 VPN、IP 白名单或其他网关保护时，才应设置 `AUTH_ENABLED=false`。

## 网页配置

打开网页后填写账号、目标好友和 Cookie-Editor 导出的 JSON。已保存的 Cookie 只显示数量，不会回显。网页的“立即运行”会与每日定时任务共用锁，避免并发发送。

## 手动执行和查看日志

```bash
systemctl start douyin-fire.service
journalctl -u douyin-fire.service -n 200 --no-pager
```

更新代码后重新构建并重启网页服务：

```bash
docker compose build --pull
systemctl restart douyin-fire-web.service
```

每日任务默认北京时间 09:00 执行，并随机延迟最多 5 分钟。Cookie 属于登录凭证，不要提交到 Git 或发送到聊天。
