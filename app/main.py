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
from fastapi import Cookie, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .player import Player
from .search import SearchError, Searcher, extract_video_id
from .store import Store
from .tv import FakeScreen, LoungeScreen, Screen

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "kq_token"


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

    def public_state() -> dict:
        return {**store.snapshot(), "tv": screen.status()}

    hub.state_fn = public_state
    store.on_change(hub.notify)

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

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

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
        video_id = extract_video_id(body.video_id)
        if not video_id:
            raise HTTPException(400, "Not a YouTube video")
        try:
            # Look the title up ourselves rather than trusting what the phone sent.
            video = await app.state.searcher.lookup(video_id)
        except SearchError as exc:
            raise HTTPException(400, str(exc))
        return store.add(singer, video).public()

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

