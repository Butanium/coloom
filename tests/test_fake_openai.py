"""The gpt-fake server must produce responses coloom's parser fully understands."""

from coloom.fake_openai import build_app
from coloom.inference import parse_completion_response
from coloom.models import Tokens
from fastapi.testclient import TestClient


def fake_request(body: dict) -> dict:
    app = build_app()
    with TestClient(app) as client:
        resp = client.post("/v1/completions", json=body)
    assert resp.status_code == 200
    return resp.json()


def test_fake_response_parses_to_token_nodes():
    body = {"model": "gpt-fake", "prompt": "Once", "n": 3, "max_tokens": 12, "logprobs": 5}
    nodes = parse_completion_response(fake_request(body), body)
    assert len(nodes) == 3
    for node in nodes:
        assert isinstance(node.content, Tokens)
        assert 1 <= len(node.content.tokens) <= 12
        for tok in node.content.tokens:
            assert tok.logprob is not None and tok.logprob <= 0
            assert len(tok.top_logprobs) == 5
            # the chosen token is among its own alternatives, ranked first
            assert tok.top_logprobs[0].text == tok.text
        assert node.creator.label == "gpt-fake"
        assert node.metadata["finish_reason"] in ("stop", "length")


def test_fake_seed_is_deterministic():
    body = {"model": "gpt-fake", "prompt": "x", "n": 2, "max_tokens": 8, "logprobs": 3, "seed": 7}
    r1, r2 = fake_request(body), fake_request(body)
    assert [c["text"] for c in r1["choices"]] == [c["text"] for c in r2["choices"]]
