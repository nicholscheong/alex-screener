# 把 Alex 部署到云端

Alex 需要 `server.py` 这个进程在跑（它代理 Yahoo 并管理 cookie/crumb），
所以不能放纯静态托管。服务端不存任何状态，镜像只有几 MB，很适合部署。

已经为你准备好：`Dockerfile`、`fly.toml`、`.dockerignore`，`server.py` 也改成了
从环境变量读 `PORT` / `HOST`，并支持可选的 HTTP Basic 口令（`ALEX_AUTH`）。

---

## 方案 A（推荐）：Fly.io — 闲时自动关机，约 $0–2/月

优点：不需要 GitHub，本地文件夹直接部署；闲置自动缩到 0，冷启动 2–3 秒；
自带 HTTPS 和 `https://<你的名字>.fly.dev` 域名。

```bash
# 1. 安装 flyctl（Windows PowerShell）
iwr https://fly.io/install.ps1 -useb | iex

# 2. 注册 / 登录（需要一张信用卡做验证，不会乱扣）
fly auth signup      # 已有账号用 fly auth login

# 3. 在本文件夹里初始化（会读现成的 fly.toml；app 名全球唯一，按提示改一个）
fly launch --no-deploy --copy-config

# 4. 设置访问口令（强烈建议；把 me / 换成你自己的）
fly secrets set ALEX_AUTH="me:一个只有你知道的密码"

# 5. 部署
fly deploy
```

完成后打开 `https://<你的app名>.fly.dev`，浏览器会弹出登录框，输入上面设的
用户名 / 密码即可。

- 想换机房：编辑 `fly.toml` 的 `primary_region`（`nrt`=东京，`hkg`=香港，
  `sjc`=硅谷），或部署后 `fly regions set nrt`。
- 想关掉自动休眠（一直在线，约 $2/月）：`fly.toml` 里 `min_machines_running = 1`。
- 更新代码后重新部署：再跑一次 `fly deploy`。
- 停掉：`fly apps destroy <你的app名>`。

---

## 方案 B：Render 免费档 — $0，但冷启动慢

需要先把这个文件夹推到 GitHub（一次性）：

```bash
git init && git add -A && git commit -m "Alex screener"
# 在 GitHub 建一个空仓库，然后：
git remote add origin https://github.com/<你>/<仓库>.git
git push -u origin main
```

然后在 [render.com](https://render.com)：New → **Web Service** → 连上这个仓库 →
Environment 选 **Docker** → 免费档创建。
Environment 里加一条：`ALEX_AUTH` = `me:你的密码`。

免费档闲置 15 分钟休眠，下次打开要等 ~50 秒冷启动；每月 750 小时额度。

---

## 方案 C：Oracle Cloud「Always Free」— 真·永久免费 + 常驻

价最低（$0 永久）但要自己管一台 Linux 小主机：

1. 注册 Oracle Cloud（要信用卡验证，偶尔会被风控拒）。
2. 创建一台 **Always Free** 的 ARM（Ampere）实例，Ubuntu。
3. 开放 8080 端口（安全组 + `iptables`）。
4. `scp` 上传 `server.py index.html universe.json`，然后：
   ```bash
   sudo apt update && sudo apt install -y python3
   HOST=0.0.0.0 PORT=8080 ALEX_AUTH="me:密码" nohup python3 server.py --no-browser &
   ```
   长期跑建议写成 systemd 服务。
5. HTTPS：用 Caddy 反代，或在前面套一层 Cloudflare。

---

## 通用注意

- **一定设 `ALEX_AUTH`**。公网裸奔的话，别人也能借你的服务器刷 Yahoo，容易让你的
  服务器 IP 被 Yahoo 限流。
- 首次打开仍要在后台拉 503 只 × 2 年日线，约 30–40 秒；之后历史缓存在**你的浏览器**里
  （换设备 / 换浏览器会重新拉一次）。
- Yahoo 是免费公共接口，被限流时会短时间拉不到数据，等几分钟自动恢复。
- 云服务器在海外，反而不用自己翻墙去够 Yahoo（Yahoo Finance 在中国大陆是被墙的）；
  你只要能访问 `*.fly.dev` / `*.onrender.com` 即可，必要时用自定义域名 + Cloudflare。
