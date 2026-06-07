"""Runtime configuration and path helpers for portrait gallery."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_ROOT = APP_DIR.parent


def _non_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def get_nested(data: dict, path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def unique_values(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _non_empty(value)
        if text and text not in result:
            result.append(text)
    return result


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_project_root(config_path: str = "", config: dict | None = None) -> Path:
    env_root = _non_empty(os.getenv("HERMES_PORTRAIT_GALLERY_HOME") or os.getenv("PORTRAIT_GALLERY_HOME"))
    if env_root:
        return Path(env_root).expanduser().resolve()

    configured = _non_empty(get_nested(config or {}, "paths.project_root", ""))
    if configured:
        root = Path(configured).expanduser()
        return (DEFAULT_PROJECT_ROOT / root).resolve() if not root.is_absolute() else root.resolve()

    if config_path:
        path = Path(config_path).expanduser().resolve()
        if path.name:
            return path.parent.parent.resolve()

    return DEFAULT_PROJECT_ROOT.resolve()


def resolve_config_path(config_path: str = "") -> str:
    explicit = _non_empty(config_path or os.getenv("CONFIG_PATH"))
    if explicit:
        return str(Path(explicit).expanduser().resolve())

    env_root = _non_empty(os.getenv("HERMES_PORTRAIT_GALLERY_HOME") or os.getenv("PORTRAIT_GALLERY_HOME"))
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser() / "config" / "config.yaml")

    candidates.extend([
        DEFAULT_PROJECT_ROOT / "config" / "config.yaml",
        Path.home() / "hermes-portrait-gallery-external" / "config" / "config.yaml",
        Path.home() / "Docker" / "hermes-portrait-gallery" / "config" / "config.yaml",
        Path.home() / "docker" / "hermes-portrait-gallery" / "config" / "config.yaml",
    ])
    for volume in Path("/Volumes").glob("*"):
        candidates.append(volume / "hermes-portrait-gallery" / "config" / "config.yaml")

    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())

    return str((DEFAULT_PROJECT_ROOT / "config" / "config.yaml").resolve())


def load_config(config_path: str = "") -> dict:
    path = resolve_config_path(config_path)
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    local_path = Path(path).with_name("local.yaml")
    if local_path.exists():
        with open(local_path, encoding="utf-8") as f:
            local_config = yaml.safe_load(f) or {}
        if isinstance(local_config, dict):
            config = deep_merge(config, local_config)
    return config


def resolve_path(value: Any, root: Path, default: str | Path = "") -> str:
    raw = _non_empty(value)
    if not raw and default:
        raw = str(default)
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def resolve_data_dir(config: dict, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "paths.data_dir", "") or config.get("data_dir", "data")
    return resolve_path(value, root, "data")


def resolve_script_dir(config: dict, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "image_gen.script_dir", "")
    return resolve_path(value, root, "app/zhuzhu")


def resolve_builtin_reference_dir(config: dict, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "paths.builtin_reference_dir", "")
    return resolve_path(value, root, "app/references")


def resolve_reference_dir(config: dict, data_dir: str, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "paths.reference_dir", "")
    return resolve_path(value, root, Path(data_dir) / "references")


def load_json_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def api_keys_path(data_dir: str) -> str:
    return os.path.join(data_dir, "api_keys_config.json")


def plugin_config_path(data_dir: str) -> str:
    return os.path.join(data_dir, "plugin_config.json")


def normalize_chat_url(base_url: str) -> str:
    base = _non_empty(base_url).rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def llm_request_config(config: dict, data_dir: str) -> dict:
    keys = load_json_file(api_keys_path(data_dir))
    base_url = (
        _non_empty(keys.get("cpa_url"))
        or _non_empty(os.getenv("CPA_BASE_URL"))
        or _non_empty(get_nested(config, "llm.base_url", ""))
    )
    api_key = (
        _non_empty(keys.get("cpa_key"))
        or _non_empty(os.getenv("CPA_API_KEY"))
        or _non_empty(get_nested(config, "llm.api_key", ""))
    )
    models = unique_values(
        get_nested(config, "llm.model", ""),
        get_nested(config, "llm.fallback_model", ""),
    )
    return {"base_url": base_url.rstrip("/"), "chat_url": normalize_chat_url(base_url), "api_key": api_key, "models": models}


def apply_network_env(config: dict, env: dict[str, str] | None = None) -> dict[str, str]:
    target = env if env is not None else os.environ
    network = config.get("network", {}) if isinstance(config.get("network"), dict) else {}
    mapping = {
        "http_proxy": ("HTTP_PROXY", "http_proxy"),
        "https_proxy": ("HTTPS_PROXY", "https_proxy"),
        "no_proxy": ("NO_PROXY", "no_proxy"),
    }
    for key, env_names in mapping.items():
        value = _non_empty(network.get(key))
        if not value:
            continue
        for env_name in env_names:
            target[env_name] = value
    return target


def build_child_env(config: dict, config_path: str, data_dir: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    root = resolve_project_root(config_path, config)
    env = dict(os.environ)
    env["HERMES_PORTRAIT_GALLERY_HOME"] = str(root)
    env["CONFIG_PATH"] = str(Path(config_path).expanduser().resolve())
    env["GALLERY_DATA_DIR"] = data_dir
    env["ZHUZHU_DATA_DIR"] = data_dir
    env["ZHUZHU_PROJECT_DIR"] = str(root)
    env["ZHUZHU_MEDIA_DIR"] = os.path.join(data_dir, "images")
    app_path = str(root / "app")
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = app_path if not current_pythonpath else app_path + os.pathsep + current_pythonpath
    apply_network_env(config, env)
    if extra:
        env.update({k: v for k, v in extra.items() if v})
    return env


def configured_python(config: dict) -> str:
    return _non_empty(get_nested(config, "runtime.python", "")) or _non_empty(get_nested(config, "image_gen.python", ""))
