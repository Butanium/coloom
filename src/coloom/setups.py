"""Two-layer inference config: model setups + sampler setups.

A *model setup* is an endpoint + model + default params; a *sampler setup* is a
named reference to a model setup plus param overrides. Both live in server-global
SQLite tables (not per-weave). `params` is an arbitrary pass-through JSON object —
unknown keys flow straight into the /v1/completions body, by design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from coloom.models import new_id

if TYPE_CHECKING:
    from coloom.config import EndpointConfig

REDACTED = "***"


class ModelSetup(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    base_url: str
    api_key: str | None = None  # literal key; redacted to "***" in API responses
    api_key_env: str | None = None  # env var holding the key, resolved at gen time
    model: str
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exclusive_key(self) -> ModelSetup:
        if self.api_key is not None and self.api_key_env is not None:
            raise ValueError("api_key and api_key_env are mutually exclusive")
        return self

    def redacted(self) -> ModelSetup:
        """Copy with the literal api_key masked (never echo secrets over the API)."""
        if self.api_key is None:
            return self
        return self.model_copy(update={"api_key": REDACTED})


class SamplerSetup(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    model_setup_id: str
    params: dict[str, Any] = Field(default_factory=dict)


def resolve_sampler(
    model_setup: ModelSetup,
    sampler: SamplerSetup | None,
    server_defaults: dict[str, Any],
    request_params: dict[str, Any],
) -> tuple["EndpointConfig", dict[str, Any]]:
    """Build the endpoint + merged params for a sampler-driven /gen.

    Merge order (later wins): {model, server defaults} <- model_setup.params
    <- sampler.params <- request_params. Unknown keys pass straight through.
    """
    from coloom.config import EndpointConfig

    endpoint = EndpointConfig(
        base_url=model_setup.base_url,
        model=model_setup.model,
        api_key=model_setup.api_key,
        api_key_env=model_setup.api_key_env,
        params={},  # all params live in `merged` below; keep the endpoint clean
    )
    merged = {
        "model": model_setup.model,
        **server_defaults,
        **model_setup.params,
        **(sampler.params if sampler else {}),
        **request_params,
    }
    return endpoint, merged


class CreateModelSetup(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class UpdateModelSetup(BaseModel):
    # omitted field = unchanged; api_key: null = clear (distinguished via model_fields_set)
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    params: dict[str, Any] | None = None


class CreateSamplerSetup(BaseModel):
    name: str
    model_setup_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class UpdateSamplerSetup(BaseModel):
    name: str | None = None
    model_setup_id: str | None = None
    params: dict[str, Any] | None = None
