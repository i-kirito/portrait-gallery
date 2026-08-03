# 🎀 Portrait Gallery

当前版本：**v1.3.10**

> AI 穿搭生图 & 个人画廊系统 —— 让 AI 每天为你量身定制穿搭方案并自动生成写真

一个基于 LLM 日程驱动的 AI 个人形象生成与展示系统。每天自动生成穿搭日程，按时调用 AI 生图引擎生成写真照片，通过 Web 画廊展示和管理。

## ✨ 功能亮点

- 📅 **LLM 日程驱动** — DeepSeek 自动生成每日穿搭日程（HH:mm 精度），按时触发生图
- 👥 **多角色与群聊** — 支持本地角色库、单人图、合照、群聊房间、角色自动回复和群聊触发生图
- 🎨 **多引擎生图** — 支持 OpenAI-compatible API (GPT Image / AxonHub / 自定义端点)、Gemini、Gitee z-image-turbo 可选回退（默认关闭）
- 🧩 **三级 LLM 模型链** — 设置页维护主模型与多级 fallback，日程、caption、群聊和即时推断都会按链路降级
- ☁️ **关键词偏好云** — 从自定义生图输入与收藏衣柜中提取偏好词，作为每日穿搭日程的柔性参考
- 🗓️ **真实日期约束** — 日程生成会识别周末、法定节假日、调休上班日和自定义假期，减少休息日写上班/上课的冲突
- 🔁 **日程去重约束** — 结合近期日程历史，减少连续出现赖床、刷手机、做饭等重复模板
- ✏️ **今日计划微调** — 今日生图计划支持双击编辑活动内容，并同步保存到当天日程和待执行任务
- 🖼️ **Web 画廊** — 今日/全部/收藏/衣柜四 Tab，横版大卡 + 网格双布局
- 🎀 **穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- ⏰ **动态调度** — LLM 日程驱动，根据 HH:mm 时间动态创建一次性生图任务
- 🔧 **REST API** — 完整 CRUD 接口，支持集成到任何 AI Agent

## 🚀 快速开始

### 1. 克隆

```bash
git clone https://github.com/OWNER/REPO.git
cd portrait-gallery
```

### 2. 安装依赖

```bash
python3 -m pip install -r app/requirements.txt
```

### 3. 配置

编辑 `config/config.yaml`，填入你的 API 密钥：

```yaml
llm:
  base_url: "http://your-cpa-proxy:port/v1"   # CPA 代理地址（用于 LLM）
  api_key: "your-api-key"
  model: "deepseek-v4-flash"

image_gen:
  script_dir: "./app/zhuzhu"
  default_engine: "gptimage"
  timeout: 300
```

或启动后通过 Web UI 的 ⚙️ 设置面板在线填写。

### 4. 启动

```bash
cd app
python3 main.py
```

访问 **http://localhost:18889** 即可使用画廊。

首次部署时，本机打开 Web UI 会先要求设置访问密码；也可以提前通过 `GALLERY_PASSWORD` 环境变量预置密码。非本机访问需要输入密码，验证通过后服务端会签发独立的 7 天会话 Cookie；修改密码会立即使旧会话失效。

### 5. 部署方式

#### 方式一：直接运行（开发/测试）

```bash
cd app
python3 main.py
```

#### 方式二：Python 后台运行（本机推荐）

`app/run_launch.sh` 会自动定位项目目录、优先使用项目 `.venv`，并设置
`CONFIG_PATH`、`GALLERY_DATA_DIR` 等运行环境：

```bash
nohup ./app/run_launch.sh >/tmp/portrait_gallery_manual_start.log 2>&1 &
curl http://localhost:18889/api/health
```

服务启动后，网页设置里的“重启服务”按钮会调用 `/api/restart`，
由当前 Python 服务拉起新的 Python 进程并退出旧进程；不会拉取代码，
也不会修改本地配置、API Key、图片或参考图。

应用会追加写入 `logs/gallery.log`，重启不会覆盖；日志按天轮转，并自动清理 3 天前的轮转文件。

#### 方式三：Docker Hub 镜像

`docker-compose.yaml` 默认构建并使用本地镜像 `hermes-portrait-gallery:latest`。
如果要使用已经发布到 Docker Hub/GHCR 的镜像，通过 `PORTRAIT_GALLERY_IMAGE` 指定：

```bash
PORTRAIT_GALLERY_IMAGE=REGISTRY_OR_USER/hermes-portrait-gallery:1.3.10 docker compose up -d
curl http://localhost:18889/api/health
```

#### 方式四：一键发布新版本

仓库提供 `scripts/release.sh`，可一键完成版本号更新、Git tag、多架构 Docker 镜像推送和 GitHub Release：

```bash
# 预演（不改文件、不推送）
./scripts/release.sh 1.3.9 --dry-run --notes $'- fix A\n- feat B'

# 正式发布（工作区需干净）
./scripts/release.sh 1.3.9 --notes-file /tmp/notes.md

# 或自动 bump patch
./scripts/release.sh --bump patch --notes $'- bugfix'

# 仅重发当前 VERSION 对应镜像
./scripts/release.sh --docker-only
```

默认镜像为 `ikirito9/hermes-portrait-gallery`，builder 为 `portrait-gallery-publisher`，平台 `linux/amd64,linux/arm64`。


本地开发仍可使用源码构建：

```bash
docker compose build
docker compose up -d
```

### 在线动态时间线

“群聊”Tab 的动态流可以作为一个共享时间线使用。每个用户的浏览器仍只访问自己的本地画廊；本地服务负责把用户确认发布的推文上传到 ECS 中心，并代理其他用户的动态图片。

1. 在 ECS 使用专用 Compose 文件和全新的专用 Docker volume 启动只包含共享动态 API 的中心进程。不要复用完整画廊的旧 `data/`。令牌通过 Compose secret 文件或 Secrets Manager 提供，不会写入中心数据卷，也不会出现在容器环境变量中：

   ```bash
   install -d -m 700 secrets
   install -o 10001 -g 10001 -m 400 /dev/null secrets/social_server_token
   openssl rand -hex 32 > secrets/social_server_token
   docker compose -f docker-compose.social-hub.yaml up -d --build
   curl http://127.0.0.1:18889/api/health  # 应包含 "service":"social-hub"
   test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18889/)" = 404
   ```

   最后一条检查用于确认 18889 对应的是不提供 UI 的专用 Hub，而不是完整画廊或其他服务。
   如使用已发布镜像而不是在服务器上构建源码，可指定与画廊版本相同的镜像标签：

   ```bash
   SOCIAL_HUB_IMAGE=REGISTRY_OR_USER/hermes-portrait-gallery-social-hub:1.3.10 \
     docker compose -f docker-compose.social-hub.yaml up -d
   ```

2. 当前线上中心地址为 `https://8.134.251.200`，使用 Let's Encrypt 短周期 IP 证书提供受信任的 HTTPS，不依赖域名或 DNS 配置。Nginx 模板见 [`deploy/nginx/portrait-gallery-social-hub.conf`](deploy/nginx/portrait-gallery-social-hub.conf)，只公开 `/api/social/hub/*`，并将动态配图上传上限提高到 70 MiB；不要把 18889 或画廊的其他路径直接暴露到公网。

   证书检查和续期模板见 [`deploy/systemd/portrait-social-hub-cert.service`](deploy/systemd/portrait-social-hub-cert.service) 与 [`deploy/systemd/portrait-social-hub-cert.timer`](deploy/systemd/portrait-social-hub-cert.timer)。它们使用 `lego` 每日两次检查证书；需要续期时临时释放 443 端口完成 TLS-ALPN-01 验证，并保证任务结束后恢复 Nginx。将模板安装到 ECS 宿主机对应目录后启用 timer：

   ```bash
   systemctl daemon-reload
   systemctl start portrait-social-hub-cert.service
   systemctl enable --now portrait-social-hub-cert.timer
   nginx -t
   test "$(curl -sS -o /dev/null -w '%{http_code}' https://8.134.251.200/api/social/hub/status)" = 401
   ```

   上述模板假定 Nginx 运行在 ECS 宿主机；若 Nginx 本身在普通 bridge 网络容器中，它的 `127.0.0.1` 不是宿主机，需让两者使用同一可达网络或让 Nginx 也使用 host 网络。中心令牌只放在专用 secret 文件中，不要写入 Nginx、systemd unit 或 README。

3. 在每台客户端画廊的“群聊 → 动态 → 设置”将中心地址填写为 `https://8.134.251.200`，再填写中心令牌和本机名称，保存后用“测试连接”确认连通。
4. 在今日卡片、画廊卡片或图片详情点击“发推”，确认草稿后发布。中心节点会保存图片副本，因此之后删除原画廊图片不会让已发布动态失效。

中心节点只持久化所发布的名称、256px 头像、推文/回复正文、推文配图，以及分页和安全删除所必需的不透明 ID 与时间。头像和配图会在上传前后重新编码并移除 EXIF/XMP 等本地元数据；本地角色 ID、画廊名称、生成提示词、来源路径、客户端令牌以及点赞/转发/收藏参与者都不会写进 ECS 的动态数据。点赞、转发和收藏仅保存在各自浏览器中；回复作为回复推文共享。

客户端身份由其仅保存在本机的私有令牌派生，中心无须保存客户端注册表，其他客户端也无法仅凭共享中心令牌冒充它删除内容。普通画廊 API 和图片仍沿用原有访问密码保护。专用中心的 `hermes-portrait-social-hub-data` volume 只创建 `social.json`、`social.lock` 和 `social-media/`；不启动日程、生图、群聊房间、设置页面或静态 UI。启动时会清理中断写入留下的社交临时文件。运行时数据不应提交到 Git。

## 📐 架构

```
portrait-gallery/
├── app/
│   ├── main.py              # 入口：APScheduler 调度 + aiohttp 启动
│   ├── web_server.py        # REST API + 静态文件服务
│   ├── core.py              # 生图核心（同步、元数据、翻译）
│   ├── store.py             # 文件锁封装（并发安全读写）
│   ├── scheduler.py         # LLM 日程生成
│   ├── characters.py        # 多角色注册表、角色 prompt 与本地角色存储
│   ├── group_chat.py        # 群聊房间/消息持久化与 Hermes bridge payload
│   ├── calendar_context.py  # 周末、节假日、调休日等真实日期约束
│   ├── text_repair.py       # 常见中文 mojibake 文本修复
│   ├── zhuzhu/
│   │   ├── core.py          # 生图底层（GPT Image / Gitee 调用）
│   │   ├── generate.py      # 生图调度器（主题、风格、发型 LLM）
│   │   ├── generate_gptimage.py  # GPT Image 引擎
│   │   └── generate_gitee.py     # Gitee z-image-turbo 引擎
│   └── web/
│       └── index.html       # 单文件前端（HTML+CSS+JS）
├── config/
│   └── config.yaml          # 主配置
└── data/                    # 运行时数据（自动生成）
    ├── schedule_data.json   # 日程 + 图片条目数据库
    ├── api_keys_config.json # API 密钥存储
    └── images/              # 生成的图片
```

## 🎯 生图引擎

| 引擎 | 速度 | 质量 | 适用场景 |
|------|------|------|----------|
| **GPT Image** | 40-60s | ⭐⭐⭐⭐⭐ | 日常首选，高质量写真 |
| **Gitee z-image-turbo** | 12s | ⭐⭐⭐ | 快速出图、性感风格 |

默认优先使用 GPT Image，并按重试机制处理失败；只有在设置里开启 Gitee 回退时，GPT Image 多次失败后才会改用 Gitee。

### AxonHub 适配推荐

推荐通过 [AxonHub](https://github.com/looplj/AxonHub) 统一管理多个生图渠道：

1. 部署 AxonHub 并配置多个图像生成 channel（GPT Image / Gemini / 自定义）
2. 在画廊的 Web 设置面板填入：
   - **GPT Image Base URL**: `http://your-axonhub-host:port/v1`
   - **GPT Image API Key**: AxonHub 的 API key（`ah-xxxxx` 格式）
3. AxonHub 会自动按优先级 + 负载均衡路由请求

**优势**：
- 多渠道自动降级（一个挂了自动换下一个）
- 统一鉴权和请求日志
- 按模型名路由（`gpt-image-2` / `gemini-3.1-flash-image`）

## ⏰ 调度说明

**日程驱动动态调度**：

1. **07:00** — LLM 自动生成当日穿搭日程（HH:mm 格式，如 `09:30 逛街`、`14:00 下午茶`）
2. **解析日程** — 提取所有 HH:mm 时间，根据小时映射主题：
   - `< 12` → `morning`（甜妹风）
   - `12-17` → `noon`（少女风）
   - `18-20` → `evening`（冷御风）
   - `≥ 21` → `bedtime`（慵懒风）
3. **动态创建任务** — 为每个时间点创建一次性 APScheduler 任务
4. **按时生图** — 到达指定时间后自动执行：读取日程 → LLM 选风格/发型 → 调用引擎 → 保存图片 → 推送微信

**优势**：不再固定 4 个时段，完全由 LLM 日程决定生图时间和数量，每天可能 2-5 张不等。

## 🔌 API 端点

### 画廊数据

```bash
# 获取今日照片
curl http://localhost:18889/api/today

# 获取全部照片
curl http://localhost:18889/api/gallery

# 健康检查
curl http://localhost:18889/api/health
```

### 生图操作

```bash
# 立即生图
curl -X POST http://localhost:18889/api/generate-now \
  -H "Content-Type: application/json" \
  -d '{"theme": "evening"}'

# 自定义 prompt 生图
curl -X POST http://localhost:18889/api/generate-custom \
  -H "Content-Type: application/json" \
  -d '{"prompt": "穿着白色连衣裙在樱花树下", "size": "1024x1536"}'
```

**theme 可选值**：`morning` / `noon` / `evening` / `bedtime` / `sexy` / `custom`

### 主题日日程

主题日支持当天或第二天，自定义主题留空时可随机抽取；开启 Web 设置中的小红书日程模式后，会先搜索匹配主题的真人穿搭，再让视觉 LLM 围绕参考图生成整天计划。

```bash
# 当天指定主题
curl -X POST http://localhost:18889/api/theme-day \
  -H "Content-Type: application/json" \
  -d '{"target":"today","mode":"custom","theme":"霍格沃兹体验日"}'

# 第二天随机主题
curl -X POST http://localhost:18889/api/theme-day \
  -H "Content-Type: application/json" \
  -d '{"target":"tomorrow","mode":"random"}'

# 读取第二天已生成的主题日详情
curl "http://localhost:18889/api/schedule-detail?date=YYYY-MM-DD"
```

### Hermes 安全升级 API

Hermes 可以直接调用下面的接口完成检查和一键升级；升级只更新仓库代码，会跳过本地密钥、配置、画廊图片、参考图和运行时数据。

```bash
# 检查最新版本
curl http://localhost:18889/api/hermes/check-update

# 预览本次会更新/跳过哪些文件，不重启
curl -X POST http://localhost:18889/api/hermes/update \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'

# 执行安全升级，成功后服务自动重启
curl -X POST http://localhost:18889/api/hermes/update \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false, "restart": true}'
```

如果旧版本（尤其是 `v1.2.3`）在 Web 一键升级时返回本机/认证相关错误，请在部署机器执行手动安全升级命令：

```bash
cd /path/to/portrait-gallery
git fetch origin main
git checkout origin/main -- VERSION README.md Dockerfile docker-compose.yaml app
./app/run_launch.sh
```

Docker 部署可将最后一行换成：

```bash
docker compose up -d --build
```

如果检查更新报 `Attempt to decode JSON with unexpected mimetype: text/html`，通常是旧本地配置把 GitHub 更新地址写成了仓库网页。升级到 `v1.2.6` 后会自动兼容；旧版本可先把 `config/config.yaml` 里的 `update.github_api` 清空，或改成：

```yaml
update:
  github_api: https://api.github.com/repos/OWNER/REPO/releases/latest
```

受保护路径包括：`.env`、`config/config.yaml`、`config/local.yaml`、`docker-compose.override.yml`、`data/`、`app/data/`、`logs/`、`app/references/uploads/`。

### 图片管理

```bash
# 切换收藏
curl -X POST http://localhost:18889/api/images/{img_id}/favorite

# 删除图片
curl -X DELETE http://localhost:18889/api/images/{img_id}
```

### 配置管理

```bash
# 获取 API 密钥状态
curl http://localhost:18889/api/config/keys

# 保存 API 密钥
curl -X POST http://localhost:18889/api/config/keys \
  -H "Content-Type: application/json" \
  -d '{
    "gitee_url": "https://ai.gitee.com/v1/images/generations",
    "gitee_key": "gitee-xxx",
    "gpt_base_url": "https://your-gpt-image-endpoint/v1",
    "gpt_key": "sk-xxx"
  }'
```

## 🔑 环境变量

| 变量 | 说明 |
|------|------|
| `CPA_API_KEY` | CPA 代理 API Key（覆盖 config） |
| `GITEE_API_URL` | Gitee 生图完整端点；默认 `https://ai.gitee.com/v1/images/generations` |
| `GITEE_API_KEY` | Gitee 生图 API Key（覆盖 Web 配置） |
| `GPT_IMAGE_API_KEY` | GPT Image API Key（覆盖 config） |
| `GPT_IMAGE_BASE_URL` | GPT Image 基础地址，通常填写到 `/v1`（覆盖 config） |
| `GITHUB_PROXY` | GitHub 更新检查/在线更新代理（也可在 Web 设置中填写） |
| `GITHUB_REPOSITORY` | GitHub Release 检查仓库，格式 `OWNER/REPO` |
| `GITHUB_RELEASE_API` | GitHub Release API 完整地址，优先级高于 `GITHUB_REPOSITORY` |
| `GALLERY_PASSWORD` | Web UI 访问密码；留空时首次本机打开页面设置 |
| `PORTRAIT_GALLERY_IMAGE` | Docker Compose 使用的镜像名/标签，默认 `hermes-portrait-gallery:latest` |
| `SOCIAL_HUB_IMAGE` | Social Hub Compose 使用的镜像名/标签，默认 `hermes-portrait-gallery-social-hub:latest` |
| `SOCIAL_SERVER_TOKEN` | 共享动态中心令牌；Hub-only 进程必填、须为 24–256 个可打印 ASCII 字符；直接运行时可用 |
| `SOCIAL_SERVER_TOKEN_FILE` | 共享动态中心令牌文件；Compose 默认挂载 `./secrets/social_server_token` |
| `SOCIAL_HUB_DATA_DIR` | Hub-only 数据目录，容器默认 `/app/data` |
| `SOCIAL_HUB_HOST` | Hub-only 监听地址，默认仅监听 `127.0.0.1` |
| `SOCIAL_HUB_PORT` | Hub-only 监听端口，默认 `18889` |

Docker Compose 会读取本地 `.env` 并把 `GALLERY_PASSWORD` 传入容器；不要把 `.env` 提交到仓库。

远程命令行访问受保护 API 时，需要保存登录返回的签名会话 Cookie：

```bash
curl -c /tmp/portrait-gallery.cookies \
  -X POST http://localhost:18889/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your-gallery-password"}'

curl -b /tmp/portrait-gallery.cookies \
  http://localhost:18889/api/gallery
```

脚本也可以从登录响应读取 `session_token`，后续通过 `X-Gallery-Session` 或
`Authorization: Bearer SESSION_TOKEN` 请求，不需要依赖固定来源 IP。

### Hermes 生图文案

Hermes 调用 `/api/generate-custom`、`/api/hermes/text-to-image` 或 `/api/hermes/image-to-image` 时，可在请求体传入 `caption`、`thought`、`small_thought`、`copy`、`copywriting` 或 `message`。画廊不会为 Hermes 图片另行生成小心思，会直接把该字段写入卡片 `caption` 并在画廊里展示。

## 📱 微信推送

生图完成后自动通过 `hermes send --to weixin` 推送到微信：
- 先发送图片（`MEDIA:/path/to/image.png`）
- 再发送文案（caption）

无需额外配置，确保 hermes CLI 已登录微信即可。

## 🖥️ 前端功能

- **今日 Tab** — 横版大卡片，直接展示穿搭/日程/caption，今日生图计划可双击编辑活动内容
- **全部 Tab** — 响应式网格，卡片支持收藏、重抽、编辑和删除
- **收藏 Tab** — 显示全图库真实收藏总数，并一次展示全部收藏图片
- **衣柜 Tab** — 展示收藏穿搭方案和 GPT 生成的衣架参考图，支持编辑、重生和图生图引用
- **角色 Tab** — 管理本地角色、人设、外貌、绑定模型、单人照、设定图和多角色合照
- **群聊 Tab** — 创建群聊房间、编辑参与角色、保存消息、删除/清空上下文、回溯重发回复和触发群聊图片生成
- **🎀 穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- **⚙️ 设置** — Web UI 管理 API 密钥、三级 LLM 模型链、Gitee 回退、日程风格和升级选项

## 🧾 Release Notes

### v1.3.10

- 新增可选 LLM 流式响应开关（默认关闭），适合 Grok 等长请求，避免代理空等超时。
- OpenAI-compatible SSE 会缓冲成现有 completion 结构，并处理流式错误。
- 流式内容按原始 UTF-8 解码，并修复 Latin-1 mojibake，避免中文日程校验失败。
- Grok 日程 JSON 请求默认关闭 reasoning，保留兼容重试。
- Web 设置页可直接开关流式响应；相关 runtime 配置与回归测试已补齐。

### v1.3.9

- 跨日 00:00–01:59 夜间尾巴任务按正确日程日恢复，引用/配额/失败记账不再串日。
- 日程多样性改为软约束提示，不再因语义重复直接否决整份计划。
- GPT Image 增加 unavailable_channel / reference_unavailable 分类，渠道与参考图失败更稳。
- 前端长请求统一 fetchWithTimeout，减少生图/重抽/衣架操作挂死。
- 在线升级与并发写路径更稳，补齐 overnight / diversity / failover 回归测试。
- 新增 scripts/release.sh 一键发布 GitHub Release 与多架构 Docker 镜像。

### v1.3.8

- 小心思过滤加强：拦截英文任务复述 / instruction leak，强制中文正文，坏文案自动回退本地 fallback。
- 参考图表情守卫加强：禁止复制嘟嘴 / duck face / pout 嘴型，甜妹风 reference prompt 改为自然放松嘴型。
- 今日日程标题旁显示生成模型标识（含历史「模型未知」兜底）。
- 生图时段覆盖：早 06–11:59 / 中 12–13:59 / 午 14–18:59 / 晚 19–01:59 至少各有一条；00:00–01:59 归次日执行。
- 旧 `zhuzhu_*` 参考预览 404 修复：版本 API / 本地 refs 归一化，避免失效路径。
- 图片版本切换与重抽替换链路更稳，相关回归测试补齐。

### v1.3.7

- 自定义生图支持有序多参考图：第一张锁动作/穿搭/场景，后续参考只迁脸与发色；API/UI 均可传 ref_images。
- 双参考上游失败自动降级为 face-only；multi-ref 对 timeout / Codex images/edits EOF 快速失败，日志带 kind 与 refs。
- 自定义生图轻量 prompt 路径：场景优先，避免过长 quality 堆叠导致崩图；日程 photo_style_en 由 LLM 按当日氛围判断摄影语言。
- 「现在生图」受每日计划配额约束；重抽改为替换原卡片并保留历史版本，支持版本回退。
- 衣柜衣架图支持历史版本归档/切换；参考像标签改为悬停缩略图，不再展示冗长文件名。
- 日程生成加强近期动作去重（LLM 判断近 3 天），可出现双人互动日程，但生图强制单主体入镜。
- 图片版本激活、翻译超时与 GPT Image 失败分类（moderation/timeout/EOF）更稳健。

### v1.3.6

- 全部画廊改为滚动接近底部时自动加载下一页，并加入旧浏览器滚动回退与网络失败冷却，避免重复请求风暴。
- 精准编辑与编辑后重抽会保留被替换的旧图；卡片详情可查看历史图片版本，删除卡片时同步清理对应归档文件。
- 自定义生图新增自动、常用比例和自定义宽高三种尺寸模式，扩充 3:2、16:9、21:9 等比例并统一前后端边界校验。
- 日程图片按活动意图选择普通人像、明确 OOTD 或风景构图，普通生活照更稳定地避免高机位、远景和无意全身构图。
- 收藏按钮可在当前列表内即时同步状态；衣柜卡片和操作按钮针对手机端重新排布，长穿搭说明不再挤压操作区。
- 群聊回复增加内部推理文本识别与兼容重试，生图进度状态补充失败后的最终错误信息。

### v1.3.5

- 画廊分页改用稳定游标，删除已加载卡片后不会漏图；收藏角标使用服务端真实总数，收藏 Tab 会自动取完并直接展示全部收藏图片。
- 图库卡片恢复“重抽”按钮，精准编辑支持后台运行和日程说明同步修改；提交前会核验源卡版本，避免旧编辑结果覆盖重抽结果或复活已删除卡片。
- 今日穿搭方案支持直接编辑发型与穿搭，并同步更新仍未执行的生图任务；衣柜参考资料、生成中状态和首次底模引导的持久化更稳健。
- 定时图片发送新增 `pending / sending / sent / failed` 持久状态，服务重启可把已生成但未送达的原图恢复为手动重发，不会重复生图。
- Web 认证从按 IP 授权升级为签名会话 Cookie/Header，密码变化会使旧会话失效；运行时模型与集成配置写入独立原子文件，局域网域名访问继续受同源保护。
- 在线升级改为稳定快进并保留无关文件的 staged 状态；图库、参考资料和元数据继续使用锁内原子更新。
- 真实日期提示会区分完整官方日历年份与零散自定义日期；自建年度假期表需在 `schedule.calendar.complete_years` 明确声明完整年份。
- 设置、日志、图片编辑弹窗补充焦点管理和触控尺寸，移动端风格标签改为横向滚动且卡片长标签会安全截断。

### v1.3.4

- 日程生成接入真实日期上下文与三天内去重校验，识别周末、法定节假日和调休工作日，减少休息日上班/上学以及连续赖床、做饭等模板化安排。
- 衣柜、收藏穿搭和关键词云继续作为低权重软偏好：每日轮换少量候选，可完全忽略，并结合“不喜欢”反馈避免照搬旧穿搭、旧场景和连续命中同一偏好。
- 新增画廊精准编辑与原位替换，支持仅修改背景、穿搭、发型、表情或道具；编辑结果重抽会使用当前图片继续图生图，并保留编辑目标、指令和历史。
- 日程与生图恢复链路增强：启动缺失日程会后台补生成，并发刷新合并为单任务；过期计划不自动补拍但可手动重试，日程图片统一限制为 3:4，图库文件名改用实际时间与活动语义。
- 微信发送增加串行冷却、限流重试和上下文失效识别，日志会给出可执行提示；局域网域名访问可正常控制 API 和读取日志。
- 生图配置统一：Gitee 环境变量优先且恢复 HTTPS 证书校验，GPT Image 聊天端点 fallback 与 Gitee fallback 均保持显式开启，默认 URL 与有效覆盖值按最终配置校验。
- 安全与持久化加固：远程请求不能伪造 `Host: localhost` 绕过保护，参考图上传限制 10 MiB 并校验真实图片，图库/元数据改为锁内原子更新，在线升级会阻止覆盖本地修改，API Key 不再进入 URL 或明文日志。
- 修复配置写入失败误报成功、DeepSeek 网络错误额外兼容重试，以及单角色图片被误标为群聊等问题。

### v1.3.3

- Web 访问控制切换为访问密码方案：支持 `GALLERY_PASSWORD` 环境变量、首次本机 Web 设置密码、非本地访问登录，以及已登录 IP 持久化；移除旧版 `GALLERY_API_KEY` / `X-API-Key` 入口。
- 远程访问会保护 API、生成图片和本地参考图；Docker Compose 支持从本地 `.env` 透传 `GALLERY_PASSWORD`，避免把部署密码写进仓库。
- 群聊图片生成增加后台任务与进度状态，前端可持续展示当前角色/阶段；取消等待不会中断后台生图，图片消息删除会同步清理画廊图片和元数据。
- 角色与群聊生图提示词改为 image-only 构建，并净化未成年、校服、露骨身体描述和挑逗语义，降低上游安全拒绝概率。
- GPT Image 失败处理更明确：额度用完、工作区停用、无可用账号/渠道等终止错误不再盲目重试；聊天端点 fallback 默认关闭，仅显式开启或显式 chat endpoint 时使用。
- 日志视图增强：保留完整生图提示词细节，过滤成功访问噪声，解释微信上下文失效和生图引擎回退原因，方便排查真实故障。
- 日程与参考图体验补强：调度历史去重更稳定，衣柜/默认参考图上传与回退链路增加测试覆盖。

### v1.3.2

- 今日生图计划支持在 Web UI 中双击活动内容直接编辑，回车或失焦保存，Esc 取消。
- 新增 `/api/photo-jobs/plan` 接口，用于安全更新当天指定 `HH:mm` 计划项，并校验时间、空内容和长度。
- 计划编辑会同步写回当天 `schedule`、`schedule_prompt`、`schedule_details`，并更新仍未执行的 APScheduler 生图任务参数。
- 生图计划列表优先展示已保存日程里的活动内容，让手动调整在“已完成/待执行/失败重试”状态间保持一致。
- 增加计划编辑 helper 单元测试，覆盖日程文本替换和 stale `schedule_details` 字段清理。

### v1.3.1

- 新增关键词偏好云：从用户输入的自定义生图 prompt 和收藏衣柜中提取高频穿搭/场景偏好，并作为每日穿搭日程的柔性参考。
- 每日日程生成加入近期历史去重，减少连续出现“赖床/刷手机/做饭”等重复模板，让当天安排更有变化。
- 群聊上下文管理增强：支持删除单条消息、清空上下文，并可对已生成回复执行回溯重发/重新生成。
- 公共发布默认值进一步收敛：Docker Compose 使用中性本地镜像名，GitHub Release 检查支持 `GITHUB_REPOSITORY` / `GITHUB_RELEASE_API` 配置，文档与代码默认值避免绑定个人账号。
- 内置写真风格 prompt 做了温和化处理，降低因模板过于显式而被上游图像服务拒绝的概率。

### v1.3.0

- 新增多角色系统：支持从运行时人设、配置和本地 `data/characters.json` 组合角色，Web UI 可新增、编辑、删除角色，并为角色生成单人照、设定图和合照。
- 新增群聊工作台：支持群聊房间、参与者绑定、消息持久化、角色自动回复、撤回重跑，以及由群聊回复触发图片生成。
- 设置页改为三级 LLM 模型链管理，主模型、备用模型和第三级模型会统一写入 `llm.models`，日程、caption、群聊、Hermes 描述和“现在在干嘛”都会按链路 fallback。
- 日程生成加入真实日期上下文，内置 2026 法定节假日和调休上班日，并支持配置自定义假期/调休日，休息日会避免上班、上课、考试等冲突安排。
- 生图参考链路强化：单人照、合照和群聊图可复用今日日程参考图，图生图提示词明确区分脸部参考和衣柜穿搭参考，并避免照抄参考图表情。
- 增加中文 mojibake 文本修复、caption 指令泄漏过滤、LLM 请求重试与模型不可用快速切换，减少乱码、空文案和坏模型拖慢整条链路。
- Docker 镜像布局调整为保留仓库内 `app/` 目录，Compose 默认使用本地镜像名，并可通过 `PORTRAIT_GALLERY_IMAGE` 指定任意发布镜像。

### v1.2.6

- 兼容旧本地配置把 GitHub 更新地址写成仓库网页 URL 的情况，自动转换为 Releases API。
- 修复检查更新遇到 `text/html` 响应时抛出 `Attempt to decode JSON with unexpected mimetype` 的问题，改为明确提示配置修复方式。

### v1.2.5

- 修复 Docker/localhost 场景下一键安全升级可能被误判为远程写操作并返回本机认证错误的问题。
- 检查更新改为只读 GET 请求；升级被鉴权拦截时会显示手动安全升级命令。
- 优化移动端顶部 Tab 和卡片底部 `收藏 / 分享 / 删除` 按钮尺寸。

### v1.2.4

- 每日日程生成改为凌晨 `03:00-06:00` 随机窗口执行；窗口内生成失败会自动重试，过了窗口不再白天补跑。
- `03:00-05:59` 作为日程生成静默时段，不安排也不执行自动生图；日程生图时间会避开整点并自然上下浮动。
- 设置页移除固定的 GitHub Release API URL 输入项，检查更新由后端根据配置或环境变量解析当前仓库。

### v1.2.3

- 设置页的 GitHub Release API URL 改为当前仓库固定值，不再要求用户手动填写，旧本地覆盖会在保存设置时自动清理。
- 检查更新支持 `GITHUB_REPOSITORY` / `GITHUB_RELEASE_API` 或 `update.github_repo` / `update.github_api` 配置，便于特殊部署和 fork。
- 默认画质提示词改为更自然的手机随拍风格，减少过度精修、塑料皮肤和 AI 感。

### v1.2.2

- 强化 LLM 日程 JSON 输出链路：增加 strict JSON 输出协议、JSON 修复重试和 schedule_details 结构兼容，避免模型输出说明文字时直接失败。
- 日程生成恢复 full → compact → emergency 逐级降级，保留历史穿搭、收藏/不喜欢反馈和完整人设口吻，只在失败后使用极简 prompt。
- Hermes/API 生图支持写入调用方文案和中文穿搭展示描述；无中文描述时短超时尝试 LLM 压缩，失败立即使用本地 fallback，不阻塞生图链路。
- 自定义生图和 Hermes 生图元数据补充 `display_outfit` / `outfit_description`，画廊详情优先展示中文穿搭描述和衣柜参考标签。
- 运行诊断日志隐藏已被后续成功覆盖的旧 LLM 原始错误，并保留真实请求超时/连接失败原因。
- 衣柜页优化为一行 4 套，衣柜参考只在点击“用它生图”后进入自定义生图参考区。
- 小心思 caption 统一选择可用文案，避免单字/空文案覆盖 gallery 已有内容。

### v1.2.1

- 日程生成失败时不再把本地 fallback 伪装成真实日程，避免 LLM 不可用时误触发定时/即时生图。
- 统一日程可用性判断：刷新、今日生图、generate-now 和子进程生图都会排除 `source=fallback` 的历史脏数据。
- 修复发色优先链路：appearance 发色优先，日程发型只决定发型/发饰，并收紧 `light/dark` 发色清洗误伤。
- 设置页重启回归 Python 模式，`app/run_launch.sh` 会自动加载本地 `.venv`、配置和数据目录，重启不修改本地 Key/URL。
- 模型测试增加多次轮询和 `temperature` 不兼容自动重试，便于 AxonHub/第三方 OpenAI-compatible 模型排查。

### v1.2.0

- 自定义生图模型链路修复：Agnes 模型会显示为 Agnes，Images API 失败后不再误切到 chat-compatible GPT Image 链路。
- 定时日程、现在在干嘛和日程重抽不再把衣柜衣架图当作直接图生图底图；衣柜仅作为发型/穿搭风格偏好参考。
- 实时诊断日志持久化到 `logs/gallery.log`，重启不丢失，并自动清理 3 天前轮转日志。
- 诊断弹窗只展示最新 3 条原始错误，原始错误区域单独上色，`content_policy_violation` 等错误会中文化提示。
- 微信/Hermes 推送增加串行队列和 iLink cooldown 识别，图片已送达但文案被限流时不再把整次推送标红。
- 生图失败、来源、模型名和文生图/图生图元数据展示继续收敛，便于排查 Hermes/API、自定义生图和日程生图链路。

### v1.1.9

- 新增衣柜 Tab：收藏穿搭方案会集中展示，并可自动生成包含衣服、配饰和假发的衣架参考图，方便后续图生图。
- 衣柜支持发型/穿搭编辑、衣架图重生、生成中软刷新和“用它生图”，减少生成过程中的闪屏和位置跳动。
- 参考图链路从硬编码 cool / girly / sweet 改为参考图 profile：默认参考图写入 prompt，上传图会经 LLM 识图生成 prompt，定时生图由 LLM 选择匹配参考图，无明确匹配时随机兜底。
- 定时生图、现在在干嘛和重 roll 链路继续对齐今日日程上下文，图片条目只保存单图 `schedule_time`，不再把全天计划塞进卡片详情。
- 新增实时诊断日志入口，错误信息保留原始错误，其余尽量中文化，便于排查本地 GPT Image 中转、Gitee 回退和生图任务失败。
- GPT Image Base URL 改为填写到 `/v1` 即可，自动适配 Images API 或 chat 兼容生图中转；Hermes/API 生图来源和文案展示继续优化。

### v1.1.8

- 自定义穿搭生成新增模型选择，会从 GPT Image 与 CPA/AxonHub 的 `/models` 列表读取 Agnes、Grok、Gemini 等可用生图模型。
- Hermes/API 生图链路优化：Hermes 传入的文案会直接写入画廊小心思，来源、文生图/图生图和视角信息展示更清晰。
- 日程生图要求 LLM 输出每个时间段的动作、场景、服饰和发型明细，并加强时间约束，减少白天活动被生成成夜景的问题。
- “现在在干嘛”和重抽链路改为更严格复用今日日程上下文，重抽会替换原卡片信息与图片，不再只换图或额外生成新卡。
- 自定义自拍/半身/全身视角继续优化，横图也会保留人物完整构图和动作空间；手机端网格列数适配更灵活。
- 新增 Hermes 安全升级 API，并加固远程写接口、参考图路径、Picxazz 同步默认值、Hermes 图片校验和元数据并发写入。

### v1.1.7

- 今日卡片和弹窗新增重抽入口，重抽会在原卡片上替换图片，不再额外插入新卡。
- 生图计划和日程展示进一步对齐：只显示实际照片计划里的时间段，计划完成后不再追加未来项。
- 优化“小心思”和 caption 口吻，减少照抄日程、重复“画廊现场感”等模板化文案。
- 全部 Tab 支持双击切换“只看未收藏”，删除卡片后保持当前浏览位置，避免列表重绘时乱跳。
- 画廊右下角新增回到顶部/底部快捷按钮，并优化列数滑块附近的浮动控件布局。

### v1.1.6

- 新增“收藏穿搭方案”，后续日程生成会把用户收藏的发型、服饰气质、配色、版型和材质作为软偏好参考。
- 定时生图改为更尊重 LLM 日程里的动作、场景、道具和时间氛围，减少代码模板覆盖今日计划的问题。
- 画廊卡片补充图片尺寸、文件大小和生成耗时等 metadata，网格按钮和列数控制在窄屏下更稳定。
- 优化定时照片重抽：按原日程重新生成时保留新图自己的 caption，不再被旧图文案覆盖。
- 修复手动补拍/重试绕过每日生图计划上限的问题，并恢复“现在在干嘛”对 `reasoning_content` LLM 响应的兼容。

### v1.1.5

- 生图子进程超时改为按 `process_timeout` 或重试、文生图/图生图、caption 等配置动态计算，长耗时图生图不再被固定 900 秒提前截断。
- 定时生图和“现在在干嘛”会把具体日程传给 caption 生成，文案更贴合当前时间、地点和活动，避免和日程冲突。
- GPT Image 图生图失败时自动回退到纯文生图，并在画廊数据和图片 metadata 中记录请求模式、实际模式、参考图和 fallback 状态。
- 今日自动生图底模会优先复用当天日程或已完成 cron 图片的 `base_style`，让同一天的自动照片风格更稳定。
- 画廊同步新增参考图 URL 映射，内置参考图和本地上传参考图会写入可展示的相对路径。

### v1.1.4

- 自定义穿搭生成新增比例、清晰度和自拍/半身/全身视角选择，并按目标尺寸保存输出图。
- 新增画廊图片重抽入口，可基于已有最终 prompt 重新生成一张新图并保留原卡片上下文。
- 日程生成新增 `base_style` 底模选择，让 LLM 在 cool / girly / sweet 中为当天风格选择参考底模。
- 日程彩蛋的生图上限改为按已完成、待执行、运行中和失败待重试的计划槽位统一统计，避免重复补拍。
- 推送渠道支持在 Web 设置中选择微信或 TG，并按人设来源自动优先使用 Hermes / OpenClaw。
- 新增 Hermes 纯净文生图/图生图 API，图生图只允许使用受控参考图目录，避免任意本地路径被读取。
- GPT Image 兼容 `/chat/completions` 和显式 `/images/generations` / `/images/edits`，保留裸 `/v1` 自动拼接 chat endpoint 的旧行为。
- 优化 caption 防人设泄露、旅行/机场/机舱场景 prompt、移动端弹窗和设置面板交互。

### v1.1.3

- 新增人设来源设置，支持从 Hermes、OpenClaw 或自定义文本读取角色人设，并让小心思、日程文案和生图外貌提示词使用同一套运行时人设。
- 收敛穿搭风格映射与参考图底模映射，风格池可在彩蛋弹窗内直接调整，避免计划风格和实际生成风格显示错位。
- GPT Image Base URL、CPA Base URL、GitHub API 代理等设置支持本地持久化并在 Web 设置面板展示当前来源状态。
- 新增图片存放位置设置和旧图清理功能，可按 3 天、7 天、1 个月、3 个月或自定义天数清理非收藏图片。
- 优化画廊和今日卡片布局、设置面板排版、Gitee 回退开关开启态对比，以及 TG/聊天生图在全部列表中的展示回填。
- 生图链路参数和 LLM 参数进一步配置化，更新流程保留本地 API Key、appearance、画廊图片和参考图。

### v1.1.2

- 设置面板新增 GitHub API 代理地址，可用于修复检查更新时的 `403`；代理只保存在本机运行数据里，不写死到仓库配置。
- 检查更新和在线更新都会优先使用 GitHub 代理配置，`git pull` 更新时同步注入代理环境变量。
- 日程彩蛋支持失败/已过未排任务重试，并展示更清晰的生图失败原因。
- 优化今日卡片自适应布局，避免图片把卡片撑高导致按钮悬在中间、底部留白过大。

### v1.1.0

- 修复 Gitee 回退开关未生效的问题：未勾选时 GPT Image 失败不会再自动生成 Gitee 图片。
- GPT Image 生图失败会先按重试策略尝试多次，只有启用 Gitee 回退后才会改用 Gitee。
- 优化 prompt 注入链路，避免画质前缀和人物外貌被重复注入，并让日程场景关键词参与最终 prompt。

### v1.0.9

- 参考图上传改为持久化保存到本地 `data/references/uploads/`，Web UI 后续打开会直接从本地文件列表恢复展示。
- 自定义生图会把 Web UI 选择的参考图 URL 安全解析为本地文件路径，内置参考图和用户上传图都能走同一生成链路。
- 启动时兼容迁移旧版 `app/references/uploads/` 中的历史上传参考图。

### v1.0.7

- 修复 Python 3.9 本地运行时对 `dict | None` 类型注解不兼容导致的启动失败。
- 修复开启旧版 Web 访问密钥后主接口未带认证导致首页、设置、生图等功能不可用的问题。
- 优化在线更新接口：`git pull` 成功后先返回响应，再延迟重启，避免前端误报更新失败。
- 补全旧图片条目的展示数据：从 `image_metadata.json` 回填完整 prompt、规范模型名、修复误显示为画质 prompt 的穿搭字段，并用当日日程 caption 做合理回填。
- 生成链路写入 caption、model、source 等 gallery 字段，让今日/全部/彩蛋视图展示更完整。

## 🤖 AI Agent 集成

本项目提供 `SKILL.md` 文件，AI Agent 读取后可直接通过 REST API 操控画廊：

- 自动生成穿搭并生图
- 查询画廊内容
- 管理图片（收藏/删除）
- 配置 API 密钥

适合集成到 Hermes、OpenClaw 等 AI Agent 框架。

## 📝 License

MIT
