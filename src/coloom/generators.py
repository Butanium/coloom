"""Templates + per-profile generators (replaces the two-layer model/sampler setups).

A *template* is a complete generator definition on the shelf, server-global;
`builtin` templates are imported from the yaml config at boot and are read-only
via the API. A *generator* is the concrete, activatable thing, per-profile; all
its definition fields are nullable (= inherited from its parent chain) and
`params` holds only overridden keys. `parent` points at a template or at
another generator of the same profile.

Contract: docs/generators-api.md.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field, model_validator

from coloom.models import new_id

REDACTED = "***"


class ParentRef(BaseModel):
    kind: Literal["template", "generator"]
    id: str


class Template(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    builtin: bool = False
    base_url: str
    model: str
    api_key: str | None = None  # literal key; redacted to "***" in API responses
    api_key_env: str | None = None  # env var holding the key, resolved at gen time
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exclusive_key(self) -> Template:
        if self.api_key is not None and self.api_key_env is not None:
            raise ValueError("api_key and api_key_env are mutually exclusive")
        return self

    def redacted(self) -> Template:
        if self.api_key is None:
            return self
        return self.model_copy(update={"api_key": REDACTED})


class Generator(BaseModel):
    id: str = Field(default_factory=new_id)
    profile: str
    name: str
    parent: ParentRef | None = None
    base_url: str | None = None  # None = inherited
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)  # overridden keys only
    # legacy sampler_setup id this generator was migrated from (lets clients map
    # old {kind:'sampler', id} settings refs exactly); None for everything else
    migrated_from: str | None = None

    @model_validator(mode="after")
    def _exclusive_key(self) -> Generator:
        if self.api_key is not None and self.api_key_env is not None:
            raise ValueError("api_key and api_key_env are mutually exclusive")
        return self

    def redacted(self) -> Generator:
        if self.api_key is None:
            return self
        return self.model_copy(update={"api_key": REDACTED})


class ResolvedGenerator(BaseModel):
    """The leaf→root resolution of a generator's parent chain. `usable` is false
    until base_url + model resolve non-empty (a parentless skeleton)."""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.base_url) and bool(self.model)

    def redacted(self) -> ResolvedGenerator:
        if self.api_key is None:
            return self
        return self.model_copy(update={"api_key": REDACTED})


class GeneratorWithResolved(Generator):
    """API shape for GET responses: raw overrides + the resolved view."""

    resolved: ResolvedGenerator
    usable: bool = False


def resolve_chain(chain: Sequence[Generator | Template]) -> ResolvedGenerator:
    """Resolve a leaf→root chain (generator → … → template).

    Scalar fields: nearest-set wins. Credentials (api_key / api_key_env) are one
    logical field — the nearest row that sets *either* wins for both, so a child
    api_key cleanly overrides an ancestor api_key_env. `params`: merged
    root→leaf, leaf wins.
    """
    assert chain, "resolve_chain needs a non-empty chain"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    cred_set = False
    for row in chain:
        if base_url is None and row.base_url:
            base_url = row.base_url
        if model is None and row.model:
            model = row.model
        if not cred_set and (row.api_key is not None or row.api_key_env is not None):
            api_key, api_key_env, cred_set = row.api_key, row.api_key_env, True
    params: dict[str, Any] = {}
    for row in reversed(chain):  # root first, leaf overrides last
        params.update(row.params)
    return ResolvedGenerator(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        params=params,
    )


# ------------------------------------------------------------ request models


class CreateTemplateRequest(BaseModel):
    """Either explicit fields (name/base_url/model required) or
    {from_generator: id} to promote a generator's RESOLVED fields."""

    from_generator: str | None = None
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class UpdateTemplateRequest(BaseModel):
    # omitted = unchanged; api_key/api_key_env: null = clear. `params` merges
    # per-key; a key set to null is removed (distinguished via model_fields_set).
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] | None = None


class CreateGeneratorRequest(BaseModel):
    """Either explicit fields ({profile, name, ...}) or
    {from: {kind,id}, mode: inherit|duplicate, profile, name?}."""

    profile: str
    name: str | None = None
    parent: ParentRef | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    from_: ParentRef | None = Field(default=None, alias="from")
    mode: Literal["inherit", "duplicate"] | None = None

    model_config = {"populate_by_name": True}


class UpdateGeneratorRequest(BaseModel):
    # omitted = unchanged; explicit null clears a field back to inherited
    # (parent: null = detach). `params` merges per-key; null removes the override.
    name: str | None = None
    parent: ParentRef | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    params: dict[str, Any] | None = None
