"""A fake OpenAI-compatible /v1/completions server: random text, random logprobs.

For UI work and testing without burning real API credit. Honors `n`, `max_tokens`,
`logprobs` (top-k size), `seed`, and `temperature` (flattens the fake distribution);
emits the same response shape coloom's parser consumes (tokens / token_logprobs /
top_logprobs / text_offset), so generated nodes carry full Token data.

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

    @app.post("/v1/completions")
    async def completions(request: Request) -> dict:
        body = await request.json()
        counter["requests"] += 1
        seed = body.get("seed", default_seed)
        rng = random.Random(seed if seed is not None else rng_entropy())
        n = int(body.get("n", 1))
        max_tokens = int(body.get("max_tokens", 16))
        top_k = int(body.get("logprobs") or 0) or 1
        temperature = float(body.get("temperature", 1.0))
        if delay > 0:
            await asyncio.sleep(delay * (0.5 + rng.random()))
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
