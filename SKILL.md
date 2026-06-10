---
name: portrait-gallery
description: >
  Install, configure, run, and operate the Portrait Gallery AI outfit portrait
  system. Use when the user wants an AI agent to set up the gallery from GitHub,
  start or debug the local service, configure image/LLM API keys, generate daily
  outfit schedules, create custom portrait images, inspect the web gallery, or
  call the REST API for photo management.
metadata:
  short-description: Run and operate the AI outfit portrait gallery
  openclaw:
    homepage: https://github.com/i-kirito/portrait-gallery
---

# Portrait Gallery Skill

This repository can be installed directly as an AI-agent skill. Once installed,
the agent should use these instructions to set up and operate the local Portrait
Gallery service.

## Install In An AI Agent

### Codex

Ask Codex to install the skill from GitHub:

```text
Use $skill-installer to install https://github.com/i-kirito/portrait-gallery as a skill.
Use repo i-kirito/portrait-gallery, path ., and name portrait-gallery.
```

If running the installer script manually:

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo i-kirito/portrait-gallery \
  --path . \
  --name portrait-gallery
```

Restart the agent after installation so it can discover the new skill.

### Other Skill-Capable Agents

Install this repository URL as a skill source:

```text
https://github.com/i-kirito/portrait-gallery
```

If the agent asks for a skill path, use `.`. If it asks for a skill name, use
`portrait-gallery`.

### Hermes Agent / OpenClaw

Both [Hermes Agent](https://hermes-agent.nousresearch.com) and OpenClaw support
skill-based integration. After installing this repo as a skill:

1. **Load the skill** — the agent loads `SKILL.md` automatically when the user
   mentions the gallery, or load it explicitly (e.g. `skill_view(name='portrait-gallery')`).
2. **Operate via REST API** — the agent drives the running gallery through the
   REST endpoints documented below (generate, schedule, favorite, delete).
3. **Image generation routing** — point the gallery at your agent's existing
   model gateway. If you run [AxonHub](https://github.com/looplj/AxonHub) (used by
   many Hermes setups), set `GPT_IMAGE_BASE_URL` to your AxonHub `/v1` endpoint
   and `GPT_IMAGE_API_KEY` to an AxonHub key — the gallery sends OpenAI-compatible
   image requests and AxonHub handles multi-channel fallback.
4. **Push delivery** — generated photos can be pushed to your agent's messaging
   channel (the reference setup uses `hermes send --to weixin`; adapt the
   delivery hook to your own platform).

## What This Skill Does

Portrait Gallery is a local AI outfit portrait system:

- Generates a daily outfit plan and HH:mm schedule with an LLM.
- Dynamically schedules image-generation jobs from the generated schedule.
- Generates portraits with OpenAI-compatible image APIs (GPT Image / AxonHub / custom endpoints), Gemini, and optional Gitee z-image-turbo fallback.
- Serves a local Web gallery for today/all/favorites/custom generation.
- Stores runtime data in `data/schedule_data.json` and images in `data/images/`.
- Exposes a REST API for agents and integrations.

## Agent Workflow

### 1. Locate Or Prepare The Project

If the user already has a checkout, work there. Otherwise clone it:

```bash
git clone https://github.com/i-kirito/portrait-gallery.git
cd portrait-gallery
```

Before editing, inspect the worktree:

```bash
git status --short --branch
```

Never overwrite user changes unless the user explicitly asks.

### 2. Install Dependencies

Direct Python run:

```bash
python3 -m pip install -r app/requirements.txt
```

Docker run:

```bash
docker compose build
docker compose up -d
```

### 3. Configure Keys

Prefer the Web UI settings panel when the service is running:

```text
http://localhost:18889
```

Or configure environment variables:

```bash
export CPA_API_KEY="your-cpa-api-key"
export CPA_BASE_URL="http://your-cpa-proxy:port/v1"
export GPT_IMAGE_API_KEY="your-api-key"  # AxonHub: ah-xxxxx, Direct: sk-xxxxx
export GPT_IMAGE_BASE_URL="http://your-endpoint/v1"  # AxonHub or OpenAI-compatible
export GALLERY_API_KEY="optional-web-api-key"
```

**Recommended Setup**: Route through [AxonHub](https://github.com/looplj/AxonHub) for multi-channel fallback and unified management:

```bash
export GPT_IMAGE_BASE_URL="http://your-axonhub-host:port/v1"
export GPT_IMAGE_API_KEY="ah-xxxxx"  # Your AxonHub API key
```

**Configuration Priority** (high to low):
1. Environment variables (`CPA_API_KEY`, `GPT_IMAGE_API_KEY`, etc.)
2. Web UI Settings Panel (saved to `data/api_keys_config.json`)
3. `config/config.yaml` (default values)

Runtime key files are stored under `data/`; avoid printing secrets in user
responses.

### 4. Start The Service

Direct run:

```bash
cd app
python3 -m main
```

The default Web UI is:

```text
http://localhost:18889
```

If a service is already listening on port `18889`, inspect it before starting a
new process:

```bash
lsof -nP -iTCP:18889 -sTCP:LISTEN
```

### 5. Verify Health

```bash
curl http://localhost:18889/api/health
curl http://localhost:18889/api/today
curl http://localhost:18889/api/schedule-detail
curl http://localhost:18889/api/photo-jobs
```

If `GALLERY_API_KEY` is set, pass it as `X-API-Key` or `?key=...`.

## Common Agent Tasks

### Refresh Today's Schedule

Regenerate the LLM schedule without immediately generating an image:

```bash
curl -X POST http://localhost:18889/api/refresh-schedule
```

This should rewrite today's date-keyed schedule entry and rebuild future dynamic
photo jobs.

### Generate A Photo For The Current Period

```bash
curl -X POST http://localhost:18889/api/generate-now \
  -H "Content-Type: application/json" \
  -d '{}'
```

The server chooses the theme from the current local time.

### Generate A Custom Prompt

```bash
curl -X POST http://localhost:18889/api/generate-custom \
  -H "Content-Type: application/json" \
  -d '{"prompt":"wearing a white dress under cherry blossoms","size":"1024x1536"}'
```

### Manage Gallery Images

```bash
curl http://localhost:18889/api/gallery
curl -X POST http://localhost:18889/api/images/<image_filename>/favorite
curl -X DELETE http://localhost:18889/api/images/<image_filename>
```

## Important Files

- `app/main.py` - aiohttp service startup and APScheduler orchestration.
- `app/scheduler.py` - LLM daily outfit and schedule generation.
- `app/web_server.py` - REST API, static files, gallery routes, settings.
- `app/image_gen.py` - async wrapper around the image-generation subprocess.
- `app/zhuzhu/generate.py` - unified image-generation CLI.
- `app/zhuzhu/core.py` - prompt building, metadata, gallery sync.
- `app/web/index.html` - single-file Web UI.
- `config/config.yaml` - default host, port, character, and engine settings.
- `data/schedule_data.json` - runtime schedule and gallery database.

## Debugging Notes

- Python changes require a service restart.
- Frontend changes in `app/web/index.html` only need a browser refresh.
- Dynamic photo jobs are in-memory APScheduler jobs; after restart, the app
  should restore future jobs from today's saved schedule.
- If the schedule looks too short, check `data/schedule_data.json` for a complete
  date-keyed entry such as `"2026-06-05"` with non-empty `schedule`, `outfit`,
  `prompt`, and `caption`.
- If images generate but no schedule appears, inspect `schedule_time` fields on
  image entries and the `/api/schedule-detail` fallback logic.
- Use `python3 -m py_compile` on edited Python files before restarting.

## Safety Notes For Agents

- Treat API keys and `data/api_keys_config.json` as secrets.
- Confirm before deleting images, changing keys, or running self-update actions.
- Avoid exposing the service publicly unless `GALLERY_API_KEY` is set.
- Do not run multiple long image-generation processes unless the user asks.
