"""Weave data model: nodes, content, creators.

Our own JSON-native model, inspired by Tapestry Loom's v1 design (see docs/PLAN.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- content


class TopLogprob(BaseModel):
    text: str
    logprob: float
    token_id: int | None = None


class Token(BaseModel):
    text: str
    logprob: float | None = None
    token_id: int | None = None
    entropy: float | None = None
    top_logprobs: list[TopLogprob] = Field(default_factory=list)


class Snippet(BaseModel):
    """Plain text content (human-written or agent-written branches)."""

    type: Literal["snippet"] = "snippet"
    text: str


class Tokens(BaseModel):
    """Base-model generation with per-token logprob info."""

    type: Literal["tokens"] = "tokens"
    tokens: list[Token]

    @property
    def text(self) -> str:
        return "".join(t.text for t in self.tokens)


NodeContent = Annotated[Union[Snippet, Tokens], Field(discriminator="type")]


def content_text(content: Snippet | Tokens) -> str:
    return content.text


# ---------------------------------------------------------------- creators


class HumanCreator(BaseModel):
    type: Literal["human"] = "human"
    label: str
    color: str | None = None
    id: str | None = None


class ModelCreator(BaseModel):
    type: Literal["model"] = "model"
    label: str
    color: str | None = None
    id: str | None = None
    seed: int | None = None
    raw_request: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None


class UnknownCreator(BaseModel):
    type: Literal["unknown"] = "unknown"


Creator = Annotated[
    Union[HumanCreator, ModelCreator, UnknownCreator], Field(discriminator="type")
]


# ---------------------------------------------------------------- nodes / weave


class Node(BaseModel):
    id: str = Field(default_factory=new_id)
    parents: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    content: NodeContent
    creator: Creator = Field(default_factory=UnknownCreator)
    created: datetime = Field(default_factory=utcnow)
    modified: datetime = Field(default_factory=utcnow)
    bookmarked: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.text


class WeaveInfo(BaseModel):
    """Weave-level metadata row (nodes live in their own table)."""

    id: str = Field(default_factory=new_id)
    title: str = "Untitled weave"
    description: str = ""
    created: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Cursor(BaseModel):
    """A named position in the weave. There is no single "active path": each
    participant (human, agent, ...) keeps a cursor, and anyone may move anyone's
    cursor — moving someone else's is the "hey, look here" gesture. A cursor's
    thread is derived (root→node is unique in a tree)."""

    name: str
    node_id: str
    updated: datetime = Field(default_factory=utcnow)
    moved_by: str | None = None


class Weave(BaseModel):
    """Full weave snapshot — the JSON export / API shape."""

    id: str
    title: str
    description: str
    created: datetime
    nodes: dict[str, Node]
    roots: list[str]
    cursors: dict[str, Cursor]
    bookmarks: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
