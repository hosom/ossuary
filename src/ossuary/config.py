"""`agents.yaml` loading.

Prompts live in config; tool implementations and schemas live in Python. Editing
a prompt is a config change, not a code change -- which is what makes the prompt
version a cache key and makes prompt iteration cheap.

Unknown keys are a hard error. A silently ignored typo in a prompt file is a
config that does not do what its author believes it does, and the failure only
shows up as degraded findings much later.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONFIG_NAME = "agents.yaml"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    prompt: str
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_turns: int = Field(default=15, ge=1, le=200)
    max_tokens: int | None = Field(default=None, ge=1)

    #: Backend-specific knobs that have no cross-backend meaning, e.g.
    #: `reasoning_effort` on Copilot. Deliberately open where the rest of this
    #: file is closed: a typo here degrades one backend rather than silently
    #: changing what every agent does.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be empty")
        return value

    @property
    def prompt_version(self) -> str:
        """Content hash of the prompt, used as a cache key.

        Editing a prompt must invalidate cached inference but not cached I/O,
        so this hashes the prompt alone -- not the model, not the temperature.
        """
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:12]


class OssuaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: dict[str, AgentConfig]

    @field_validator("agents")
    @classmethod
    def _require_known_agents(cls, value: dict[str, AgentConfig]) -> dict[str, AgentConfig]:
        missing = {"scanner", "clusterer"} - set(value)
        if missing:
            raise ValueError(
                f"agents.yaml is missing required agent(s): {', '.join(sorted(missing))}"
            )
        return value

    @property
    def scanner(self) -> AgentConfig:
        return self.agents["scanner"]

    @property
    def clusterer(self) -> AgentConfig:
        return self.agents["clusterer"]


def find_config(explicit: Path | None = None, start: Path | None = None) -> Path:
    """Locate `agents.yaml`: an explicit path, then upward from `start`, then the packaged default."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        return path

    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / DEFAULT_CONFIG_NAME
        if candidate.exists():
            return candidate

    packaged = Path(__file__).parent / "data" / DEFAULT_CONFIG_NAME
    if packaged.exists():
        return packaged
    raise FileNotFoundError(
        f"no {DEFAULT_CONFIG_NAME} found in {current} or any parent directory"
    )


def load_config(path: Path | None = None, start: Path | None = None) -> OssuaryConfig:
    config_path = find_config(path, start=start)
    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{config_path}: invalid YAML: {exc}") from exc

    if raw is None:
        raise ValueError(f"{config_path}: file is empty")
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path}: expected a mapping at the top level")

    return OssuaryConfig.model_validate(raw)
