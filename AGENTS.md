# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python portrait-gallery service with a small static web
UI. Core application code lives in `app/`: `main.py` wires scheduling and the
aiohttp server, `web_server.py` exposes REST routes and static files,
`scheduler.py` builds daily plans, `image_gen.py` orchestrates generation,
`characters.py` manages local/runtime character definitions, `group_chat.py`
persists group-chat rooms and messages, `calendar_context.py` supplies
date-aware schedule guidance, and `store.py` handles persisted JSON updates.
Image engine integrations live under `app/zhuzhu/`. The frontend is the
single-file app at `app/web/index.html`. Configuration is in
`config/config.yaml`; runtime state, generated images, local characters, group
chat data, and local references belong in `data/`. Logs belong in `logs/`.

## Build, Test, and Development Commands

- `python3 -m pip install -r app/requirements.txt` installs runtime
  dependencies.
- `cd app && python3 main.py` runs the local service on the configured host and
  port, usually `http://localhost:18889`.
- `docker compose build` builds the container image.
- `docker compose up -d` starts the service with `./data` and `./config`
  mounted into the container.
- `PORTRAIT_GALLERY_IMAGE=REGISTRY_OR_USER/hermes-portrait-gallery:1.3.6 docker compose up -d`
  runs a pinned published image from Docker Hub.
- `curl http://localhost:18889/api/health` verifies that the server is
  responding.

## Coding Style & Naming Conventions

Use Python 3.11-compatible code and follow the existing style: 4-space
indentation, descriptive `snake_case` functions and variables, module-level
constants in `UPPER_SNAKE_CASE`, and concise docstrings for non-obvious
behavior. Keep route handlers and scheduling logic separated from lower-level
storage or image-generation helpers. The frontend currently uses plain
HTML/CSS/JavaScript in `app/web/index.html`; keep IDs and functions descriptive
and avoid introducing a build step unless it is clearly needed.

## Testing Guidelines

Python regression tests live under `tests/` and can be run with
`python3 -m unittest discover -s tests`. Add focused tests for new lower-level
logic, especially scheduler, calendar, store, character, and group-chat helpers.
For service/UI changes, run the service locally and exercise affected endpoints
with `curl`. For UI changes, verify the gallery in a browser at
`localhost:18889` and check relevant API responses.

## Commit & Pull Request Guidelines

Git history uses Conventional Commit-style subjects, for example
`feat: add disliked outfit feedback`, `fix: harden gallery image generation
flows`, and `chore: release 1.3.3`. Keep commits scoped and use `feat:`,
`fix:`, `style:`, `chore:`, or similar prefixes. Pull requests should include a
summary, testing notes, linked issues when applicable, screenshots for UI
changes, and any configuration, Docker image, or migration steps.

## Security & Configuration Tips

Prefer environment variables or local-only config for API keys. Set
`GALLERY_PASSWORD` or complete the first-run Web password setup before exposing
the service beyond localhost. Treat `config/config.yaml`, `data/`, `logs/`, and
generated images as deployment-specific unless a change is intentionally part of
the product.
