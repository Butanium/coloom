"""Inference client: OpenAI-compatible /v1/completions with logprobs.

Parsing edge cases follow Tapestry-Loom's polyparser (see docs/PLAN.md):
- one node per `choices[]` entry (n>1),
- prefer `text_offset` + `text` slicing over `tokens[]` strings,
- null logprobs tolerated (vLLM echoes the prompt's first token with null),
- sidecar `token_ids` zipped only on exact length match,
- top_logprobs sorted desc and truncated to the requested k,
- malformed/missing logprobs degrade to a Snippet node, never fail the response.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from coloom.config import EndpointConfig
from coloom.models import (
    ModelCreator,
    Node,
    NodeContent,
    Snippet,
    Token,
    Tokens,
    TopLogprob,
)


class InferenceError(Exception):
    pass


def truncated_entropy(top_logprobs: list[TopLogprob]) -> float:
    """Entropy estimate from the top-k alternatives only.

    Biased low when the top-k mass is far from 1 — treat as a lower bound."""
    return -sum(math.exp(t.logprob) * t.logprob for t in top_logprobs)


def _parse_top_logprobs(
    entry: Any, requested_top: int | None
) -> list[TopLogprob] | None:
    """One position's top-k map {token_str: logprob}. Malformed -> None (cleared)."""
    if not isinstance(entry, dict):
        return None
    tops = []
    for text, logprob in entry.items():
        if not isinstance(logprob, (int, float)):
            return None
        tops.append(TopLogprob(text=text, logprob=logprob))
    tops.sort(key=lambda t: t.logprob, reverse=True)
    if requested_top is not None:
        tops = tops[:requested_top]
    return tops


def parse_completion_choice(
    choice: dict[str, Any], requested_top: int | None
) -> NodeContent:
    """Parse one completions choice into Tokens (or Snippet when logprobs absent)."""
    text = choice.get("text", "")
    lp = choice.get("logprobs")
    if not isinstance(lp, dict):
        return Snippet(text=text)
    token_texts = lp.get("tokens")
    token_logprobs = lp.get("token_logprobs")
    if not isinstance(token_texts, list) or not isinstance(token_logprobs, list):
        return Snippet(text=text)
    if len(token_texts) != len(token_logprobs):
        return Snippet(text=text)

    # Prefer text_offset slicing: robust to lossy token strings (OpenAI renders
    # partial-UTF8 tokens as "bytes:..."). NB: OpenAI offsets are relative to the
    # PROMPT+completion, so they start at len(prompt), not 0 — rebase on offsets[0].
    offsets = lp.get("text_offset")
    if (
        isinstance(offsets, list)
        and len(offsets) == len(token_texts)
        and offsets
        and all(isinstance(o, int) for o in offsets)
    ):
        rel = [o - offsets[0] for o in offsets]
        if rel == sorted(rel) and rel[-1] <= len(text):
            bounds = rel + [len(text)]
            token_texts = [text[bounds[i] : bounds[i + 1]] for i in range(len(rel))]

    tops_per_token = lp.get("top_logprobs")
    if not isinstance(tops_per_token, list) or len(tops_per_token) != len(token_texts):
        tops_per_token = [None] * len(token_texts)

    token_ids = choice.get("token_ids")  # vLLM sidecar
    if not isinstance(token_ids, list) or len(token_ids) != len(token_texts):
        token_ids = [None] * len(token_texts)

    tokens = []
    for t_text, t_logprob, t_tops, t_id in zip(
        token_texts, token_logprobs, tops_per_token, token_ids
    ):
        tops = _parse_top_logprobs(t_tops, requested_top) or []
        tokens.append(
            Token(
                text=t_text,
                logprob=t_logprob if isinstance(t_logprob, (int, float)) else None,
                token_id=t_id if isinstance(t_id, int) else None,
                entropy=truncated_entropy(tops) if tops else None,
                top_logprobs=tops,
            )
        )
    return Tokens(tokens=tokens)


def parse_completion_response(
    response: dict[str, Any], request_body: dict[str, Any]
) -> list[Node]:
    """Turn a /v1/completions response into sibling Nodes (no parents linked yet)."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError(f"response has no choices: {str(response)[:500]}")
    requested_top = request_body.get("logprobs")
    if not isinstance(requested_top, int):
        requested_top = None
    model_label = response.get("model") or request_body.get("model") or "unknown"
    shared = {k: v for k, v in response.items() if k != "choices"}

    nodes = []
    for i, choice in enumerate(choices):
        content = parse_completion_choice(choice, requested_top)
        # raw_response keeps only this node's choice to avoid n-fold duplication
        raw_choice = {k: v for k, v in choice.items()}
        creator = ModelCreator(
            label=model_label,
            seed=request_body.get("seed"),
            raw_request=request_body,
            raw_response={**shared, "choice": raw_choice},
        )
        metadata: dict[str, Any] = {"choice_index": choice.get("index", i)}
        if choice.get("finish_reason") is not None:
            metadata["finish_reason"] = choice["finish_reason"]
        nodes.append(Node(content=content, creator=creator, metadata=metadata))
    return nodes


async def generate(
    endpoint: EndpointConfig,
    prompt: str,
    params: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> list[Node]:
    """POST /completions and parse into Nodes. `params` overlays endpoint params."""
    if endpoint.kind != "completions":
        raise InferenceError(f"endpoint kind {endpoint.kind!r} not supported yet")
    body: dict[str, Any] = {
        "model": endpoint.model,
        **endpoint.params,
        **(params or {}),
        "prompt": prompt,
        "stream": False,
    }
    if body.get("echo"):
        raise InferenceError(
            "echo is not supported: an echoed node would duplicate the prompt"
            " (= the parent thread) into the weave"
        )
    url = endpoint.base_url.rstrip("/") + "/completions"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=endpoint.resolve_headers())
    except httpx.HTTPError as e:
        raise InferenceError(f"request to {url} failed: {e!r}") from e
    if resp.status_code != 200:
        raise InferenceError(f"HTTP {resp.status_code} from {url}: {resp.text[:2000]}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise InferenceError(f"non-JSON response from {url}: {resp.text[:500]}") from e
    return parse_completion_response(payload, body)
