"""Parser tests: real gpt4-base fixture + edge cases from the polyparser checklist."""

import json
import math
from pathlib import Path

import pytest
from coloom.inference import (
    InferenceError,
    parse_completion_choice,
    parse_completion_response,
    truncated_entropy,
)
from coloom.models import Snippet, Tokens, TopLogprob

FIXTURE = Path(__file__).parent / "fixtures" / "gpt4base_completion.json"


@pytest.fixture
def gpt4base():
    return json.loads(FIXTURE.read_text())


def test_fixture_roundtrip(gpt4base):
    nodes = parse_completion_response(gpt4base["response"], gpt4base["request"])
    assert len(nodes) == len(gpt4base["response"]["choices"])
    for node, choice in zip(nodes, gpt4base["response"]["choices"]):
        assert isinstance(node.content, Tokens)
        assert node.text == choice["text"]  # offset-slicing reconstructs exactly
        assert all(t.logprob is not None for t in node.content.tokens)
        k = gpt4base["request"]["logprobs"]
        assert all(len(t.top_logprobs) <= k for t in node.content.tokens)
        assert all(
            t.top_logprobs == sorted(t.top_logprobs, key=lambda x: -x.logprob)
            for t in node.content.tokens
        )
        assert node.creator.type == "model"
        assert node.creator.raw_request == gpt4base["request"]
        assert node.creator.raw_response["choice"] == choice
        assert node.metadata["finish_reason"] == choice["finish_reason"]


def test_fixture_offsets_are_prompt_relative(gpt4base):
    """Regression guard: real OpenAI text_offset starts at len(prompt), NOT 0.
    The parser must rebase — an `offsets[0] == 0` guard would dead-code the
    slicing path and store lossy token strings verbatim."""
    offsets = gpt4base["response"]["choices"][0]["logprobs"]["text_offset"]
    assert offsets[0] == len(gpt4base["request"]["prompt"]) > 0


def test_offset_slicing_recovers_lossy_token_strings():
    # OpenAI renders partial-UTF8 tokens as "bytes:\\xc3" etc.; offsets recover
    # the real text. Offsets are prompt-relative (prompt len 37 here).
    choice = {
        "text": "é!",
        "logprobs": {
            "tokens": ["bytes:\\xc3\\xa9", "!"],
            "token_logprobs": [-0.1, -0.2],
            "text_offset": [37, 38],
        },
    }
    content = parse_completion_choice(choice, None)
    assert [t.text for t in content.tokens] == ["é", "!"]
    assert content.text == "é!"


def test_bad_offsets_fall_back_to_token_strings():
    choice = {
        "text": "ab",
        "logprobs": {
            "tokens": ["a", "b"],
            "token_logprobs": [-0.1, -0.2],
            "text_offset": [37, 1000],  # out of range after rebase
        },
    }
    content = parse_completion_choice(choice, None)
    assert [t.text for t in content.tokens] == ["a", "b"]


async def test_echo_rejected():
    from coloom.config import EndpointConfig
    from coloom.inference import generate

    endpoint = EndpointConfig(base_url="http://127.0.0.1:1", model="m")
    with pytest.raises(InferenceError, match="echo"):
        await generate(endpoint, "p", {"echo": True})


async def test_unreachable_endpoint_is_inference_error():
    from coloom.config import EndpointConfig
    from coloom.inference import generate

    endpoint = EndpointConfig(base_url="http://127.0.0.1:1", model="m")
    with pytest.raises(InferenceError, match="failed"):
        await generate(endpoint, "p", timeout=2.0)


def test_no_logprobs_degrades_to_snippet():
    content = parse_completion_choice({"text": "hello"}, None)
    assert content == Snippet(text="hello")


def test_null_token_logprob_tolerated():
    choice = {
        "text": "ab",
        "logprobs": {
            "tokens": ["a", "b"],
            "token_logprobs": [None, -0.5],  # vLLM prompt-echo pattern
            "top_logprobs": None,
            "text_offset": [0, 1],
        },
    }
    content = parse_completion_choice(choice, 5)
    assert isinstance(content, Tokens)
    assert content.tokens[0].logprob is None
    assert content.tokens[1].logprob == -0.5


def test_length_mismatch_drops_tops_keeps_tokens():
    choice = {
        "text": "ab",
        "logprobs": {
            "tokens": ["a", "b"],
            "token_logprobs": [-0.1, -0.2],
            "top_logprobs": [{"a": -0.1}],  # wrong length
        },
    }
    content = parse_completion_choice(choice, 5)
    assert isinstance(content, Tokens)
    assert all(t.top_logprobs == [] for t in content.tokens)


def test_tokens_logprobs_mismatch_degrades_to_snippet():
    choice = {
        "text": "ab",
        "logprobs": {"tokens": ["a"], "token_logprobs": [-0.1, -0.2]},
    }
    assert parse_completion_choice(choice, 5) == Snippet(text="ab")


def test_vllm_token_ids_zipped_on_exact_match_only():
    base = {
        "text": "ab",
        "logprobs": {"tokens": ["a", "b"], "token_logprobs": [-0.1, -0.2]},
    }
    ok = parse_completion_choice({**base, "token_ids": [5, 7]}, None)
    assert [t.token_id for t in ok.tokens] == [5, 7]
    bad = parse_completion_choice({**base, "token_ids": [5]}, None)
    assert [t.token_id for t in bad.tokens] == [None, None]


def test_top_logprobs_sorted_and_truncated():
    choice = {
        "text": "a",
        "logprobs": {
            "tokens": ["a"],
            "token_logprobs": [-0.1],
            "top_logprobs": [{"x": -3.0, "a": -0.1, "y": -1.0}],
        },
    }
    content = parse_completion_choice(choice, 2)
    tops = content.tokens[0].top_logprobs
    assert [(t.text, t.logprob) for t in tops] == [("a", -0.1), ("y", -1.0)]


def test_malformed_top_entry_clears_position():
    choice = {
        "text": "a",
        "logprobs": {
            "tokens": ["a"],
            "token_logprobs": [-0.1],
            "top_logprobs": [{"a": "not-a-number"}],
        },
    }
    content = parse_completion_choice(choice, 5)
    assert content.tokens[0].top_logprobs == []
    assert content.tokens[0].entropy is None


def test_truncated_entropy():
    # uniform over 2 with half the mass each: -2 * 0.5*ln(0.5) = ln(2)
    tops = [
        TopLogprob(text="a", logprob=math.log(0.5)),
        TopLogprob(text="b", logprob=math.log(0.5)),
    ]
    assert truncated_entropy(tops) == pytest.approx(math.log(2))


def test_empty_choices_raises():
    with pytest.raises(InferenceError):
        parse_completion_response({"choices": []}, {})
