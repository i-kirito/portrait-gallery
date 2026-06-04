---
name: portrait-gallery
description: "AI outfit generation & portrait gallery system. Daily schedule-driven image generation with GPT Image + Gitee engines, web gallery UI, and REST API. Use when: (1) generate AI outfit/portrait images, (2) view/manage gallery photos, (3) set up schedule-driven image generation, (4) interact with portrait gallery API."
---

# Portrait Gallery — AI 穿搭生图 & 画廊系统

基于 LLM 日程驱动的 AI 个人形象生成与展示系统。每天自动生成穿搭日程，按时生图，Web 画廊展示。

**核心能力**：
- 🎨 双引擎生图：GPT Image (jiuuij.de5.net) + Gitee z-image-turbo
- 📅 LLM 日程驱动：DeepSeek 生成每日穿搭日程，按 HH:mm 时间点自动生图
- 🖼️ Web 画廊：今日/全部/收藏三 Tab，横版大卡+网格双布局
- 🔧 REST API：完整的 CRUD 接口，支持自定义生图、收藏、删除
- ⏰ APScheduler 内置调度：07:00 生成日程，07:01/12:00/18:00/22:00 自动生图

## 快速开始

### 1. 克隆仓库
```bash
git clone https://github.com/i-kirito/portrait-gallery.git
cd portrait-gallery
```

### 2. 配置 API 密钥
编辑 `config/config.yaml`，填入你的 API 密钥：
```yaml
zhuzhu:
  enabled: true
  cpa_base_url: "http://127.0.0.1:8327/v1"   # CPA 代理地址（用于 LLM 日程生成）
  primary_api_key: "your-cpa-api-key"          # CPA API Key
  gpt_image_base_url: "https://your-gpt-endpoint/v1/chat/completions"
  gpt_image_api_key: "your-gpt-image-key"
  gitee_api_keys:                              # Gitee z-image API Keys（支持多个轮询）
    - "your-gitee-key-1"
    - "your-gitee-key-2"
```

或启动后通过 Web UI（端口 18889）的 ⚙️ 设置面板填写。

### 3. 启动服务

**方式 A：直接运行**
```bash
pip install aiohttp apscheduler pyyaml requests
cd app
python3 main.py
```

**方式 B：macOS launchd 服务**
```bash
# 编辑 plist 中的路径，然后加载
launchctl load ~/Library/LaunchAgents/com.hermes.portrait-gallery.plist
```

**方式 C：Docker**
```bash
docker compose build && docker compose up -d
```

服务默认运行在 **http://localhost:18889**

## 架构

```
portrait-gallery/
├── app/
│   ├── main.py              # 入口：APScheduler 调度 + aiohttp 启动
│   ├── web_server.py        # REST API + 静态文件服务
│   ├── core.py              # 生图核心（同步、元数据、翻译）
│   ├── store.py             # 文件锁封装（并发安全读写 schedule_data.json）
│   ├── scheduler.py         # LLM 日程生成（调用 CPA DeepSeek）
│   ├── zhuzhu/
│   │   ├── core.py          # 生图底层（GPT Image / Gitee / Gemini 调用）
│   │   ├── generate.py      # 生图调度器（主题选择、发型LLM、风格LLM）
│   │   ├── generate_gptimage.py  # GPT Image 引擎
│   │   └── generate_gitee.py     # Gitee z-image-turbo 引擎
│   └── web/
│       └── index.html       # 单文件前端（HTML+CSS+JS 内联）
├── config/
│   └── config.yaml          # 主配置
└── data/                    # 运行时数据（gitignore）
    ├── schedule_data.json   # 日程+图片条目数据库
    ├── api_keys_config.json # API 密钥存储
    ├── plugin_config.json   # Gitee 插件配置
    └── images/              # 生成的图片
```

## API 端点

### 画廊数据

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/today` | 今日照片列表（含日程、穿搭、caption） |
| GET | `/api/gallery` | 全部照片列表 |
| GET | `/api/entries/{date}` | 指定日期条目 |
| GET | `/api/health` | 健康检查 |

### 生图操作

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/generate-now` | 立即生图（body: `{"theme":"morning"}`） |
| POST | `/api/generate-custom` | 自定义生图（body: `{"prompt":"...", "ref_image":"...", "size":"1024x1024"}`） |
| POST | `/api/generate` | 生成日程（不生图） |

**theme 可选值**：`morning`（甜妹风）、`noon`（少女风）、`evening`（冷御风）、`bedtime`（慵懒风）、`sexy`（性感风）、`custom`（自定义）

### 图片管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/images/{img_id}/favorite` | 切换收藏状态 |
| DELETE | `/api/images/{img_id}` | 删除图片 |

### 参考图管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ref-list` | 内置底模参考图列表（cool/girly/sweet） |
| GET | `/api/uploaded-refs` | 用户上传的参考图列表 |
| POST | `/api/upload-ref` | 上传参考图（multipart/form-data） |
| DELETE | `/api/uploaded-refs/{filename}` | 删除上传的参考图 |

### 配置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config/keys` | 获取 API 密钥状态（masked） |
| POST | `/api/config/keys` | 保存 API 密钥 |

## 使用示例

### 立即生成一张图
```bash
curl -X POST http://localhost:18889/api/generate-now \
  -H "Content-Type: application/json" \
  -d '{"theme": "evening"}'
```

### 自定义 prompt 生图
```bash
curl -X POST http://localhost:18889/api/generate-custom \
  -H "Content-Type: application/json" \
  -d '{"prompt": "穿着白色连衣裙在樱花树下", "size": "1024x1536"}'
```

### 获取今日照片
```bash
curl http://localhost:18889/api/today
```

### 切换收藏
```bash
curl -X POST http://localhost:18889/api/images/zhuzhu_evening_1749012345.png/favorite
```

## 调度说明

APScheduler 内置 5 个定时任务：

| 时间 | 任务 | 说明 |
|------|------|------|
| 07:00 | `daily_job` | LLM 生成当日穿搭日程（写入 schedule_data.json） |
| 07:01 | `photo_job(morning)` | 甜妹风晨间生图 |
| 12:00 | `photo_job(noon)` | 少女风午间生图 |
| 18:00 | `photo_job(evening)` | 冷御风傍晚生图 |
| 22:00 | `photo_job(bedtime)` | 慵懒风睡前生图 |

生图流程：读取日程 → LLM 选风格/发型 → 调用 GPT Image（失败降级 Gitee）→ 保存图片+元数据 → 更新画廊

## 生图引擎

| 引擎 | 速度 | 质量 | 适用场景 |
|------|------|------|----------|
| GPT Image (jiuuij.de5.net) | 40-60s | ⭐⭐⭐⭐⭐ | 日常首选，高质量 |
| Gitee z-image-turbo | 12s | ⭐⭐⭐ | 快速出图、sexy 主题强制 |

**降级链**：GPT Image → Gitee（自动降级，无需手动干预）

## 配置项说明

`config/config.yaml` 完整配置：

```yaml
server:
  host: "0.0.0.0"
  port: 18889

scheduler:
  timezone: "Asia/Shanghai"
  daily_job_hour: 7
  daily_job_minute: 0

zhuzhu:
  enabled: true
  cpa_base_url: "http://127.0.0.1:8327/v1"
  primary_api_key: ""
  gpt_image_base_url: ""
  gpt_image_api_key: ""
  gitee_api_keys: []
  style_pool:
    - "sweet daily outfit"
    - "elegant casual look"
    - "cozy streetwear"
    # ... 更多风格
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `CPA_API_KEY` | CPA 代理 API Key（覆盖 config） |
| `GPT_IMAGE_API_KEY` | GPT Image API Key（覆盖 config） |
| `GPT_IMAGE_BASE_URL` | GPT Image 端点（覆盖 config） |
| `GALLERY_API_KEY` | Web UI 认证密钥（留空则不认证） |
| `TELEGRAM_CHAT_ID` | Telegram 通知 Chat ID |

## 前端功能

- **今日 Tab**：横版大卡片，直接展示穿搭/日程/caption，点击图片全屏查看
- **全部 Tab**：6 列网格，点击弹窗查看详情（收藏/分享/删除）
- **收藏 Tab**：筛选已收藏图片
- **⚙️ 设置**：Web UI 管理 API 密钥
- **🎀 穿搭生成**：自定义 prompt + 参考图 + 尺寸选择

## 注意事项

- `data/` 目录包含运行时数据，首次启动会自动创建
- `schedule_data.json` 使用文件锁保护并发读写
- GPT Image 安全过滤器对部分场景不稳定，会自动降级到 Gitee
- 修改 `app/web/index.html` 后刷新浏览器即可生效（volume mount）
- 修改 `app/` 下 Python 文件需重启服务
