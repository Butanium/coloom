"""FastAPI server: the authority over the canonical weave.

Every mutation goes through the WeaveStore (SQLite, transactional) and emits
events; the EventHub pushes any newly logged events to all WebSocket clients,
and `GET /events?since=` serves the same stream to polling clients.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from coloom.config import DEFAULT_PARAMS, ColoomConfig, ConfigError
from coloom.inference import InferenceError, generate
from coloom.models import (
    Creator,
    Cursor,
    HumanCreator,
    Node,
    NodeContent,
    Snippet,
    Weave,
    WeaveInfo,
)
from coloom.setups import (
    CreateModelSetup,
    CreateSamplerSetup,
    ModelSetup,
    SamplerSetup,
    UpdateModelSetup,
    UpdateSamplerSetup,
    resolve_sampler,
)
from coloom.store import Conflict, NotFound, WeaveStore, WeaveStoreError, current_origin

logger = logging.getLogger("coloom.server")


class EventHub:
    """Fans newly logged store events out to connected WebSocket clients.

    Each client gets a bounded queue drained by its own connection handler, so a
    stalled consumer never blocks mutations or other clients — when its queue
    overflows the client is dropped (it can resync via `GET /events?since=`).
    """

    QUEUE_SIZE = 512

    def __init__(self, store: WeaveStore):
        self.store = store
        self.last_seq = store.last_event_seq()
        self.subscribers: dict[asyncio.Queue, str | None] = {}  # queue -> weave filter
        self.lock = asyncio.Lock()

    def subscribe(self, weave_id: str | None) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self.subscribers[queue] = weave_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.pop(queue, None)

    async def push_new(self) -> None:
        async with self.lock:
            events = self.store.get_events(since=self.last_seq)
            if not events:
                return
            self.last_seq = events[-1]["seq"]
        for queue, weave_filter in list(self.subscribers.items()):
            for event in events:
                if weave_filter is not None and event["weave_id"] != weave_filter:
                    continue
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("dropping slow websocket subscriber")
                    self.unsubscribe(queue)
                    break


# ------------------------------------------------------------ request models


class CreateWeaveRequest(BaseModel):
    title: str = "Untitled weave"
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateWeaveRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None


class AddNodeRequest(BaseModel):
    text: str = ""
    # full typed content (e.g. Tokens with logprobs for counterfactual branches);
    # when given it wins over `text`
    content: NodeContent | None = None
    parent_id: str | None = None
    creator: Creator = Field(default_factory=lambda: HumanCreator(label="anonymous"))
    move_cursor: str | None = None  # cursor name to move to the new node
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateNodeRequest(BaseModel):
    content: NodeContent
    metadata: dict[str, Any] | None = None  # when given, replaces node metadata


class SetCursorRequest(BaseModel):
    node_id: str
    moved_by: str | None = None  # who moved it (the "look here" gesture when ≠ name)


class BookmarkRequest(BaseModel):
    bookmarked: bool


class SplitRequest(BaseModel):
    at: int


class GenRequest(BaseModel):
    node_id: str | None = None  # default: the requester's cursor (`cursor`)
    cursor: str | None = None  # requester's cursor name
    sampler_id: str | None = None  # a sampler setup; beats `preset` when given
    preset: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    move_cursor: bool = False  # move `cursor` to the first generated node


class PutProfileRequest(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class ThreadResponse(BaseModel):
    path: list[str]
    content: str
    nodes: list[Node]


def create_app(store: WeaveStore, config: ColoomConfig | None = None) -> FastAPI:
    app = FastAPI(title="coloom server")
    config = config or ColoomConfig()
    hub = EventHub(store)
    app.state.store = store
    app.state.hub = hub
    app.state.config = config

    @app.middleware("http")
    async def stamp_origin(request: Request, call_next):
        # per-tab client id → request-scoped origin, stamped into event payloads
        # (contextvars propagate into call_next's task and threadpool endpoints)
        token = current_origin.set(request.headers.get("x-coloom-client"))
        try:
            return await call_next(request)
        finally:
            current_origin.reset(token)

    def _404(exc: NotFound) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    # ------------------------------------------------------------ setups

    @app.get("/setups")
    def list_setups() -> dict[str, Any]:
        return {
            "models": [m.redacted() for m in store.list_model_setups()],
            "samplers": store.list_sampler_setups(),
        }

    @app.post("/setups/models", status_code=201)
    def create_model_setup(req: CreateModelSetup) -> ModelSetup:
        try:
            setup = ModelSetup(**req.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return store.create_model_setup(setup).redacted()

    @app.patch("/setups/models/{setup_id}")
    def update_model_setup(setup_id: str, req: UpdateModelSetup) -> ModelSetup:
        fields = req.model_dump(exclude_unset=True)  # omitted = unchanged; null = clear
        try:
            return store.update_model_setup(setup_id, fields).redacted()
        except NotFound as e:
            raise _404(e)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/setups/models/{setup_id}", status_code=204)
    def delete_model_setup(setup_id: str) -> None:
        try:
            store.delete_model_setup(setup_id)
        except NotFound as e:
            raise _404(e)
        except Conflict as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.post("/setups/samplers", status_code=201)
    def create_sampler_setup(req: CreateSamplerSetup) -> SamplerSetup:
        setup = SamplerSetup(**req.model_dump())
        try:
            return store.create_sampler_setup(setup)
        except NotFound as e:  # model_setup_id references a missing model
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/setups/samplers/{setup_id}")
    def update_sampler_setup(setup_id: str, req: UpdateSamplerSetup) -> SamplerSetup:
        fields = req.model_dump(exclude_unset=True)
        try:
            return store.update_sampler_setup(setup_id, fields)
        except NotFound as e:
            if "model setup" in str(e):  # bad reference in the update
                raise HTTPException(status_code=400, detail=str(e))
            raise _404(e)

    @app.delete("/setups/samplers/{setup_id}", status_code=204)
    def delete_sampler_setup(setup_id: str) -> None:
        try:
            store.delete_sampler_setup(setup_id)
        except NotFound as e:
            raise _404(e)

    # ------------------------------------------------------------ profiles
    # Per-person client settings (keybindings, ui prefs, active generators…)
    # stored server-side so a profile roams across browsers. Opaque JSON.

    @app.get("/profiles")
    def list_profiles() -> list[dict[str, Any]]:
        return store.list_profiles()

    @app.get("/profiles/{name}")
    def get_profile(name: str) -> dict[str, Any]:
        try:
            return store.get_profile(name)
        except NotFound as e:
            raise _404(e)

    @app.put("/profiles/{name}")
    def put_profile(name: str, req: PutProfileRequest) -> dict[str, Any]:
        if not name.strip():
            raise HTTPException(status_code=400, detail="profile name required")
        return store.put_profile(name, req.settings)

    @app.delete("/profiles/{name}", status_code=204)
    def delete_profile(name: str) -> None:
        try:
            store.delete_profile(name)
        except NotFound as e:
            raise _404(e)

    # ------------------------------------------------------------ weaves

    @app.get("/weaves")
    def list_weaves() -> list[WeaveInfo]:
        return store.list_weaves()

    @app.post("/weaves", status_code=201)
    async def create_weave(req: CreateWeaveRequest) -> WeaveInfo:
        info = store.create_weave(req.title, req.description, req.metadata)
        await hub.push_new()
        return info

    @app.get("/weaves/{weave_id}")
    def get_weave(weave_id: str) -> Weave:
        try:
            return store.get_weave(weave_id)
        except NotFound as e:
            raise _404(e)

    @app.patch("/weaves/{weave_id}")
    async def update_weave(weave_id: str, req: UpdateWeaveRequest) -> WeaveInfo:
        try:
            info = store.update_weave_info(
                weave_id, req.title, req.description, req.metadata
            )
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return info

    @app.delete("/weaves/{weave_id}", status_code=204)
    async def delete_weave(weave_id: str) -> None:
        try:
            store.delete_weave(weave_id)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()

    # ------------------------------------------------------------ nodes

    @app.get("/weaves/{weave_id}/nodes/{node_id}")
    def get_node(weave_id: str, node_id: str) -> Node:
        try:
            return store.get_node(weave_id, node_id)
        except NotFound as e:
            raise _404(e)

    @app.post("/weaves/{weave_id}/nodes", status_code=201)
    async def add_node(weave_id: str, req: AddNodeRequest) -> Node:
        try:
            node = store.add_node(
                weave_id,
                req.content if req.content is not None else Snippet(text=req.text),
                creator=req.creator,
                parent_id=req.parent_id,
                move_cursor=req.move_cursor,
                metadata=req.metadata,
            )
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return node

    @app.delete("/weaves/{weave_id}/nodes/{node_id}")
    async def remove_node(weave_id: str, node_id: str) -> dict[str, Any]:
        try:
            removed = store.remove_node(weave_id, node_id)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return {"removed": removed}

    @app.post("/weaves/{weave_id}/nodes/{node_id}/split")
    async def split_node(
        weave_id: str, node_id: str, req: SplitRequest
    ) -> dict[str, Node]:
        try:
            head, tail = store.split_node(weave_id, node_id, req.at)
        except NotFound as e:
            raise _404(e)
        except WeaveStoreError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await hub.push_new()
        return {"head": head, "tail": tail}

    @app.patch("/weaves/{weave_id}/nodes/{node_id}")
    async def update_node(
        weave_id: str, node_id: str, req: UpdateNodeRequest
    ) -> Node:
        try:
            node = store.update_node_content(
                weave_id, node_id, req.content, req.metadata
            )
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return node

    @app.put("/weaves/{weave_id}/nodes/{node_id}/bookmark")
    async def set_bookmark(
        weave_id: str, node_id: str, req: BookmarkRequest
    ) -> dict[str, Any]:
        try:
            store.set_bookmarked(weave_id, node_id, req.bookmarked)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return {"node_id": node_id, "bookmarked": req.bookmarked}

    # ------------------------------------------------------------ cursors

    @app.get("/weaves/{weave_id}/cursors")
    def list_cursors(weave_id: str) -> dict[str, Cursor]:
        try:
            return store.list_cursors(weave_id)
        except NotFound as e:
            raise _404(e)

    @app.put("/weaves/{weave_id}/cursors/{name}")
    async def set_cursor(weave_id: str, name: str, req: SetCursorRequest) -> Cursor:
        try:
            cursor = store.set_cursor(weave_id, name, req.node_id, req.moved_by)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return cursor

    @app.delete("/weaves/{weave_id}/cursors/{name}", status_code=204)
    async def delete_cursor(weave_id: str, name: str) -> None:
        try:
            store.delete_cursor(weave_id, name)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()

    @app.get("/weaves/{weave_id}/cursors/{name}/thread")
    def get_cursor_thread(weave_id: str, name: str) -> ThreadResponse:
        try:
            nodes = store.get_cursor_thread(weave_id, name)
        except NotFound as e:
            raise _404(e)
        return ThreadResponse(
            path=[n.id for n in nodes],
            content="".join(n.text for n in nodes),
            nodes=nodes,
        )

    # ------------------------------------------------------------ generation

    @app.get("/presets")
    def list_presets() -> dict[str, Any]:
        """Selectable generation presets (presets shadow same-named endpoints,
        matching `resolve_preset`), with merged params so the UI can show them."""
        names = list(config.presets) + [
            n for n in config.endpoints if n not in config.presets
        ]
        presets = {}
        for name in names:
            endpoint, params = config.resolve_preset(name)
            # base_url/api_key_env let the UI clone a preset into an editable
            # model setup ("start from an existing one, then edit")
            presets[name] = {
                "model": endpoint.model,
                "params": params,
                "base_url": endpoint.base_url,
                "api_key_env": endpoint.api_key_env,
            }
        return {"presets": presets, "default_preset": config.default_preset}

    @app.post("/weaves/{weave_id}/gen", status_code=201)
    async def gen(weave_id: str, req: GenRequest) -> list[Node]:
        if req.move_cursor and req.cursor is None:
            raise HTTPException(
                status_code=400, detail="move_cursor requires a cursor name"
            )
        parent_id = req.node_id
        if parent_id is None:
            if req.cursor is None:
                raise HTTPException(
                    status_code=400, detail="give a node_id or a cursor name"
                )
            try:
                parent_id = store.get_cursor(weave_id, req.cursor).node_id
            except NotFound as e:
                raise _404(e)
        sampler_name = None
        try:
            prompt = store.get_thread_content(weave_id, parent_id)
            if req.sampler_id is not None:
                # sampler_id beats preset beats default preset
                sampler = store.get_sampler_setup(req.sampler_id)
                model_setup = store.get_model_setup(sampler.model_setup_id)
                sampler_name = sampler.name
                endpoint, params = resolve_sampler(
                    model_setup, sampler, DEFAULT_PARAMS, req.params
                )
            else:
                endpoint, base_params = config.resolve_preset(req.preset)
                params = {**base_params, **req.params}
        except NotFound as e:
            raise _404(e)
        except KeyError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # presence events: who is generating where (live indicator + activity feed)
        gen_info: dict[str, Any] = {
            "gen_id": uuid.uuid4().hex,
            "requester": req.cursor,
            "node_id": parent_id,
            "preset": None if req.sampler_id else (req.preset or config.default_preset),
        }
        if sampler_name is not None:
            gen_info["sampler"] = sampler_name
        store.log_activity(weave_id, "gen_started", gen_info)
        await hub.push_new()
        try:
            generated = await generate(endpoint, prompt, params)
        except (InferenceError, ConfigError) as e:
            store.log_activity(weave_id, "gen_finished", {**gen_info, "error": str(e)})
            await hub.push_new()
            raise HTTPException(status_code=502, detail=str(e))
        nodes = []
        try:
            for i, node in enumerate(generated):
                nodes.append(
                    store.add_node(
                        weave_id,
                        node.content,
                        creator=node.creator,
                        parent_id=parent_id,
                        move_cursor=req.cursor if req.move_cursor and i == 0 else None,
                        metadata=node.metadata,
                    )
                )
        except NotFound as e:
            # parent/weave was removed while the model was generating; any
            # earlier siblings were attached before the removal and got
            # cascade-deleted with the parent's subtree.
            try:
                store.log_activity(
                    weave_id, "gen_finished", {**gen_info, "error": str(e)}
                )
            except NotFound:
                pass  # the whole weave is gone — nowhere to log the finish
            raise HTTPException(
                status_code=409,
                detail=f"target disappeared during generation: {e}",
            )
        else:
            store.log_activity(
                weave_id,
                "gen_finished",
                {**gen_info, "node_ids": [n.id for n in nodes]},
            )
        finally:
            await hub.push_new()
        return nodes

    # ------------------------------------------------------------ events

    @app.get("/events")
    def get_events(
        since: int = Query(0), weave_id: str | None = None, limit: int = 1000
    ) -> dict[str, Any]:
        events = store.get_events(weave_id=weave_id, since=since, limit=limit)
        return {
            "events": events,
            "cursor": events[-1]["seq"] if events else since,
        }

    @app.websocket("/ws")
    async def ws_events(ws: WebSocket, weave_id: str | None = None) -> None:
        await ws.accept()
        queue = hub.subscribe(weave_id)

        async def sender() -> None:
            while True:
                await ws.send_json(await queue.get())

        async def receiver() -> None:  # detects disconnect; inbound msgs ignored
            while True:
                await ws.receive_text()

        tasks = {asyncio.create_task(sender()), asyncio.create_task(receiver())}
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:  # surface non-disconnect errors
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    return app
