"""FastAPI server: the authority over the canonical weave.

Every mutation goes through the WeaveStore (SQLite, transactional) and emits
events; the EventHub pushes any newly logged events to all WebSocket clients,
and `GET /events?since=` serves the same stream to polling clients.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from coloom.config import DEFAULT_PARAMS, ColoomConfig, ConfigError, EndpointConfig
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
    new_id,
)
from coloom.generators import (
    CreateGeneratorRequest,
    CreateTemplateRequest,
    Generator,
    GeneratorWithResolved,
    Template,
    UpdateGeneratorRequest,
    UpdateTemplateRequest,
)
from coloom.store import (
    GLOBAL_EVENT_SCOPE,
    BadReference,
    Conflict,
    Forbidden,
    NotFound,
    WeaveStore,
    WeaveStoreError,
    current_origin,
    current_profile,
)

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
                # global (template/generator) events reach every subscriber;
                # weave events respect the subscriber's filter
                if (
                    weave_filter is not None
                    and event["weave_id"] != weave_filter
                    and event["weave_id"] != GLOBAL_EVENT_SCOPE
                ):
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
    generator_id: str | None = None  # required: the generator to run (clean break
    # from the old sampler_id/preset selection — yaml presets only exist as
    # builtin templates now, and templates never generate directly)
    params: dict[str, Any] = Field(default_factory=dict)  # per-request overrides
    move_cursor: bool = False  # move `cursor` to the first generated node


class PutProfileRequest(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class ProbeEndpointRequest(BaseModel):
    # Two shapes: literal {base_url, api_key?|api_key_env?} for new/edited
    # creds, or {template_id|generator_id, base_url?} to re-probe an EXISTING
    # row whose stored key the client only ever sees as "***" — the server
    # resolves the stored/inherited credentials (joint resolution, same as
    # /gen). An explicit base_url always wins over the stored one ("user is
    # editing the URL field right now").
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    template_id: str | None = None
    generator_id: str | None = None


class ThreadResponse(BaseModel):
    path: list[str]
    content: str
    nodes: list[Node]


def create_app(
    store: WeaveStore,
    config: ColoomConfig | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="coloom server")
    config = config or ColoomConfig()
    hub = EventHub(store)
    app.state.store = store
    app.state.hub = hub
    app.state.config = config

    @app.middleware("http")
    async def stamp_origin(request: Request, call_next):
        # per-tab client id → request-scoped origin, actor profile → `by`; both
        # stamped into event payloads (contextvars propagate into call_next's
        # task and threadpool endpoints)
        origin_token = current_origin.set(request.headers.get("x-coloom-client"))
        # profile names may be non-ASCII ("clément") which HTTP headers can't
        # carry raw: the header value is percent-encoded UTF-8
        raw_profile = request.headers.get("x-coloom-profile")
        profile_token = current_profile.set(
            unquote(raw_profile) if raw_profile else None
        )
        try:
            return await call_next(request)
        finally:
            current_origin.reset(origin_token)
            current_profile.reset(profile_token)

    def _404(exc: NotFound) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    # boot: yaml presets → builtin templates (upsert by name; silent — config
    # sync, not a user mutation), then seed builtin-derived generators for
    # every active profile so new yaml entries appear for everyone.
    preset_names = list(config.presets) or list(config.endpoints)
    for preset_name in preset_names:
        endpoint, merged_params = config.resolve_preset(preset_name)
        store.upsert_builtin_template(
            name=preset_name,
            base_url=endpoint.base_url,
            model=endpoint.model,
            api_key=endpoint.api_key,
            api_key_env=endpoint.api_key_env,
            params=merged_params,
        )
    for prof in store.list_profiles():
        store.seed_profile_generators(prof["name"], log=False)

    def _generator_out(g: Generator) -> GeneratorWithResolved:
        resolved = store.resolve_generator(g.id)
        return GeneratorWithResolved(
            **g.redacted().model_dump(),
            resolved=resolved.redacted(),
            usable=resolved.usable,
        )

    # ------------------------------------------------------------ templates

    @app.get("/templates")
    def list_templates() -> list[Template]:
        return [t.redacted() for t in store.list_templates()]

    @app.post("/templates", status_code=201)
    async def create_template(req: CreateTemplateRequest) -> Template:
        if req.from_generator is not None:
            # promote: materialize the generator's RESOLVED fields
            try:
                gen = store.get_generator(req.from_generator)
            except NotFound as e:
                raise HTTPException(status_code=400, detail=str(e))
            resolved = store.resolve_generator(gen.id)
            if not resolved.usable:
                raise HTTPException(
                    status_code=400,
                    detail=f"generator {gen.name!r} does not resolve to a usable"
                    " base_url + model; cannot promote",
                )
            template = Template(
                name=req.name or gen.name,
                base_url=resolved.base_url,
                model=resolved.model,
                api_key=resolved.api_key,
                api_key_env=resolved.api_key_env,
                params=resolved.params,
            )
        else:
            missing = [
                f for f in ("name", "base_url", "model") if getattr(req, f) is None
            ]
            if missing:
                raise HTTPException(
                    status_code=400, detail=f"missing fields: {', '.join(missing)}"
                )
            try:
                template = Template(
                    name=req.name,
                    base_url=req.base_url,
                    model=req.model,
                    api_key=req.api_key,
                    api_key_env=req.api_key_env,
                    params=req.params,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        store.create_template(template)
        await hub.push_new()
        return template.redacted()

    @app.patch("/templates/{template_id}")
    async def update_template(template_id: str, req: UpdateTemplateRequest) -> Template:
        fields = req.model_dump(exclude_unset=True)  # omitted = unchanged; null = clear
        try:
            template = store.update_template(template_id, fields)
        except NotFound as e:
            raise _404(e)
        except Forbidden as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await hub.push_new()
        return template.redacted()

    @app.delete("/templates/{template_id}", status_code=204)
    async def delete_template(template_id: str) -> None:
        try:
            store.delete_template(template_id)
        except NotFound as e:
            raise _404(e)
        except Forbidden as e:
            raise HTTPException(status_code=403, detail=str(e))
        await hub.push_new()

    # ------------------------------------------------------------ generators

    @app.get("/generators")
    def list_generators(profile: str = Query(...)) -> list[GeneratorWithResolved]:
        return [_generator_out(g) for g in store.list_generators(profile)]

    @app.get("/generators/{generator_id}")
    def get_generator(generator_id: str) -> GeneratorWithResolved:
        try:
            return _generator_out(store.get_generator(generator_id))
        except NotFound as e:
            raise _404(e)

    @app.post("/generators", status_code=201)
    async def create_generator(req: CreateGeneratorRequest) -> GeneratorWithResolved:
        try:
            if req.from_ is not None:
                if req.mode is None:
                    raise HTTPException(
                        status_code=400, detail="`from` requires `mode`"
                    )
                gen = _generator_from_source(req)
            else:
                if req.name is None:
                    raise HTTPException(status_code=400, detail="name required")
                gen = Generator(
                    profile=req.profile,
                    name=req.name,
                    parent=req.parent,
                    base_url=req.base_url,
                    model=req.model,
                    api_key=req.api_key,
                    api_key_env=req.api_key_env,
                    params=req.params,
                )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            store.create_generator(gen)
        except BadReference as e:
            raise HTTPException(status_code=400, detail=str(e))
        await hub.push_new()
        return _generator_out(gen)

    def _generator_from_source(req: CreateGeneratorRequest) -> Generator:
        """The {from, mode} creation shapes (docs/generators-api.md):
        inherit → parent = from, empty overrides; duplicate → literal row copy
        (generator source) or full field copy with no parent (template source)."""
        assert req.from_ is not None and req.mode is not None
        try:
            if req.from_.kind == "template":
                source: Generator | Template = store.get_template(req.from_.id)
            else:
                source = store.get_generator(req.from_.id)
        except NotFound as e:
            raise HTTPException(status_code=400, detail=str(e))
        name = req.name or source.name
        if req.mode == "inherit":
            return Generator(profile=req.profile, name=name, parent=req.from_)
        if isinstance(source, Template):
            return Generator(
                profile=req.profile,
                name=name,
                parent=None,
                base_url=source.base_url,
                model=source.model,
                api_key=source.api_key,
                api_key_env=source.api_key_env,
                params=dict(source.params),
            )
        return source.model_copy(
            update={
                "id": new_id(),
                "profile": req.profile,
                "name": name,
                "params": dict(source.params),
                "migrated_from": None,  # a duplicate is not the migrated row
            }
        )

    @app.patch("/generators/{generator_id}")
    async def update_generator(
        generator_id: str, req: UpdateGeneratorRequest
    ) -> GeneratorWithResolved:
        fields = req.model_dump(exclude_unset=True)
        try:
            gen = store.update_generator(generator_id, fields)
        except NotFound as e:
            raise _404(e)
        except BadReference as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await hub.push_new()
        return _generator_out(gen)

    @app.delete("/generators/{generator_id}", status_code=204)
    async def delete_generator(generator_id: str) -> None:
        try:
            store.delete_generator(generator_id)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()

    # ------------------------------------------------------------ probe

    @app.post("/probe-endpoint")
    async def probe_endpoint(req: ProbeEndpointRequest) -> dict[str, Any]:
        """Reachability + model suggestions for the edit form: server-side GET
        of {base_url}/models (the browser can't — CORS — and must not — secrets
        — call third-party endpoints directly). Never echoes the key."""
        if req.template_id is not None and req.generator_id is not None:
            raise HTTPException(
                status_code=400,
                detail="template_id and generator_id are mutually exclusive",
            )
        by_id = req.template_id is not None or req.generator_id is not None
        if by_id and (req.api_key is not None or req.api_key_env is not None):
            raise HTTPException(
                status_code=400,
                detail="stored-credential probe (template_id/generator_id) and"
                " literal credentials are mutually exclusive",
            )
        if req.api_key is not None and req.api_key_env is not None:
            raise HTTPException(
                status_code=400,
                detail="api_key and api_key_env are mutually exclusive",
            )
        base_url, api_key, api_key_env = req.base_url, req.api_key, req.api_key_env
        if req.template_id is not None:
            try:
                template = store.get_template(req.template_id)
            except NotFound as e:
                raise HTTPException(status_code=400, detail=str(e))
            base_url = base_url or template.base_url
            api_key, api_key_env = template.api_key, template.api_key_env
        elif req.generator_id is not None:
            try:
                resolved = store.resolve_generator(req.generator_id)
            except NotFound as e:
                raise HTTPException(status_code=400, detail=str(e))
            base_url = base_url or resolved.base_url
            api_key, api_key_env = resolved.api_key, resolved.api_key_env
        if not base_url:
            if by_id:  # the stored row genuinely has no URL — operational, not misuse
                return {"ok": False, "error": "no base_url stored or given", "models": []}
            raise HTTPException(
                status_code=400,
                detail="base_url or template_id/generator_id required",
            )
        key = api_key
        if key is None and api_key_env:
            key = os.environ.get(api_key_env)
            if not key:
                return {
                    "ok": False,
                    "error": f"env var {api_key_env!r} is not set on the server",
                    "models": [],
                }
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = base_url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=3.0) as probe_client:
                resp = await probe_client.get(url, headers=headers)
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"cannot reach {url}: {e!r}", "models": []}
        if resp.status_code in (404, 405, 501):
            # endpoint is up but doesn't implement /models (llama.cpp variants)
            return {"ok": True, "error": None, "models": []}
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {resp.status_code} from {url}: {resp.text[:200]}",
                "models": [],
            }
        try:
            data = resp.json()["data"]
            models = [m["id"] for m in data]
        except (ValueError, KeyError, TypeError):
            return {
                "ok": False,
                "error": f"unparseable /models response from {url}",
                "models": [],
            }
        return {"ok": True, "error": None, "models": models}

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
    async def put_profile(name: str, req: PutProfileRequest) -> dict[str, Any]:
        if not name.strip():
            raise HTTPException(status_code=400, detail="profile name required")
        profile = store.put_profile(name, req.settings)
        # every profile gets one generator per builtin template; idempotent
        # (generator_seeds), so frequent settings saves are a cheap no-op
        store.seed_profile_generators(name)
        await hub.push_new()
        return profile

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
        """Soft-delete the node + its subtree (restorable via .../restore).
        An already-deleted node is invisible → 404."""
        try:
            removed, moved_cursors = store.remove_node(weave_id, node_id)
        except NotFound as e:
            raise _404(e)
        await hub.push_new()
        return {
            "deleted_node_ids": removed,
            "moved_cursors": moved_cursors,
            # legacy alias (pre-soft-delete clients); same list as deleted_node_ids
            "removed": removed,
        }

    @app.post("/weaves/{weave_id}/nodes/{node_id}/restore")
    async def restore_node(weave_id: str, node_id: str) -> dict[str, Any]:
        """Un-soft-delete (frontend undo): restores the node's deletion op plus
        any deleted-ancestor ops needed for reachability. 409 if the node is
        not deleted. Cursors do not move back."""
        try:
            restored = store.restore_node(weave_id, node_id)
        except NotFound as e:
            raise _404(e)
        except Conflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        await hub.push_new()
        return {"restored_node_ids": restored}

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

    @app.post("/weaves/{weave_id}/nodes/{node_id}/merge-with-parent")
    async def merge_with_parent(weave_id: str, node_id: str) -> dict[str, Any]:
        """Merge the node into its parent: a NEW node M = concat(parent, node)
        under the grandparent; the node's children migrate to M; the node (and,
        when it was the parent's only child, the parent too) is soft-deleted.
        Restorable via .../restore on deleted_node_ids[0] — see
        docs/events-api.md for the undo semantics. 409 on a root node."""
        try:
            merged, deleted, moved_cursors = store.merge_with_parent(
                weave_id, node_id
            )
        except NotFound as e:
            raise _404(e)
        except Conflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        await hub.push_new()
        return {
            "merged_node_id": merged.id,
            "merged_node": merged,
            # DELETE-shaped tail so undo machinery can be reused as-is
            "deleted_node_ids": deleted,
            "moved_cursors": moved_cursors,
        }

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

    @app.post("/weaves/{weave_id}/gen", status_code=201)
    async def gen(weave_id: str, req: GenRequest) -> list[Node]:
        if req.move_cursor and req.cursor is None:
            raise HTTPException(
                status_code=400, detail="move_cursor requires a cursor name"
            )
        if req.generator_id is None:
            raise HTTPException(status_code=400, detail="generator_id required")
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
        try:
            prompt = store.get_thread_content(weave_id, parent_id)
            generator = store.get_generator(req.generator_id)
        except NotFound as e:
            raise _404(e)
        resolved = store.resolve_generator(generator.id)
        if not resolved.usable:
            raise HTTPException(
                status_code=400,
                detail=f"generator {generator.name!r} does not resolve to a"
                " base_url + model (parentless skeleton?)",
            )
        endpoint = EndpointConfig(
            base_url=resolved.base_url,
            model=resolved.model,
            api_key=resolved.api_key,
            api_key_env=resolved.api_key_env,
            params={},  # all params live in the merged body below
        )
        # merge order (later wins): server defaults <- resolved generator
        # params <- per-request overrides (CLI/agents)
        params = {
            "model": resolved.model,
            **DEFAULT_PARAMS,
            **resolved.params,
            **req.params,
        }
        # presence events: who is generating where (live indicator + activity
        # feed + client-side placeholder nodes, one per expected completion)
        gen_info: dict[str, Any] = {
            "gen_id": uuid.uuid4().hex,
            "requester": req.cursor,
            "node_id": parent_id,
            "generator": generator.name,
            "generator_id": generator.id,
            "n": int(params.get("n") or 1),
        }
        store.log_activity(weave_id, "gen_started", gen_info)
        await hub.push_new()

        async def on_retry(attempt: int, max_retries: int, error: str) -> None:
            # one event per retry attempt: live "retrying k/max" indicators
            store.log_activity(
                weave_id,
                "gen_retrying",
                {**gen_info, "attempt": attempt, "max": max_retries, "error": error},
            )
            await hub.push_new()

        try:
            generated = await generate(endpoint, prompt, params, on_retry=on_retry)
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

    # ------------------------------------------------------------ static UI
    # Mounted LAST so every API route above takes precedence; html=True serves
    # index.html at "/" (the SPA does no path routing, so no fallback needed).
    if static_dir is not None:
        if static_dir.is_dir():
            from fastapi.staticfiles import StaticFiles

            app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
            logger.info("serving web UI from %s", static_dir)
        else:
            logger.warning(
                "static dir %s missing — API only (build with: cd web && npm run build)",
                static_dir,
            )

    return app
