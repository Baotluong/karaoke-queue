"""FastAPI app: guest pages, JSON API, and a WebSocket that pushes queue updates.

Run:  .venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .admin import AdminAuth
from .config import Settings
from .player import Player
from .search import SearchError, Searcher, extract_video_id
from .store import Store
from .tv import FakeScreen, LoungeScreen, Screen

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "kq_token"
ADMIN_COOKIE = "kq_admin"


class Hub:
    """Pushes the latest state to every connected phone."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.state_fn = lambda: {}

    def notify(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self.broadcast())
        except RuntimeError:
            pass  # no loop (e.g. during shutdown)

    async def broadcast(self) -> None:
        msg = json.dumps(self.state_fn())
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self.clients.discard(ws)


class JoinBody(BaseModel):
    name: str


class RequestBody(BaseModel):
    video_id: str


class PinBody(BaseModel):
    pin: str


class MoveBody(BaseModel):
    index: int


class BlockBody(BaseModel):
    blocked: bool


def create_app(settings: Settings | None = None, screen: Screen | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("pyytlounge").setLevel(logging.WARNING)
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    if screen is None:
        screen = (
            FakeScreen(settings.fake_song_seconds)
            if settings.fake_tv
            else LoungeScreen(settings.auth_file)
        )
    player = Player(store, screen)
    hub = Hub()
    auth = AdminAuth(settings.admin_pin)

    def public_state() -> dict:
        return {**store.snapshot(), "tv": screen.status()}

    hub.state_fn = public_state
    store.on_change(hub.notify)
    screen.on_status = hub.notify

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with aiohttp.ClientSession() as session:
            app.state.searcher = Searcher(settings.yt_api_key, session)
            await player.start()
            try:
                yield
            finally:
                await player.stop()

    app = FastAPI(title="Karaoke Queue", lifespan=lifespan)
    app.state.store, app.state.player, app.state.screen = store, player, screen

    def current_singer(token: str | None):
        singer = store.singer_by_token(token)
        if not singer:
            raise HTTPException(401, "Enter your name first")
        return singer

    def require_admin(kq_admin: str | None = Cookie(default=None)) -> None:
        if not auth.is_admin(kq_admin):
            raise HTTPException(401, "Admin login required")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin")
    async def admin_page():
        return FileResponse(STATIC / "admin.html")

    @app.get("/api/state")
    async def get_state(kq_token: str | None = Cookie(default=None)):
        singer = store.singer_by_token(kq_token)
        return {
            **public_state(),
            "me": {"id": singer.id, "name": singer.name} if singer else None,
            "can_search": app.state.searcher.can_search,
        }

    @app.post("/api/join")
    async def join(body: JoinBody, response: Response):
        try:
            singer = store.join(body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        response.set_cookie(COOKIE, singer.token, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return {"id": singer.id, "name": singer.name}

    @app.get("/api/search")
    async def search(q: str):
        try:
            results = await app.state.searcher.search(q)
        except SearchError as exc:
            raise HTTPException(400, str(exc))
        return {"results": [r.__dict__ for r in results]}

    @app.post("/api/requests")
    async def add_request(body: RequestBody, kq_token: str | None = Cookie(default=None)):
        singer = current_singer(kq_token)
        if singer.blocked:
            raise HTTPException(403, "An admin has stopped you from adding songs")
        video_id = extract_video_id(body.video_id)
        if not video_id:
            raise HTTPException(400, "Not a YouTube video")
        try:
            # Look the title up ourselves rather than trusting what the phone sent.
            video = await app.state.searcher.lookup(video_id)
        except SearchError as exc:
            raise HTTPException(400, str(exc))
        return store.add(singer, video).public()

    # --- admin ----------------------------------------------------------------

    @app.post("/api/admin/login")
    async def admin_login(body: PinBody, request: Request, response: Response):
        client = request.client.host if request.client else "?"
        if wait := auth.locked_for(client):
            raise HTTPException(429, f"Too many tries. Wait {wait:.0f}s.")
        if not auth.check_pin(client, body.pin):
            raise HTTPException(401, "Wrong PIN")
        response.set_cookie(ADMIN_COOKIE, auth.token, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return {"ok": True}

    @app.post("/api/admin/logout")
    async def admin_logout(response: Response):
        response.delete_cookie(ADMIN_COOKIE)
        return {"ok": True}

    admin = [Depends(require_admin)]

    @app.get("/api/admin/state", dependencies=admin)
    async def admin_state():
        return {**public_state(), "singers": store.singers()}

    @app.post("/api/admin/skip", dependencies=admin)
    async def admin_skip():
        await player.skip()
        return {"ok": True}

    @app.post("/api/admin/pause", dependencies=admin)
    async def admin_pause():
        if not await player.pause():
            raise HTTPException(409, "TV isn't connected")
        return {"ok": True}

    @app.post("/api/admin/resume", dependencies=admin)
    async def admin_resume():
        if not await player.resume():
            raise HTTPException(409, "TV isn't connected")
        return {"ok": True}

    @app.post("/api/admin/requests/{request_id}/move", dependencies=admin)
    async def admin_move(request_id: int, body: MoveBody):
        if not store.move(request_id, body.index):
            raise HTTPException(404, "That song isn't in the queue anymore")
        return {"ok": True}

    @app.post("/api/admin/requests/{request_id}/play-now", dependencies=admin)
    async def admin_play_now(request_id: int):
        if not await player.play_now(request_id):
            raise HTTPException(404, "That song isn't in the queue anymore")
        return {"ok": True}

    @app.delete("/api/admin/requests/{request_id}", dependencies=admin)
    async def admin_remove(request_id: int):
        if not store.remove(request_id):
            raise HTTPException(404, "That song isn't in the queue anymore")
        return {"ok": True}

    @app.post("/api/admin/singers/{singer_id}/block", dependencies=admin)
    async def admin_block(singer_id: int, body: BlockBody):
        store.set_blocked(singer_id, body.blocked)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        hub.clients.add(websocket)
        try:
            await websocket.send_text(json.dumps(public_state()))
            while True:
                await websocket.receive_text()  # keep-alive pings from the page
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app

