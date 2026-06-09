"""Smoke: coloom.inference.generate against a live endpoint; saves the raw
response as a test fixture and asserts logprob structure.

Run: uv run scripts/small-smokes/smoke_generate.py [--model gpt-4-base] [--save-fixture]
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from coloom.config import EndpointConfig
from coloom.inference import generate
from coloom.models import Tokens

REPO = Path(__file__).resolve().parents[2]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-4-base")
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument("--prompt", default="The loom hummed softly as the weaver")
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("-n", type=int, default=2)
    parser.add_argument("--save-fixture", action="store_true")
    args = parser.parse_args()

    if "OPENAI_API_KEY" not in os.environ:
        env = (REPO / ".env").read_text()
        for line in env.splitlines():
            if line.startswith("OPENAI_API_KEY="):
                os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()

    endpoint = EndpointConfig(
        base_url=args.base_url,
        model=args.model,
        api_key_env="OPENAI_API_KEY",
        params={
            "temperature": 1.0,
            "max_tokens": args.max_tokens,
            "logprobs": 5,
            "n": args.n,
        },
    )
    nodes = await generate(endpoint, args.prompt)

    assert len(nodes) == args.n, f"expected {args.n} nodes, got {len(nodes)}"
    for node in nodes:
        assert isinstance(node.content, Tokens), (
            f"expected Tokens, got {node.content.type}"
        )
        assert node.content.tokens, "no tokens parsed"
        tok = node.content.tokens[0]
        assert tok.logprob is not None, "first token has no logprob"
        assert tok.top_logprobs, "no top_logprobs parsed"
        assert tok.entropy is not None and tok.entropy >= 0
        assert node.creator.type == "model"
        assert node.creator.raw_request is not None
        print(
            f"--- node ({node.metadata}) avg_logprob="
            f"{sum(t.logprob for t in node.content.tokens) / len(node.content.tokens):.3f}"
        )
        print(repr(node.text))
        print(
            f"  first token: {tok.text!r} lp={tok.logprob:.3f} ent~{tok.entropy:.3f} "
            f"tops={[(t.text, round(t.logprob, 2)) for t in tok.top_logprobs]}"
        )

    if args.save_fixture:
        fixture_dir = REPO / "tests" / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        raw = nodes[0].creator.raw_response
        assert raw is not None
        # reassemble a full-shape response (choice of node 0 only) for parser tests
        full = {k: v for k, v in raw.items() if k != "choice"}
        full["choices"] = [n.creator.raw_response["choice"] for n in nodes]
        out = fixture_dir / "gpt4base_completion.json"
        out.write_text(
            json.dumps(
                {"request": nodes[0].creator.raw_request, "response": full}, indent=2
            )
        )
        print(f"fixture saved to {out}")

    print("SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
