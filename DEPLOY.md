# 把 Alex 部署到云端

Alex 需要 `server.py` 这个进程在跑（它代理 Yahoo 并管理 cookie/crumb），
所以不能放纯静态托管。服务端不存任何状态，镜像只有几 MB，很适合部署。

已经准备好：`Dockerfile`、`fly.toml`、`render.yaml`、`.dockerignore`、`.gitignore`，
项目也已 `git init` 并提交。`server.py` 改成从环境变量读 `PORT` / `HOST`，
并支持可选口令 `ALEX_TOKEN`。

## 口令怎么用

设了环境变量 `ALEX_TOKEN=某串随机字符` 之后：**第一次**访问要带上
`?key=<你的token>`，例如

```
https://你的app.fly.dev/?key=某串随机字符
```

服务器会种一个一年有效的 cookie 再跳转到干净地址；之后直接访问即可。
把带 `?key=` 的那条链接存成书签就行。`/api/health` 不校验（给平台做健康检查）。

不设 `ALEX_TOKEN` 就是完全公开——不建议放公网。

---

## 方案 A（推荐）：Fly.io — 闲时自动关机，约 $0–2/月

不需要 GitHub；闲置自动缩到 0，冷启动 2–3 秒；自带 HTTPS 和
`https://<你的名字>.fly.dev` 域名。

```powershell
# 1. 安装 flyctl（Windows PowerShell）
iwr https://fly.io/install.ps1 -useb | iex

# 2. 注册 / 登录（需要一张信用卡做验证，不会乱扣）
fly auth signup            # 已有账号用 fly auth login

# 3. 在本文件夹里初始化（会读现成的 fly.toml；app 名全球唯一，按提示改一个）
fly launch --no-deploy --copy-config

# 4. 设置口令（换成你自己的随机串）
fly secrets set ALEX_TOKEN="改成一串只有你知道的随机字符"

# 5. 部署
fly deploy
```

完成后打开 `https://<你的app名>.fly.dev/?key=你上面设的串`。

- 换机房：改 `fly.toml` 的 `primary_region`（`nrt`=东京 / `hkg`=香港 / `sjc`=硅谷），
  或 `fly regions set nrt`。
- 一直在线不休眠（约 $2/月）：`fly.toml` 里 `min_machines_running = 1`。
- 更新代码：再跑 `fly deploy`。
- 删除：`fly apps destroy <你的app名>`。

---

## 方案 B：Render 免费档 — $0，但冷启动慢（~50 秒）

先把这个文件夹推到 GitHub（一次性；仓库已初始化）：

```bash
git remote add origin https://github.com/<你>/<仓库>.git
git push -u origin main
```

然后在 [render.com](https://render.com)：New → **Blueprint** → 选这个仓库
（它会读 `render.yaml` 自动建服务）。
建好后在服务的 Environment 里把 `ALEX_TOKEN` 填成一串随机字符。
访问 `https://<app>.onrender.com/?key=你的串`。

免费档闲置 15 分钟休眠，每月 750 小时额度。

---

## 方案 C：Oracle Cloud「Always Free」— 真·永久免费 + 常驻

价最低（$0 永久）但要自己管一台 Linux 小主机：

1. 注册 Oracle Cloud（要信用卡验证，偶尔会被风控拒）。
2. 建一台 **Always Free** 的 ARM（Ampere）实例，Ubuntu。
3. 放行 8080 端口（安全组 + `iptables`）。
4. `scp` 上传 `server.py index.html universe.json`，然后：
   ```bash
   sudo apt update && sudo apt install -y python3
   HOST=0.0.0.0 PORT=8080 ALEX_TOKEN="随机串" nohup python3 server.py --no-browser &
   ```
   长期跑写成 systemd 服务。
5. HTTPS：用 Caddy 反代，或在前面套 Cloudflare。

---

## 通用注意

- **一定设 `ALEX_TOKEN`**。公网裸奔别人也能借你的服务器刷 Yahoo，容易让 IP 被限流。
- 首次打开仍要在后台拉 503 只 × 2 年日线，约 30–40 秒；之后历史缓存在**你的浏览器**里
  （换设备 / 换浏览器会重新拉一次）。
- Yahoo 是免费公共接口，被限流时会短时间拉不到数据，等几分钟自动恢复。
- 云服务器在海外，反而不用自己翻墙去够 Yahoo（Yahoo Finance 在中国大陆被墙）；
  你只要能访问 `*.fly.dev` / `*.onrender.com`，必要时用自定义域名 + Cloudflare。
