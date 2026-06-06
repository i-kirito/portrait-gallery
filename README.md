# 🎀 Portrait Gallery

当前版本：**v1.0.7**

> AI 穿搭生图 & 个人画廊系统 —— 让 AI 每天为你量身定制穿搭方案并自动生成写真

一个基于 LLM 日程驱动的 AI 个人形象生成与展示系统。每天自动生成穿搭日程，按时调用 AI 生图引擎生成写真照片，通过 Web 画廊展示和管理。

## ✨ 功能亮点

- 📅 **LLM 日程驱动** — DeepSeek 自动生成每日穿搭日程（HH:mm 精度），按时触发生图
- 🎨 **双引擎生图** — GPT Image 高质量 + Gitee z-image-turbo 极速，自动降级
- 🖼️ **Web 画廊** — 今日/全部/收藏三 Tab，横版大卡 + 网格双布局
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

#### 方式二：launchd 原生运行（macOS 推荐）

1. 创建 plist 文件：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.portrait-gallery</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/portrait-gallery/app/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/portrait-gallery</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/portrait-gallery/logs/gallery.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/portrait-gallery/logs/gallery.log</string>
</dict>
</plist>
```

2. 加载服务：

```bash
# 创建日志目录
mkdir -p /path/to/portrait-gallery/logs

# 加载服务
launchctl load ~/Library/LaunchAgents/com.hermes.portrait-gallery.plist

# 启动服务
launchctl start com.hermes.portrait-gallery

# 查看日志
tail -f /path/to/portrait-gallery/logs/gallery.log
```

#### 方式三：Docker（可选）

```bash
docker compose up -d
```

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

启用 `GALLERY_API_KEY` 后，命令行 API 需要带认证：

```bash
curl -H "X-API-Key: $GALLERY_API_KEY" http://localhost:18889/api/gallery
# 或
curl "http://localhost:18889/api/gallery?key=$GALLERY_API_KEY"
```

## 📱 微信推送

生图完成后自动通过 `hermes send --to weixin` 推送到微信：
- 先发送图片（`MEDIA:/path/to/image.png`）
- 再发送文案（caption）

无需额外配置，确保 hermes CLI 已登录微信即可。

## 🖥️ 前端功能

- **今日 Tab** — 横版大卡片，直接展示穿搭/日程/caption，点击图片全屏查看
- **全部 Tab** — 6 列网格，点击弹窗查看详情（收藏/分享/删除）
- **收藏 Tab** — 筛选已收藏图片
- **🎀 穿搭生成** — 自定义 prompt + 参考图 + 尺寸选择
- **⚙️ 设置** — Web UI 管理 API 密钥

## 🧾 Release Notes

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
