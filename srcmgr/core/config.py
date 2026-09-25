"""Layered configuration.

Resolution order (later wins, deep-merged per section):

1. built-in :data:`DEFAULTS`
2. ``$SRCMGR_CONFIG_HOME/config.yaml`` (global section)
3. per-manager overrides from the same file (``managers.<name>``)
4. environment variables ``SRCMGR_<SECTION>__<KEY>``

Everything is exposed through :class:`Config`, a thin dict wrapper that keeps
returning the right value even when the user's file is partial or stale.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Mapping

from .errors import ConfigError
from .paths import config_file, dump_yaml, load_yaml

#: Keys that must never be printed verbatim by the CLI.
SECRET_KEYS = {"token", "password", "passwd", "secret", "auth", "apikey", "api_key", "_auth"}

DEFAULTS: dict[str, Any] = {
    "general": {
        "color": "auto",
        "language": "auto",
        "confirm_destructive": True,
        "require_root_for_system": True,
    },
    "health": {
        "timeout": 8.0,
        "max_workers": 8,
        "retries": 1,
        "check_tls": True,
        "check_gpg": False,
        "cache_ttl": 900,
    },
    "fallback": {
        # Never silently rewrite a machine's sources: opt-in only.
        "enabled": False,
        "require_confirmation": True,
        "min_latency_gain_ms": 0.0,
        "only_after_failures": 2,
        "order": [],
    },
    "backup": {
        "enabled": True,
        "keep": 50,
        "before_change": True,
    },
    "registry": {
        "prefer_country": "",
        "include_builtin": True,
        "disabled_providers": [],
    },
    "plugins": {
        "trust_user_plugins": False,
        "allow_remote_install": True,
        "update_check": False,
    },
    "managers": {},
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce(value: str, template: Any) -> Any:
    """Coerce an env-var string to the type of the default value."""
    if isinstance(template, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(value)
        except ValueError:
            return template
    if isinstance(template, float):
        try:
            return float(value)
        except ValueError:
            return template
    if isinstance(template, (list, tuple)):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _env_overrides() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for env_key, raw in os.environ.items():
        if not env_key.startswith("SRCMGR_") or "__" not in env_key:
            continue
        path = env_key[len("SRCMGR_"):].lower().split("__")
        if path[0] in {"config_home", "data_home", "cache_home"}:
            continue  # those address the directories themselves
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        template: Any = DEFAULTS.get(path[0], {})
        for part in path[1:-1]:
            template = template.get(part, {}) if isinstance(template, Mapping) else {}
        leaf = template.get(path[-1]) if isinstance(template, Mapping) else None
        node[path[-1]] = _coerce(raw, leaf if leaf is not None else raw)
    return out


class Config:
    """Effective configuration for one invocation."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        merged = deep_merge(DEFAULTS, data or {})
        self._data = deep_merge(merged, _env_overrides())

    # -- construction --------------------------------------------------------
    @classmethod
    def load(cls, path: Any | None = None) -> "Config":
        target = path or config_file()
        raw = load_yaml(target, default={}) or {}
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{target}: top level must be a mapping")
        return cls(raw)

    @classmethod
    def defaults(cls) -> "Config":
        return cls({})

    @property
    def path(self):
        return config_file()

    # -- access --------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def for_manager(self, manager: str, section: str) -> dict[str, Any]:
        """Effective settings for one manager (global < manager override)."""
        base = self.section(section)
        overrides = self.get(f"managers.{manager}.{section}", {}) or {}
        if isinstance(overrides, Mapping) and overrides:
            base = deep_merge(base, overrides)
        return base

    def health(self, manager: str | None = None) -> dict[str, Any]:
        return self.for_manager(manager, "health") if manager else self.section("health")

    def fallback(self, manager: str | None = None) -> dict[str, Any]:
        return self.for_manager(manager, "fallback") if manager else self.section("fallback")

    def backup(self, manager: str | None = None) -> dict[str, Any]:
        return self.for_manager(manager, "backup") if manager else self.section("backup")

    # -- mutation ------------------------------------------------------------
    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        return _redact(self._data) if redact else copy.deepcopy(self._data)

    def set(self, dotted: str, value: Any, *, path: Any | None = None) -> None:
        """Persist one value into the user config file."""
        target = path or config_file()
        raw = load_yaml(target, default={}) or {}
        if not isinstance(raw, Mapping):
            raw = {}
        raw = copy.deepcopy(dict(raw))
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                node = {}
        node[parts[-1]] = value
        dump_yaml(raw, target, header="Source Manager user configuration")


def _redact(data: Any) -> Any:
    if isinstance(data, Mapping):
        out = {}
        for key, value in data.items():
            if str(key).lower() in SECRET_KEYS:
                out[key] = "<redacted>" if value else value
            else:
                out[key] = _redact(value)
        return out
    if isinstance(data, list):
        return [_redact(item) for item in data]
    return data


def parse_value(text: str) -> Any:
    """Parse a CLI ``--set key=value`` value into a Python object."""
    lowered = text.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if text.startswith("[") and text.endswith("]"):
        import json

        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001 - fall back to a plain string
            return text
    return text
