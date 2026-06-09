"""Shared fixtures: a fake OpenAI-compatible completions server (serves the
captured gpt4-base fixture over real HTTP) and a coloom test app wired to it."""

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request

FIXTURE = Path(__file__).parent / "fixtures" / "gpt4base_completion.json"


@pytest.fixture(scope="session")
def fake_openai_url():
    """A real HTTP server replaying the captured gpt4-base completions response."""
    payload = json.loads(FIXTURE.read_text())["response"]
    app = FastAPI()
    received: list[dict] = []

    @app.post("/v1/completions")
    async def completions(request: Request):
        received.append(await request.json())
        return payload

    app.state.received = received

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "fake openai server failed to start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}/v1", app
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def live_server(tmp_path, fake_openai_url):
    """A real coloom server over HTTP (for CLI / WS end-to-end tests)."""
    from coloom.config import ColoomConfig, EndpointConfig
    from coloom.server.app import create_app
    from coloom.store import WeaveStore

    url, _ = fake_openai_url
    config = ColoomConfig(
        endpoints={
            "fake": EndpointConfig(
                base_url=url,
                model="gpt-4-base",
                params={"temperature": 1.0, "max_tokens": 24, "logprobs": 5, "n": 2},
            )
        },
        default_preset="fake",
    )
    store = WeaveStore(tmp_path / "live.sqlite")
    app = create_app(store, config)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "coloom server failed to start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    store.close()
