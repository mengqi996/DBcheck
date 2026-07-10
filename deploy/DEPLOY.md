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

CentOS / RHEL / Rocky / AlmaLinux 8+:

```bash
# 一次性包
sudo dnf install -y git rsync python3.11 python3.11-pip
```

CentOS / RHEL 7 如果 yum 源里没有 `python3.11`,优先走 SCL 安装 Python 3.8:

```bash
sudo yum install -y centos-release-scl
sudo yum install -y rh-python38 rh-python38-python-pip git rsync
```

如果 `centos-release-scl` 不存在,先启用 EPEL:

```bash
sudo yum install -y epel-release
sudo yum install -y python38 python38-pip git rsync
```

不要替换系统默认 Python。安装新版本后指定给安装脚本:

```bash
cd /opt/dbcheck/deploy

# SCL 安装的 Python:
sudo PYTHON_BIN=/opt/rh/rh-python38/root/usr/bin/python3.8 ./install.sh

# EPEL 安装的 Python:
sudo PYTHON_BIN=python3.8 ./install.sh
```

安装脚本也会自动尝试识别 `python3.12` / `python3.11` / `python3.10` / `python3.9` / `python3.8`。

如果用 firewalld:

```bash
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

# 如果默认 python3 小于 3.8,改用服务器上的新 Python:
sudo PYTHON_BIN=python3.11 ./install.sh
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

DBCheck 当前默认**腾讯云主动调用全部开启**。如果你要显式标注生产环境配置,
推荐在 `/opt/dbcheck/.env` 写纯值；后端会容错忽略 `#` 后面的行尾说明,
但纯值最容易排查。

编辑配置:

```bash
sudo vi /opt/dbcheck/.env
```

开关含义和推荐写法:

```env
# SQLite 数据库和 Fernet 密钥必须放在持久化 data 目录
DBCHECK_SQLITE_PATH=/opt/dbcheck/data/dbcheck.db
DBCHECK_FERNET_KEY_FILE=/opt/dbcheck/data/.fernet_key

# 腾讯云 API 总开关,控制备份同步、慢 SQL 同步、监控、binlog 等所有主动云 API
DBCHECK_TENCENT_API_ENABLED=true

# 真实创建腾讯云备份任务；如果只想同步备份元数据,可以改成 false
DBCHECK_CLOUD_BACKUP_ENABLED=true

# 慢 SQL 自动轮询
DBCHECK_SCHEDULER_ENABLED=true
```

如果把这两个路径配到 `/opt/dbcheck/app` 或保留后端默认值,代码更新后服务可能会重新创建一个空的
`dbcheck.db`,表现出来就是“生产实例资产突然全没了”。新版后端和 `install.sh` 会直接拒绝这种配置。

```bash
sudo systemctl restart dbcheck
curl -s http://127.0.0.1:8000/api/scheduler/status
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
| 重启后数据像丢了 | 检查 `/opt/dbcheck/.env` 是否启用 `DBCHECK_SQLITE_PATH=/opt/dbcheck/data/dbcheck.db`,并确认真实数据在这个文件里 |
| TC 接口 403 | 检查进程环境里 `DBCHECK_TENCENT_API_ENABLED=true` 是否生效 |
| 登录凭证失效 | `.fernet_key` 跟当初加密的不是同一个,需要重录凭证 |
| 慢查询拿不到 | 需要 `DBCHECK_TENCENT_API_ENABLED=true` 和 `DBCHECK_SCHEDULER_ENABLED=true`,且绑定已启用 |

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
