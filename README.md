# 🎀 Portrait Gallery

当前版本：**v1.2.3**

> AI 穿搭生图 & 个人画廊系统 —— 让 AI 每天为你量身定制穿搭方案并自动生成写真

一个基于 LLM 日程驱动的 AI 个人形象生成与展示系统。每天自动生成穿搭日程，按时调用 AI 生图引擎生成写真照片，通过 Web 画廊展示和管理。

## ✨ 功能亮点

- 📅 **LLM 日程驱动** — DeepSeek 自动生成每日穿搭日程（HH:mm 精度），按时触发生图
- 🎨 **多引擎生图** — 支持 OpenAI-compatible API (GPT Image / AxonHub / 自定义端点)、Gemini、Gitee z-image-turbo 可选回退（默认关闭）
- 🖼️ **Web 画廊** — 今日/全部/收藏/衣柜四 Tab，横版大卡 + 网格双布局
- 🎀 **穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- ⏰ **动态调度** — LLM 日程驱动，根据 HH:mm 时间动态创建一次性生图任务
- 🔧 **REST API** — 完整 CRUD 接口，支持集成到任何 AI Agent

## 🚀 快速开始

### 1. 克隆

```bash
git clone https://github.com/i-kirito/portrait-gallery.git
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

如果设置了 `GALLERY_API_KEY`，首次访问请带上密钥：

```text
http://localhost:18889/?key=your-gallery-api-key
```

前端会把密钥保存在浏览器 `localStorage`，后续同源 `/api/*` 请求会自动带上 `X-API-Key`。

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

## 📐 架构

```
portrait-gallery/
├── app/
│   ├── main.py              # 入口：APScheduler 调度 + aiohttp 启动
│   ├── web_server.py        # REST API + 静态文件服务
│   ├── core.py              # 生图核心（同步、元数据、翻译）
│   ├── store.py             # 文件锁封装（并发安全读写）
│   ├── scheduler.py         # LLM 日程生成
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
  -d '{"gpt_key": "sk-xxx", "gpt_base_url": "https://your-endpoint/v1/chat/completions"}'
```

## 🔑 环境变量

| 变量 | 说明 |
|------|------|
| `CPA_API_KEY` | CPA 代理 API Key（覆盖 config） |
| `GPT_IMAGE_API_KEY` | GPT Image API Key（覆盖 config） |
| `GPT_IMAGE_BASE_URL` | GPT Image 端点（覆盖 config） |
| `GITHUB_PROXY` | GitHub 更新检查/在线更新代理（也可在 Web 设置中填写） |
| `GALLERY_API_KEY` | Web UI 认证密钥（留空则不认证） |

启用 `GALLERY_API_KEY` 后，命令行 API 需要带认证：

```bash
curl -H "X-API-Key: $GALLERY_API_KEY" http://localhost:18889/api/gallery
# 或
curl "http://localhost:18889/api/gallery?key=$GALLERY_API_KEY"
```

### Hermes 生图文案

Hermes 调用 `/api/generate-custom`、`/api/hermes/text-to-image` 或 `/api/hermes/image-to-image` 时，可在请求体传入 `caption`、`thought`、`small_thought`、`copy`、`copywriting` 或 `message`。画廊不会为 Hermes 图片另行生成小心思，会直接把该字段写入卡片 `caption` 并在画廊里展示。

## 📱 微信推送

生图完成后自动通过 `hermes send --to weixin` 推送到微信：
- 先发送图片（`MEDIA:/path/to/image.png`）
- 再发送文案（caption）

无需额外配置，确保 hermes CLI 已登录微信即可。

## 🖥️ 前端功能

- **今日 Tab** — 横版大卡片，直接展示穿搭/日程/caption，点击图片全屏查看
- **全部 Tab** — 6 列网格，点击弹窗查看详情（收藏/分享/删除）
- **收藏 Tab** — 筛选已收藏图片
- **衣柜 Tab** — 展示收藏穿搭方案和 GPT 生成的衣架参考图，支持编辑、重生和图生图引用
- **🎀 穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- **⚙️ 设置** — Web UI 管理 API 密钥

## 🧾 Release Notes

### v1.2.3

- 设置页的 GitHub Release API URL 改为当前仓库固定值，不再要求用户手动填写，旧本地覆盖会在保存设置时自动清理。
- 检查更新默认使用 `https://api.github.com/repos/i-kirito/portrait-gallery/releases/latest`，仍保留环境变量或配置覆盖能力，便于特殊部署。
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
- 修复开启 `GALLERY_API_KEY` 后 Web UI 主接口未带认证导致首页、设置、生图等功能不可用的问题。
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
