import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import admin as admin_mod
from app.config import Settings
from app.limits import RateLimiter, client_ip, is_https
from app.main import create_app
from app.search import Searcher
from app.store import Store, Video
from app.tv import FakeScreen
from conftest import scan_qr

PIN = "party-pin-123"


class OfflineTV(FakeScreen):
    @property
    def ready(self) -> bool:
        return False


@pytest.fixture
def app(monkeypatch):
    async def fake_lookup(self, video_id):
        return Video(video_id=video_id, title=video_id)

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    return create_app(
        Settings(db_path=":memory:", admin_pin=PIN, public_url="https://party.example.com"),
        screen=OfflineTV(),
    )


@pytest.fixture
def c(app):
    with TestClient(app) as client:
        yield client


def admin_login(c):
    assert c.post("/api/admin/login", json={"pin": PIN}).status_code == 200


# --- party link ---------------------------------------------------------------

def test_without_the_party_link_guests_see_the_gate_and_cant_use_the_api(c):
    assert "Scan the QR code" in c.get("/").text
    assert c.get("/api/state").status_code == 403
    assert c.post("/api/join", json={"name": "Stranger"}).status_code == 403
    assert c.get("/api/search", params={"q": "x"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws") as ws:
            ws.receive_text()
    assert c.get("/admin").status_code == 200  # admins can always reach their login


def test_scanning_the_link_lets_you_in_and_wrong_codes_dont(c):
    r = c.get("/join/WRONGCODE", follow_redirects=False)
    assert r.headers["location"] == "/?expired=1"
    assert c.get("/api/state").status_code == 403

    code = c.app.state.store.join_code
    r = c.get(f"/join/{code.lower()}", follow_redirects=False)  # case doesn't matter
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Who's singing" in c.get("/").text
    assert c.get("/api/state").status_code == 200


def test_new_link_locks_out_old_one(c):
    scan_qr(c)
    c.post("/api/join", json={"name": "Ana"})
    old = c.app.state.store.join_code

    other = TestClient(c.app)  # an admin on another phone
    admin_login(other)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        new = other.post("/api/admin/party", json={"new_link": True}).json()["join_url"]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()  # kicked
        assert exc.value.code == 4403

    assert new == f"https://party.example.com/join/{c.app.state.store.join_code}"
    assert c.app.state.store.join_code != old
    assert c.get("/api/state").status_code == 403
    assert c.get(f"/join/{old}", follow_redirects=False).headers["location"] == "/?expired=1"
    scan_qr(c)
    assert c.get("/api/state").json()["me"]["name"] == "Ana"  # same name after rescanning


def test_closed_party_blocks_joining_and_adding_but_not_admins(c):
    scan_qr(c)
    c.post("/api/join", json={"name": "Ana"})
    admin = TestClient(c.app)
    admin_login(admin)
    admin.post("/api/admin/party", json={"open": False})

    assert c.get("/api/state").json()["party_open"] is False
    assert c.post("/api/requests", json={"video_id": "aaaaaaaaaaa"}).status_code == 403
    newcomer = scan_qr(TestClient(c.app))
    assert newcomer.post("/api/join", json={"name": "Ben"}).status_code == 403

    admin.post("/api/join", json={"name": "Host"})
    assert admin.post("/api/requests", json={"video_id": "aaaaaaaaaaa"}).status_code == 200

    admin.post("/api/admin/party", json={"open": True})
    assert c.post("/api/requests", json={"video_id": "bbbbbbbbbbb"}).status_code == 200


def test_party_settings_are_admin_only(c):
    scan_qr(c)
    assert c.get("/api/admin/party").status_code == 401
    assert c.post("/api/admin/party", json={"new_link": True}).status_code == 401
    admin_login(c)
    info = c.get("/api/admin/party").json()
    assert info == {
        "join_url": f"https://party.example.com/join/{c.app.state.store.join_code}",
        "open": True,
        "public": True,
    }


def test_everyone_in_the_party_can_get_the_invite_qr(c):
    assert c.get("/api/party").status_code == 403  # strangers can't
    assert c.get("/api/qr.svg").status_code == 403
    scan_qr(c)  # a guest, not an admin
    link = c.get("/api/party").json()["join_url"]
    assert link == f"https://party.example.com/join/{c.app.state.store.join_code}"
    r = c.get("/api/qr.svg")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert 'xmlns="http://www.w3.org/2000/svg"' in r.text  # needed for <img src=...>


def test_without_public_url_the_link_uses_the_address_you_browsed_to(monkeypatch):
    app = create_app(Settings(db_path=":memory:", admin_pin=PIN), screen=OfflineTV())
    with TestClient(app, base_url="http://192.168.4.69:8000") as c:
        admin_login(c)
        info = c.get("/api/admin/party").json()
        assert info["join_url"].startswith("http://192.168.4.69:8000/join/")
        assert info["public"] is False


# --- rate limits, client IP, cookies ------------------------------------------

def test_rate_limiter_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.limits.time.time", lambda: now[0])
    rl = RateLimiter(2, 60)
    assert rl.hit("a") == 0 and rl.hit("a") == 0
    assert rl.hit("a") == pytest.approx(60)
    assert rl.hit("b") == 0  # other clients unaffected
    now[0] += 61
    assert rl.hit("a") == 0


def test_join_is_rate_limited_per_client(c):
    scan_qr(c)
    codes = [c.post("/api/join", json={"name": f"N{i}"}).status_code for i in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


def test_faked_cloudflare_header_cant_dodge_limits(c):
    # TestClient isn't loopback (not the tunnel), so a CF-Connecting-IP header from
    # it is ignored: changing it doesn't reset the limit. The tunnel case is
    # covered by test_client_ip_trusts_cloudflare_header_only_from_loopback.
    scan_qr(c)
    hdr = {"CF-Connecting-IP": "1.2.3.4"}
    for i in range(5):
        c.post("/api/join", json={"name": f"N{i}"}, headers=hdr)
    assert c.post("/api/join", json={"name": "x"}, headers={"CF-Connecting-IP": "5.6.7.8"}).status_code == 429


class FakeConn:
    def __init__(self, peer, headers, scheme="http"):
        self.client = type("C", (), {"host": peer})()
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = type("U", (), {"scheme": scheme})()


def test_client_ip_trusts_cloudflare_header_only_from_loopback():
    assert client_ip(FakeConn("127.0.0.1", {"CF-Connecting-IP": "1.2.3.4"})) == "1.2.3.4"
    assert client_ip(FakeConn("::1", {"CF-Connecting-IP": "1.2.3.4"})) == "1.2.3.4"
    assert client_ip(FakeConn("192.168.4.66", {"CF-Connecting-IP": "1.2.3.4"})) == "192.168.4.66"
    assert client_ip(FakeConn("127.0.0.1", {})) == "127.0.0.1"


def test_https_detection_for_secure_cookies():
    assert is_https(FakeConn("127.0.0.1", {"X-Forwarded-Proto": "https"}))
    assert not is_https(FakeConn("192.168.4.66", {"X-Forwarded-Proto": "https"}))  # spoofed
    assert not is_https(FakeConn("192.168.4.66", {}))
    assert is_https(FakeConn("1.2.3.4", {}, scheme="https"))


def test_cookies_are_secure_over_https(app):
    with TestClient(app, base_url="https://party.example.com") as c:
        code = app.state.store.join_code
        r = c.get(f"/join/{code}", follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower()
    with TestClient(app) as c:
        r = c.get(f"/join/{code}", follow_redirects=False)
        assert "secure" not in r.headers["set-cookie"].lower()


# --- admin lockouts and search cache ----------------------------------------

def test_admin_login_global_lockout_stops_guessing_from_many_ips(c, monkeypatch):
    monkeypatch.setattr(admin_mod, "GLOBAL_MAX_FAILURES", 6)
    auth = admin_mod.AdminAuth(PIN)
    for i in range(6):
        assert not auth.check_pin(f"10.0.0.{i}", "guess")  # a different IP every time
    assert auth.locked_for("10.0.0.99") > 0
    assert not auth.check_pin("10.0.0.99", PIN)  # even the right PIN waits


async def test_search_results_are_cached():
    calls = []

    class FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def json(self):
            return {"items": [{"id": {"videoId": "aaaaaaaaaaa"}, "snippet": {"title": "Song &amp; Co"}}]}

    class FakeSession:
        def get(self, url, params):
            calls.append(params["q"])
            return FakeResp()

    s = Searcher("key", FakeSession())
    first = await s.search("Bohemian Rhapsody")
    second = await s.search("  bohemian   RHAPSODY ")
    assert first == second and first[0].title == "Song & Co"
    assert calls == ["Bohemian Rhapsody karaoke"]  # YouTube asked once


async def test_search_modes():
    calls = []

    class FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def json(self):
            return {"items": []}

    class FakeSession:
        def get(self, url, params):
            calls.append(params["q"])
            return FakeResp()

    s = Searcher("key", FakeSession())
    await s.search("Dancing Queen")  # karaoke by default
    await s.search("Dancing Queen", mode="lyrics")
    # Already says what they want, so it's left alone (and is the same search as above).
    await s.search("Dancing Queen lyrics", mode="karaoke")
    await s.search("dancing queen", mode="lyrics")  # cached
    assert calls == ["Dancing Queen karaoke", "Dancing Queen lyrics"]


def test_search_api_mode(c):
    scan_qr(c)
    assert c.get("/api/search", params={"q": "x", "mode": "acapella"}).status_code == 422
