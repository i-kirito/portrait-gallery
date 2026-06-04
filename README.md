# 🎀 Portrait Gallery

> AI 穿搭生图 & 个人画廊系统 —— 让 AI 每天为你量身定制穿搭方案并自动生成写真

一个基于 LLM 日程驱动的 AI 个人形象生成与展示系统。每天自动生成穿搭日程，按时调用 AI 生图引擎生成写真照片，通过 Web 画廊展示和管理。

## ✨ 功能亮点

- 📅 **LLM 日程驱动** — DeepSeek 自动生成每日穿搭日程（HH:mm 精度），按时触发生图
- 🎨 **双引擎生图** — GPT Image 高质量 + Gitee z-image-turbo 极速，自动降级
- 🖼️ **Web 画廊** — 今日/全部/收藏三 Tab，横版大卡 + 网格双布局
- 🎀 **穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- ⏰ **定时调度** — APScheduler 内置 5 个时段自动生图（早/午/晚/睡前）
- 🔧 **REST API** — 完整 CRUD 接口，支持集成到任何 AI Agent

## 🚀 快速开始

### 1. 克隆

```bash
git clone https://github.com/i-kirito/portrait-gallery.git
cd portrait-gallery
```

### 2. 安装依赖

```bash
pip install aiohttp apscheduler pyyaml requests
```

### 3. 配置

编辑 `config/config.yaml`，填入你的 API 密钥：

```yaml
llm:
  base_url: "http://127.0.0.1:8327/v1"   # CPA 代理地址（用于 LLM）
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

生图失败时自动从 GPT Image 降级到 Gitee，无需手动干预。

## ⏰ 调度说明

APScheduler 内置 5 个定时任务：

| 时间 | 主题 | 风格 |
|------|------|------|
| 07:00 | 📅 日程生成 | LLM 生成当日穿搭日程 |
| 07:01 | 🌅 morning | 甜妹风 |
| 12:00 | ☀️ noon | 少女风 |
| 18:00 | 🌆 evening | 冷御风 |
| 22:00 | 🌙 bedtime | 慵懒风 |

生图流程：读取日程 → LLM 选风格/发型 → 调用引擎 → 保存图片 + 元数据 → 更新画廊

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
| `GALLERY_API_KEY` | Web UI 认证密钥（留空则不认证） |
| `TELEGRAM_CHAT_ID` | Telegram 通知 Chat ID（可选） |

## 🖥️ 前端功能

- **今日 Tab** — 横版大卡片，直接展示穿搭/日程/caption，点击图片全屏查看
- **全部 Tab** — 6 列网格，点击弹窗查看详情（收藏/分享/删除）
- **收藏 Tab** — 筛选已收藏图片
- **🎀 穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- **⚙️ 设置** — Web UI 管理 API 密钥

## 🤖 AI Agent 集成

本项目提供 `SKILL.md` 文件，AI Agent 读取后可直接通过 REST API 操控画廊：

- 自动生成穿搭并生图
- 查询画廊内容
- 管理图片（收藏/删除）
- 配置 API 密钥

适合集成到 Hermes、OpenClaw 等 AI Agent 框架。

## 📝 License

MIT
