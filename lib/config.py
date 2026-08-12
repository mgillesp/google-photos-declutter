"""Config loading with sensible defaults.

Scripts call load_config() which merges config.yaml (if present, gitignored) over
the built-in defaults, so the tools work out of the box with no config file.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "analysis": {
        "blur_variance_threshold": 60.0,
        "burst_window_seconds": 3,
        "czkawka_max_difference": 10,
        "video_max_mb": 200,
        "video_max_seconds": 0,
        "thumbnail_max_px": 320,
        "ollama": {
            "host": "http://localhost:11434",
            "model": "moondream",
        },
    },
    "exif": {
        "write_tags": ["DateTimeOriginal", "CreateDate", "ModifyDate"],
    },
    "upload": {
        "client_secret_path": "~/.config/gphotos-declutter/client_secret.json",
        "token_path": "~/.config/gphotos-declutter/token.json",
        "scope": "https://www.googleapis.com/auth/photoslibrary.appendonly",
        "batch_size": 50,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Return merged config. Looks for config.yaml at repo root unless `path` given."""
    cfg_path = Path(path) if path else REPO_ROOT / "config.yaml"
    if cfg_path.exists() and yaml is not None:
        with open(cfg_path) as f:
            user_cfg = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULTS, user_cfg)
    return copy.deepcopy(DEFAULTS)


def expand(path: str) -> Path:
    """Expand ~ and env vars in a config path."""
    return Path(os.path.expanduser(os.path.expandvars(path)))
