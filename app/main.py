"""FastAPI app: guest pages, JSON API, and a WebSocket that pushes queue updates.

Run:  .venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
import segno
from starlette.requests import HTTPConnection
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .admin import AdminAuth
from .config import Settings
from .limits import RateLimiter, client_ip, is_https
from .player import Player
from .search import SearchError, Searcher, extract_video_id
from .store import QueueRuleError, Store
from .tv import FakeScreen, LoungeScreen, Screen

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "kq_token"
ADMIN_COOKIE = "kq_admin"
PARTY_COOKIE = "kq_party"
MONTH = 60 * 60 * 24 * 30


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


class SettingsBody(BaseModel):
    max_waiting: int | None = None
    fair_turns: bool | None = None


class PartyBody(BaseModel):
    open: bool | None = None
    new_link: bool = False


def set_cookie(response: Response, conn: HTTPConnection, name: str, value: str) -> None:
    # Secure (HTTPS-only) when the phone came in over the tunnel's HTTPS.
    response.set_cookie(name, value, max_age=MONTH, httponly=True, samesite="lax", secure=is_https(conn))


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
    # Per guest IP. Generous for a party, tight enough to stop scripts.
    join_limit = RateLimiter(5, 10 * 60)
    search_limit = RateLimiter(20, 10 * 60)
    request_limit = RateLimiter(20, 10 * 60)

    def public_state() -> dict:
        return {**store.snapshot(), "tv": screen.status(), **store.settings()}

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

    @app.middleware("http")
    async def revalidate_pages(request: Request, call_next):
        # Phones would otherwise keep stale JS/CSS after an update. "no-cache" still
        # lets them reuse their copy when the server says it hasn't changed (304).
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") or path in ("/", "/admin"):
            response.headers["Cache-Control"] = "no-cache"
        return response
    app.state.store, app.state.player, app.state.screen = store, player, screen

    def current_singer(token: str | None):
        singer = store.singer_by_token(token)
        if not singer:
            raise HTTPException(401, "Enter your name first")
        return singer

    def require_admin(kq_admin: str | None = Cookie(default=None)) -> None:
        if not auth.is_admin(kq_admin):
            raise HTTPException(401, "Admin login required")

    def has_party_access(conn: HTTPConnection) -> bool:
        """Scanned the current party QR code (or is an admin)."""
        if auth.is_admin(conn.cookies.get(ADMIN_COOKIE)):
            return True
        code = conn.cookies.get(PARTY_COOKIE)
        return bool(code) and hmac.compare_digest(code, store.join_code)

    def require_party(request: Request) -> None:
        if not has_party_access(request):
            raise HTTPException(403, "Scan the party's QR code to join")

    def require_open(request: Request) -> None:
        if not store.party_open and not auth.is_admin(request.cookies.get(ADMIN_COOKIE)):
            raise HTTPException(403, "The party isn't taking new songs right now")

    def limit(limiter: RateLimiter, request: Request) -> None:
        if wait := limiter.hit(client_ip(request)):
            raise HTTPException(429, f"Slow down! Try again in {wait:.0f}s.")

    def join_url(request: Request) -> str:
        base = settings.public_url or str(request.base_url).rstrip("/")
        return f"{base}/join/{store.join_code}"

    party = [Depends(require_party)]

    @app.get("/")
    async def index(request: Request):
        return FileResponse(STATIC / ("index.html" if has_party_access(request) else "gate.html"))

    @app.get("/join/{code}")
    async def join_link(code: str, request: Request):
        if not hmac.compare_digest(code.upper().encode(), store.join_code.encode()):
            return RedirectResponse("/?expired=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        set_cookie(response, request, PARTY_COOKIE, store.join_code)
        return response

    @app.get("/admin")
    async def admin_page():
        return FileResponse(STATIC / "admin.html")

    @app.get("/api/state", dependencies=party)
    async def get_state(kq_token: str | None = Cookie(default=None)):
        singer = store.singer_by_token(kq_token)
        return {
            **public_state(),
            "me": {"id": singer.id, "name": singer.name} if singer else None,
            "can_search": app.state.searcher.can_search,
        }

    @app.post("/api/join", dependencies=party + [Depends(require_open)])
    async def join(body: JoinBody, request: Request, response: Response):
        limit(join_limit, request)
        try:
            singer = store.join(body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        set_cookie(response, request, COOKIE, singer.token)
        return {"id": singer.id, "name": singer.name}

    @app.get("/api/search", dependencies=party)
    async def search(q: str, request: Request):
        limit(search_limit, request)
        try:
            results = await app.state.searcher.search(q)
        except SearchError as exc:
            raise HTTPException(400, str(exc))
        return {"results": [r.__dict__ for r in results]}

    @app.post("/api/requests", dependencies=party + [Depends(require_open)])
    async def add_request(body: RequestBody, request: Request, kq_token: str | None = Cookie(default=None)):
        singer = current_singer(kq_token)
        limit(request_limit, request)
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
        try:
            added = store.add(singer, video)
        except QueueRuleError as exc:
            raise HTTPException(409, str(exc))
        place = next(i for i, r in enumerate(store.upcoming(), 1) if r.id == added.id)
        return {**added.public(), "place": place}

    # Anyone in the party can invite others: they already have the link anyway.
    # If it spreads too far, an admin makes a new one.
    @app.get("/api/party", dependencies=party)
    async def party_link(request: Request):
        return {"join_url": join_url(request)}

    @app.get("/api/qr.svg", dependencies=party)
    async def party_qr(request: Request):
        buf = io.BytesIO()
        # A standalone SVG file (with xmlns), not svg_inline(), so <img> can load it.
        segno.make(join_url(request), error="m").save(buf, kind="svg", scale=8, border=2, dark="#000", light="#fff")
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.delete("/api/requests/{request_id}", dependencies=party)
    async def remove_own_request(request_id: int, kq_token: str | None = Cookie(default=None)):
        singer = current_singer(kq_token)
        if not store.remove(request_id, singer_id=singer.id):
            raise HTTPException(404, "That song isn't waiting in the queue, or it isn't yours")
        return {"ok": True}

    # --- admin ----------------------------------------------------------------

    @app.post("/api/admin/login")
    async def admin_login(body: PinBody, request: Request, response: Response):
        client = client_ip(request)
        if wait := auth.locked_for(client):
            raise HTTPException(429, f"Too many tries. Wait {wait:.0f}s.")
        if not auth.check_pin(client, body.pin):
            raise HTTPException(401, "Wrong PIN")
        set_cookie(response, request, ADMIN_COOKIE, auth.token)
        return {"ok": True}

    @app.post("/api/admin/logout")
    async def admin_logout(response: Response):
        response.delete_cookie(ADMIN_COOKIE)
        return {"ok": True}

    admin = [Depends(require_admin)]

    @app.get("/api/admin/state", dependencies=admin)
    async def admin_state():
        return {**public_state(), "singers": store.singers()}

    @app.post("/api/admin/settings", dependencies=admin)
    async def admin_settings(body: SettingsBody):
        try:
            if body.max_waiting is not None:
                store.set_max_waiting(body.max_waiting)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if body.fair_turns is not None:
            store.set_fair_turns(body.fair_turns)
        return {"ok": True}

    @app.get("/api/admin/party", dependencies=admin)
    async def admin_party(request: Request):
        return {"join_url": join_url(request), "open": store.party_open, "public": bool(settings.public_url)}

    @app.post("/api/admin/party", dependencies=admin)
    async def admin_party_update(body: PartyBody, request: Request):
        if body.open is not None:
            store.set_party_open(body.open)
        if body.new_link:
            store.rotate_join_code()
            # Phones that joined with the old link lose live updates right away.
            for client in list(hub.clients):
                if not has_party_access(client):
                    hub.clients.discard(client)
                    await client.close(code=4403)
        return {"join_url": join_url(request), "open": store.party_open, "public": bool(settings.public_url)}


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
        if not has_party_access(websocket):
            await websocket.close(code=4403)
            return
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

