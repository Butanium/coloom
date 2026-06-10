"""Endpoint + preset configuration.

Sampling params are an untyped pass-through dict (Tapestry-Loom's lesson: typed
sampling params go stale against backend-specific extensions; let the endpoint
reject what it doesn't know).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(Exception):
    pass


def load_env_file(path: str | Path = ".env") -> bool:
    """Load a repo-local .env into the process environment (api_key_env
    endpoints like gpt4-base resolve from it at request time). Existing
    environment variables always win (override=False). Returns whether the
    file existed."""
    path = Path(path)
    if not path.exists():
        return False
    load_dotenv(path, override=False)
    return True


DEFAULT_PARAMS: dict[str, Any] = {
    "temperature": 1.0,
    "max_tokens": 64,
    "logprobs": 5,
    "n": 1,
}


class EndpointConfig(BaseModel):
    base_url: str  # e.g. "https://api.openai.com/v1"
    model: str
    kind: str = "completions"  # "chat" later
    api_key: str | None = None  # literal bearer key (e.g. from a model setup)
    api_key_env: str | None = None  # env var holding the key; never the key itself
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=lambda: dict(DEFAULT_PARAMS))

    def resolve_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        key = self.api_key
        if key is None and self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ConfigError(
                    f"endpoint {self.model!r}: env var {self.api_key_env!r} is not set"
                )
        if key:
            headers.setdefault("Authorization", f"Bearer {key}")
        return headers


class Preset(BaseModel):
    endpoint: str  # name in ColoomConfig.endpoints
    params: dict[str, Any] = Field(default_factory=dict)  # overlay on endpoint params


class ColoomConfig(BaseModel):
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    presets: dict[str, Preset] = Field(default_factory=dict)
    default_preset: str | None = None

    def resolve_preset(self, name: str | None) -> tuple[EndpointConfig, dict[str, Any]]:
        """Returns (endpoint, merged params) for a preset name (None = default)."""
        name = name or self.default_preset
        if name is None:
            if len(self.endpoints) == 1:
                (endpoint,) = self.endpoints.values()
                return endpoint, dict(endpoint.params)
            raise KeyError("no preset given and no default_preset configured")
        if name in self.presets:
            preset = self.presets[name]
            endpoint = self.endpoints[preset.endpoint]
            return endpoint, {**endpoint.params, **preset.params}
        if name in self.endpoints:
            endpoint = self.endpoints[name]
            return endpoint, dict(endpoint.params)
        raise KeyError(f"unknown preset or endpoint {name!r}")


def load_config(path: str | Path) -> ColoomConfig:
    with open(path) as f:
        return ColoomConfig.model_validate(yaml.safe_load(f))
