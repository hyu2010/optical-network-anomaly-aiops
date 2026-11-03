"""Settings: config/diag_agent.yaml + .env + environment + CLI overrides (doc 05 §6.3)."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "diag_agent.yaml"

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-((?:[^{}]|\{[^{}]*\}|\$\{[^{}]*\})*))?\}")


def _interpolate(value: str) -> str:
    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env not in (None, ""):
            return env
        return _interpolate(default) if default is not None else ""

    prev = None
    while prev != value:
        prev, value = value, _VAR.sub(sub, value)
    return value


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _walk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        out = _interpolate(obj)
        return _coerce(out)
    return obj


def _coerce(s: str) -> Any:
    low = s.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _set_path(d: dict, path: list[str], value: Any) -> None:
    cur = d
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def _env_overrides(raw: dict) -> None:
    """DIAG__AGENT__LEDGER__COMMIT_P=0.85 -> raw['agent']['ledger']['commit_p']."""
    for key, value in os.environ.items():
        if key.startswith("DIAG__"):
            path = [p.lower() for p in key[len("DIAG__"):].split("__") if p]
            if path:
                _set_path(raw, path, _coerce(value))


@dataclass
class LLMProfile:
    name: str
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    structured_output: str = "json_mode"
    tools: str = "native"
    timeout_s: float = 90
    max_retries: int = 2
    extra_body: dict | None = None

    @property
    def enabled(self) -> bool:
        return self.name != "none" and bool(self.base_url) and bool(self.model)

    def redacted(self) -> dict:
        return {"name": self.name, "base_url": self.base_url, "model": self.model,
                "api_key": "***" if self.api_key else "", "structured_output": self.structured_output,
                "tools": self.tools}


@dataclass
class Settings:
    raw: dict
    config_path: Path
    root: Path = REPO_ROOT
    cli: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ access
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, dotted: str) -> Path:
        p = Path(str(self.get(dotted)))
        return p if p.is_absolute() else self.root / p

    @property
    def agent(self) -> dict:
        return self.raw["agent"]

    # ------------------------------------------------------------------ LLM
    @property
    def llm_profile_name(self) -> str:
        if os.environ.get("FMA_NO_LLM", "").strip() in ("1", "true", "yes"):
            return "none"
        return str(self.get("llm.profile") or "none")

    def profile_for(self, role: str) -> LLMProfile:
        name = self.llm_profile_name
        role_cfg = self.get(f"llm.roles.{role}", {}) or {}
        if role_cfg.get("profile") and name != "none":
            name = role_cfg["profile"]
        if name == "none":
            return LLMProfile(name="none")
        cfg = self.get(f"llm.profiles.{name}")
        if cfg is None:
            raise ValueError(f"unknown LLM profile {name!r}")
        known = {k: v for k, v in cfg.items() if k in LLMProfile.__dataclass_fields__}
        return LLMProfile(name=name, **{k: ("" if v is None else v) if k in ("base_url", "model", "api_key") else v
                                        for k, v in known.items()})

    def role_params(self, role: str) -> dict:
        cfg = dict(self.get(f"llm.roles.{role}", {}) or {})
        cfg.pop("profile", None)
        return cfg

    def with_overrides(self, **dotted: Any) -> "Settings":
        raw = copy.deepcopy(self.raw)
        for k, v in dotted.items():
            if v is not None:
                _set_path(raw, k.split("."), v)
        return Settings(raw=raw, config_path=self.config_path, root=self.root, cli=dict(self.cli))


def load_settings(config_path: str | Path | None = None, *, env_file: str | Path | None = None,
                  overrides: dict[str, Any] | None = None, root: Path | None = None) -> Settings:
    """Load settings with the doc 05 §6.3 precedence.

    `.env` never overrides variables already present in the process environment.
    `overrides` uses dotted keys (CLI flags), e.g. {"llm.profile": "none"}.
    """
    root = root or REPO_ROOT
    dotenv = Path(env_file) if env_file else root / ".env"
    if dotenv.exists():
        load_dotenv(dotenv, override=False)
    path = Path(config_path) if config_path else (Path(os.environ["DIAG_CONFIG"]) if os.environ.get("DIAG_CONFIG") else DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text()) or {}
    _env_overrides(raw)
    raw = _walk(raw)
    s = Settings(raw=raw, config_path=path, root=root)
    if overrides:
        s = s.with_overrides(**overrides)
        s.cli = dict(overrides)
    return s
