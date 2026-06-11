"""A fake OpenAI-compatible /v1/completions server: random text, random logprobs.

For UI work and testing without burning real API credit. Honors `n`, `max_tokens`,
`logprobs` (top-k size), `seed`, and `temperature` (flattens the fake distribution);
emits the same response shape coloom's parser consumes (tokens / token_logprobs /
top_logprobs / text_offset), so generated nodes carry full Token data. Test-only
body params: `delay` (exact per-request sleep) and `fail_times`/`fail_key`/
`fail_status` (fail the first N requests sharing a key — drives retry tests).

Run: `uv run coloom-fake-openai [--port 9999] [--delay 0.5] [--seed 0]`
Point an endpoint at it:
    endpoints:
      gpt-fake: {base_url: "http://127.0.0.1:9999/v1", model: "gpt-fake"}
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

WORDS = (
    "the of and a to in is it you that he was for on are with as his they be at"
    " one have this from or had by hot word but what some we can out other were"
    " all there when up use your how said an each she which do their time if will"
    " way about many then them write would like so these her long make thing see"
    " him two has look more day could go come did number sound no most people my"
    " over know water than call first who may down side been now find any new"
    " work part take get place made live where after back little only round man"
    " year came show every good me give our under name very through just form"
    " sentence great think say help low line differ turn cause much mean before"
    " move right boy old too same tell does set three want air well also play"
    " small end put home read hand port large spell add even land here must big"
    " high such follow act why ask men change went light kind off need house"
    " picture try us again animal point mother world near build self earth father"
    " loom thread weave branch shuttle warp weft pattern tapestry yarn"
).split()
PUNCT = [".", ",", ";", "—", "?", "!"]


def _gen_choice(
    rng: random.Random, max_tokens: int, top_k: int, temperature: float, index: int
) -> dict:
    n_tokens = rng.randint(max(1, max_tokens // 2), max_tokens)
    tokens: list[str] = []
    token_logprobs: list[float] = []
    top_logprobs: list[dict[str, float]] = []
    flat = max(temperature, 0.1)
    for i in range(n_tokens):
        if rng.random() < 0.12 and i > 0:
            tok = rng.choice(PUNCT) + (rng.choice(["", " ", "\n", "\n\n"]))
        else:
            tok = (" " if (i > 0 or rng.random() < 0.8) else "") + rng.choice(WORDS)
        # chosen-token probability: confident at low temp, diffuse at high temp
        p = rng.betavariate(2.0 / flat, 2.0)
        lp = math.log(max(p, 1e-6))
        # alternatives strictly below the chosen probability, roughly geometric
        alts: dict[str, float] = {tok: lp}
        remaining = p
        while len(alts) < top_k:
            alt = " " + rng.choice(WORDS)
            if alt in alts:
                continue
            remaining *= rng.uniform(0.2, 0.9)
            alts[alt] = math.log(max(remaining, 1e-9))
        tokens.append(tok)
        token_logprobs.append(lp)
        top_logprobs.append(alts)

    text = "".join(tokens)
    offsets = []
    pos = 0
    for tok in tokens:
        offsets.append(pos)
        pos += len(tok)
    # finish_reason derived from the text itself, like a real backend:
    # ended on terminal punctuation -> "stop"; ran to the cap -> "length";
    # trailing whitespace/newline cut -> "stop" with a stop-sequence flavor
    stripped = text.rstrip()
    if n_tokens >= max_tokens:
        finish_reason = "length"
    elif stripped and stripped[-1] in ".?!":
        finish_reason = "stop"
    else:
        finish_reason = rng.choice(["stop", "length"])
    return {
        "text": text,
        "index": index,
        "finish_reason": finish_reason,
        "logprobs": {
            "tokens": tokens,
            "token_logprobs": token_logprobs,
            "top_logprobs": top_logprobs,
            "text_offset": offsets,
        },
    }


def build_app(default_seed: int | None = None, delay: float = 0.0) -> FastAPI:
    app = FastAPI(title="gpt-fake")
    counter = {"requests": 0}
    # synthetic-failure bookkeeping for retry tests: requests already failed,
    # keyed by the request body's `fail_key` (see below)
    fail_counts: dict[str, int] = {}
    received: list[dict] = []  # request bodies, for test assertions
    app.state.received = received

    @app.get("/v1/models")
    async def models() -> dict:
        # lets coloom's POST /probe-endpoint be exercised against the mock
        return {
            "object": "list",
            "data": [{"id": "gpt-fake", "object": "model", "owned_by": "coloom"}],
        }

    @app.post("/v1/completions", response_model=None)
    async def completions(request: Request) -> dict | JSONResponse:
        body = await request.json()
        counter["requests"] += 1
        received.append(body)
        # synthetic-failure mode for retry tests: a `fail_times` body param
        # makes the first N requests sharing the same `fail_key` fail with
        # `fail_status` (default 500), then succeed. Like `delay`, these ride
        # the params pass-through — a test generator opts in via its params.
        fail_times = int(body.get("fail_times") or 0)
        if fail_times > 0:
            key = str(body.get("fail_key") or "")
            failed = fail_counts.get(key, 0)
            if failed < fail_times:
                fail_counts[key] = failed + 1
                status = int(body.get("fail_status") or 500)
                return JSONResponse(
                    status_code=status,
                    content={
                        "error": {
                            "message": f"synthetic failure {failed + 1}/{fail_times}"
                            f" (key={key!r})"
                        }
                    },
                )
        seed = body.get("seed", default_seed)
        rng = random.Random(seed if seed is not None else rng_entropy())
        n = int(body.get("n", 1))
        max_tokens = int(body.get("max_tokens", 16))
        top_k = int(body.get("logprobs") or 0) or 1
        temperature = float(body.get("temperature", 1.0))
        if delay > 0:
            await asyncio.sleep(delay * (0.5 + rng.random()))
        # per-request override: a `delay` param in the body sleeps EXACTLY that
        # long (deterministic) — lets one test/generator opt into a held-open
        # in-flight window (e.g. the gen-placeholder UI) without slowing the
        # whole stack via --delay
        req_delay = float(body.get("delay") or 0)
        if req_delay > 0:
            await asyncio.sleep(req_delay)
        return {
            "id": f"cmpl-fake-{counter['requests']}",
            "object": "text_completion",
            "created": 0,
            "model": body.get("model", "gpt-fake"),
            "choices": [
                _gen_choice(rng, max_tokens, top_k, temperature, i) for i in range(n)
            ],
            "usage": {"prompt_tokens": len(body.get("prompt", "")) // 4},
        }

    return app


def rng_entropy() -> int:
    return random.SystemRandom().randrange(2**31)


def main() -> None:
    parser = argparse.ArgumentParser(description="fake OpenAI completions server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--seed", type=int, default=None, help="deterministic output")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.6,
        help="simulated generation latency in seconds (randomized ±50%%)",
    )
    args = parser.parse_args()
    app = build_app(default_seed=args.seed, delay=args.delay)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
