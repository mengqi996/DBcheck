# DBCheck 生产部署指南

目标环境:**CentOS / RHEL / Rocky / AlmaLinux 8+**,systemd 管理服务,FastAPI 合并前端单端口 8000 监听 127.0.0.1(默认被 firewalld 保护)。

---

## 0. 决策摘要

| 项 | 选定 |
|---|---|
| 安装路径 | `/opt/dbcheck`(代码 + venv + 数据 + 日志一棵树下) |
| 监听 | `127.0.0.1:8000` (systemd),对外需走 Nginx 反代或开 firewalld |
| 数据迁移 | `dbcheck.db` + `.fernet_key` 完整迁过去 |
| 进程用户 | `dbcheck`(系统用户,无登录) |
| 日志 | `journalctl -u dbcheck` |
| 主动调用 | **默认全关**,需要时编辑 `/opt/dbcheck/.env` |

---

## 1. 服务器准备

```bash
# 一次性包
sudo dnf install -y python3 python3-pip python3-venv rsync

# 如果用 firewalld
sudo systemctl enable --now firewalld
```

---

## 2. 从 Mac 推送代码

```bash
# 在 Mac 上,项目根目录
rsync -av --delete \
    --exclude='.DS_Store' \
    --exclude='backend/venv/' \
    --exclude='backend/__pycache__/' \
    --exclude='backend/dbcheck.db' \
    --exclude='backend/.fernet_key' \
    -e ssh \
    ./ user@server:/tmp/dbcheck-staging/
```

> ⚠️ `--exclude` 把数据库和密钥排除掉,**单独传**(见第 4 步),免得覆盖服务器上已有的数据。

然后在服务器上搬到最终位置:

```bash
ssh user@server
sudo mkdir -p /opt
sudo cp -a /tmp/dbcheck-staging /opt/dbcheck
sudo chown -R root:root /opt/dbcheck
```

> 如果是**全新部署**(服务器上还没有 dbcheck),可以直接 `rsync` 到 `/opt/dbcheck` 跳过 staging 这一步。

---

## 3. 跑 install.sh

```bash
ssh user@server
cd /opt/dbcheck/deploy
sudo ./install.sh
```

install.sh 会做这些事:
1. 装 python3(如果缺)
2. 创建系统用户 `dbcheck`
3. 建目录树 `app/ data/ logs/ run/ venv/`
4. 把代码 rsync 到 `/opt/dbcheck/app/`
5. 把 `index.html` 复制到 `/opt/dbcheck/`
6. 建 venv,装 requirements
7. 复制 `.env.example` → `/opt/dbcheck/.env`
8. 装 systemd unit,enable + start
9. 开放 firewalld 8000/tcp(如果 firewalld 启用)

**注意**:install.sh 默认是把 staging 里的 backend/ 当源。你需要先确认 `PROJECT_ROOT=/opt/dbcheck` 存在 `backend/` 和 `index.html`。

---

## 4. 迁移数据(关键步骤)

```bash
# Mac 上,把数据库和密钥推到服务器
scp backend/dbcheck.db backend/.fernet_key user@server:/opt/dbcheck/data/

# 服务器上,改属主 + 收紧权限
ssh user@server
sudo chown dbcheck:dbcheck /opt/dbcheck/data/*
sudo chmod 600 /opt/dbcheck/data/*

# 重启服务让它加载新数据
sudo systemctl restart dbcheck
```

> ⚠️ **.fernet_key 决定了你那 1 个腾讯云凭证的解密能力**。丢失它,加密的 SecretKey 永久无法恢复,只能重新录入。

---

## 5. 验证

```bash
# 服务状态
sudo systemctl status dbcheck

# 实时日志
sudo journalctl -u dbcheck -f

# 端口监听
sudo ss -tlnp | grep 8000

# 接口探测
curl -s http://127.0.0.1:8000/api/dashboard | python3 -m json.tool
```

浏览器访问 `http://<server-ip>:8000/`,应该看到 DBCheck 的工作台页面。

---

## 6. 对外暴露(可选)

默认 systemd 绑定 `127.0.0.1:8000`,**仅本机**能访问。两种方式对外开放:

### A. 走 firewalld(简单,内网够用)

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

### B. 加 Nginx 反代 + HTTPS(推荐公网)

```bash
sudo dnf install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/conf.d/dbcheck.conf`:

```nginx
server {
    listen 80;
    server_name dbcheck.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo systemctl enable --now nginx
sudo certbot --nginx -d dbcheck.example.com
```

**强烈建议**:如果服务要暴露到公网,一定要走 Nginx + HTTPS,不要直接开放 8000 端口。

---

## 7. 主动调用开关(腾讯云)

DBCheck 默认**所有主动调用都关闭**,所以部署后它不会主动连腾讯云。

当你准备好接入时:

```bash
sudo systemctl edit dbcheck   # 或直接编辑 /opt/dbcheck/.env
```

把对应行取消注释并改为 `true`:

```bash
DBCHECK_TENCENT_API_ENABLED=true
# DBCHECK_CLOUD_BACKUP_ENABLED=true
# DBCHECK_SCHEDULER_ENABLED=true
```

```bash
sudo systemctl restart dbcheck
```

---

## 8. 日常维护

| 操作 | 命令 |
|---|---|
| 查看状态 | `sudo systemctl status dbcheck` |
| 实时日志 | `sudo journalctl -u dbcheck -f` |
| 最近 200 行日志 | `sudo journalctl -u dbcheck -n 200 --no-pager` |
| 重启服务 | `sudo systemctl restart dbcheck` |
| 停止服务 | `sudo systemctl stop dbcheck` |
| 更新代码 | `rsync` 新代码 → `sudo systemctl restart dbcheck` |
| 备份数据 | `sudo cp -a /opt/dbcheck/data /backup/dbcheck-$(date +%F)` |
| 完全卸载 | `sudo /opt/dbcheck/deploy/uninstall.sh [--purge]` |

---

## 9. 排错速查

| 现象 | 检查 |
|---|---|
| 服务起不来 | `sudo journalctl -u dbcheck -n 50` |
| 端口没起 | `sudo ss -tlnp | grep 8000` |
| 页面 502 | 后端没起或挂了 |
| 页面 404 | 浏览器没带路径,直访 `http://server:8000/`(不是 `/index.html`) |
| TC 接口 403 | `.env` 里没开 `DBCHECK_TENCENT_API_ENABLED=true` |
| 登录凭证失效 | `.fernet_key` 跟当初加密的不是同一个,需要重录凭证 |
| 慢查询拿不到 | 同时需要 `TENCENT_API_ENABLED=true` 和 `SCHEDULER_ENABLED=true`,且绑定已启用 |

---

## 10. 备份建议

最关键的 2 个文件:

- `/opt/dbcheck/data/dbcheck.db` — 你的实例/备份/慢查询/凭证
- `/opt/dbcheck/data/.fernet_key` — 加密密钥

建议每日 cron 备份到一个独立位置(异机更佳):

```bash
# /etc/cron.d/dbcheck-backup
0 2 * * * root tar -czf /backup/dbcheck-$(date +\%F).tgz -C /opt/dbcheck data
```
