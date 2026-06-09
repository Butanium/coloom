"""FastAPI server: the authority over the canonical weave.

Every mutation goes through the WeaveStore (SQLite, transactional) and emits
events; the EventHub pushes any newly logged events to all WebSocket clients,
and `GET /events?since=` serves the same stream to polling clients.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from coloom.config import ColoomConfig, ConfigError
from coloom.inference import InferenceError, generate
from coloom.models import Creator, HumanCreator, Node, Snippet, Weave, WeaveInfo
from coloom.store import NotFound, WeaveStore, WeaveStoreError

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


class AddNodeRequest(BaseModel):
    text: str
    parent_id: str | None = None
    creator: Creator = Field(default_factory=lambda: HumanCreator(label="anonymous"))
    set_active: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SetActiveRequest(BaseModel):
    node_id: str | None


class BookmarkRequest(BaseModel):
    bookmarked: bool


class SplitRequest(BaseModel):
    at: int


class GenRequest(BaseModel):
    node_id: str | None = None  # default: tip of the active path
    preset: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    set_active: bool = False  # make the first generated node the new active tip


class ActiveResponse(BaseModel):
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

    def _404(exc: NotFound) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

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
                Snippet(text=req.text),
                creator=req.creator,
                parent_id=req.parent_id,
                set_active=req.set_active,
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

    # ------------------------------------------------------------ active path

    @app.get("/weaves/{weave_id}/active")
    def get_active(weave_id: str) -> ActiveResponse:
        try:
            nodes = store.get_active_thread(weave_id)
        except NotFound as e:
            raise _404(e)
        return ActiveResponse(
            path=[n.id for n in nodes],
            content="".join(n.text for n in nodes),
            nodes=nodes,
        )

    @app.put("/weaves/{weave_id}/active")
    async def set_active(weave_id: str, req: SetActiveRequest) -> dict[str, list[str]]:
        try:
            path = store.set_active(weave_id, req.node_id)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return {"path": path}

    # ------------------------------------------------------------ generation

    @app.post("/weaves/{weave_id}/gen", status_code=201)
    async def gen(weave_id: str, req: GenRequest) -> list[Node]:
        try:
            info = store.get_weave_info(weave_id)
        except NotFound as e:
            raise _404(e)
        parent_id = req.node_id
        if parent_id is None:
            if not info.active_path:
                raise HTTPException(
                    status_code=400,
                    detail="no node_id given and the weave has no active path",
                )
            parent_id = info.active_path[-1]
        try:
            prompt = store.get_thread_content(weave_id, parent_id)
            endpoint, params = config.resolve_preset(req.preset)
        except NotFound as e:
            raise _404(e)
        except KeyError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            generated = await generate(endpoint, prompt, {**params, **req.params})
        except (InferenceError, ConfigError) as e:
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
                        set_active=req.set_active and i == 0,
                        metadata=node.metadata,
                    )
                )
        except NotFound as e:
            # parent/weave was removed while the model was generating; any
            # earlier siblings were attached before the removal and got
            # cascade-deleted with the parent's subtree.
            raise HTTPException(
                status_code=409,
                detail=f"target disappeared during generation: {e}",
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
