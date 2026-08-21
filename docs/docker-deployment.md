# Docker 部署

本项目在 VPS 上以一次性容器运行：容器完成当天任务后退出，由 systemd timer 每天启动一次。

## 首次部署

```bash
mkdir -p /etc/douyin-fire
install -m 600 .env.example /etc/douyin-fire/douyin-fire.env
vim /etc/douyin-fire/douyin-fire.env
docker compose build --pull
install -m 644 deploy/douyin-fire.service /etc/systemd/system/douyin-fire.service
install -m 644 deploy/douyin-fire.timer /etc/systemd/system/douyin-fire.timer
systemctl daemon-reload
systemctl enable --now douyin-fire.timer
```

## 手动试运行

```bash
systemctl start douyin-fire.service
journalctl -u douyin-fire.service -n 200 --no-pager
```

## 维护

更新代码后重新构建镜像：

```bash
docker compose build --pull
systemctl start douyin-fire.service
```

配置文件只放在 `/etc/douyin-fire/douyin-fire.env`，不要复制进镜像或提交到 Git。定时任务默认每天北京时间 09:00 执行，并随机延迟最多 5 分钟以避免固定时间触发。
