import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.search import Searcher
from app.store import QueueRuleError, Store, Video
from app.tv import FakeScreen


def vid(tag: str) -> Video:
    return Video(video_id=tag.ljust(11, "x"), title=tag)


def order(s: Store) -> list[str]:
    return [r.title for r in s.upcoming()]


@pytest.fixture
def s():
    store = Store()
    store.set_max_waiting(0)  # rules tested separately
    return store


def test_round_robin_interleaves_singers(s):
    ana, ben, cat = s.join("Ana"), s.join("Ben"), s.join("Cat")
    for t in ("A1", "A2", "A3"):
        s.add(ana, vid(t))
    s.add(ben, vid("B1"))
    s.add(ben, vid("B2"))
    s.add(cat, vid("C1"))
    assert order(s) == ["A1", "B1", "C1", "A2", "B2", "A3"]


def test_newcomer_goes_ahead_of_second_turns(s):
    ana, ben = s.join("Ana"), s.join("Ben")
    s.add(ana, vid("A1"))
    s.add(ana, vid("A2"))
    s.add(ben, vid("B1"))
    late = s.join("Late")
    s.add(late, vid("L1"))
    assert order(s) == ["A1", "B1", "L1", "A2"]


def test_no_back_to_back_after_singing(s):
    ana, ben = s.join("Ana"), s.join("Ben")
    a1 = s.add(ana, vid("A1"))
    s.mark_playing(a1.id)  # Ana is singing now
    s.add(ana, vid("A2"))  # queues her next song...
    s.add(ben, vid("B1"))  # ...but Ben, who hasn't sung, goes first
    assert order(s) == ["B1", "A2"]


def test_alone_in_the_room_still_plays_next(s):
    ana = s.join("Ana")
    a1 = s.add(ana, vid("A1"))
    s.mark_playing(a1.id)
    s.add(ana, vid("A2"))
    assert order(s) == ["A2"]


def test_admin_reorder_is_respected(s):
    ana, ben, cat = s.join("Ana"), s.join("Ben"), s.join("Cat")
    s.add(ana, vid("A1"))
    s.add(ben, vid("B1"))
    s.add(ana, vid("A2"))
    b1 = next(r for r in s.upcoming() if r.title == "B1")
    s.move(b1.id, 2)  # admin pushes Ben behind Ana's second song
    assert order(s) == ["A1", "A2", "B1"]
    s.add(cat, vid("C1"))  # Cat's first turn: after the last first-turn song (B1)
    assert order(s) == ["A1", "A2", "B1", "C1"]


def test_waiting_limit(s):
    ana, ben = s.join("Ana"), s.join("Ben")
    s.set_max_waiting(2)
    s.add(ana, vid("A1"))
    s.add(ana, vid("A2"))
    with pytest.raises(QueueRuleError, match="2 songs waiting"):
        s.add(ana, vid("A3"))
    s.add(ben, vid("B1"))  # others unaffected

    # Once one plays, there's room again.
    s.mark_playing(s.next_up().id)
    s.add(ana, vid("A3"))

    s.set_max_waiting(0)  # no limit
    for i in range(5):
        s.add(ana, vid(f"X{i}"))


def test_duplicate_song_rejected_while_waiting_or_playing(s):
    ana, ben = s.join("Ana"), s.join("Ben")
    a1 = s.add(ana, vid("SAME"))
    with pytest.raises(QueueRuleError, match="already in the queue"):
        s.add(ben, vid("SAME"))
    s.mark_playing(a1.id)
    with pytest.raises(QueueRuleError):
        s.add(ben, vid("SAME"))
    s.mark_finished(a1.id)
    s.add(ben, vid("SAME"))  # fine once it has finished


def test_limit_setting_validation_and_default():
    s = Store()
    assert s.max_waiting == 3
    with pytest.raises(ValueError):
        s.set_max_waiting(-1)
    s.set_max_waiting(5)
    assert s.max_waiting == 5


class OfflineTV(FakeScreen):
    """A TV that's switched off, so nothing leaves the queue during the test."""

    @property
    def ready(self) -> bool:
        return False


def test_api_reports_place_and_rule_errors(monkeypatch):
    async def fake_lookup(self, video_id):
        return Video(video_id=video_id, title=video_id)

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    app = create_app(Settings(db_path=":memory:", admin_pin="9999"), screen=OfflineTV())
    with TestClient(app) as c:
        c.post("/api/join", json={"name": "Ana"})
        assert c.get("/api/state").json()["max_waiting"] == 3

        assert c.post("/api/requests", json={"video_id": "aaaaaaaaaaa"}).json()["place"] == 1
        assert c.post("/api/requests", json={"video_id": "bbbbbbbbbbb"}).json()["place"] == 2
        r = c.post("/api/requests", json={"video_id": "aaaaaaaaaaa"})
        assert r.status_code == 409 and "already" in r.json()["detail"]

        c.post("/api/admin/login", json={"pin": "9999"})
        assert c.post("/api/admin/settings", json={"max_waiting": 99}).status_code == 400
        assert c.post("/api/admin/settings", json={"max_waiting": 2}).status_code == 200
        assert c.get("/api/state").json()["max_waiting"] == 2
        r = c.post("/api/requests", json={"video_id": "ccccccccccc"})
        assert r.status_code == 409 and "2 songs waiting" in r.json()["detail"]


# --- fair turns toggle and removing your own song ----------------------------

def test_fair_turns_off_appends_to_bottom_but_keeps_limit(s):
    assert s.fair_turns  # on by default
    ana, ben = s.join("Ana"), s.join("Ben")
    s.set_fair_turns(False)
    s.set_max_waiting(3)
    for t in ("A1", "A2", "A3"):
        s.add(ana, vid(t))
    s.add(ben, vid("B1"))
    assert order(s) == ["A1", "A2", "A3", "B1"]  # plain first-come order
    with pytest.raises(QueueRuleError):
        s.add(ana, vid("A4"))  # cap still applies

    s.set_fair_turns(True)  # existing order is kept; new songs use turns again
    cat = s.join("Cat")
    s.add(cat, vid("C1"))  # Cat's 1st turn goes after the last 1st-turn song (B1)...
    s.add(ben, vid("B2"))  # ...and Ben's 2nd can't jump ahead of his own 1st
    assert order(s) == ["A1", "A2", "A3", "B1", "C1", "B2"]
    s.remove(next(r.id for r in s.upcoming() if r.title == "A3"))
    s.add(ana, vid("A9"))  # Ana's 3rd turn: after the last song in turn 3 or earlier
    assert order(s) == ["A1", "A2", "B1", "C1", "B2", "A9"]


def test_sung_songs_do_not_count_against_you(s):
    ana, ben = s.join("Ana"), s.join("Ben")
    for i in range(5):  # Ana has sung five songs tonight
        r = s.add(ana, vid(f"OLD{i}"))
        s.mark_playing(r.id)
        s.mark_finished(r.id)
    s.add(ben, vid("B1"))
    s.add(ana, vid("A1"))  # Ana's turn 1, same as Ben's: goes after him, not behind 5 rounds
    s.add(ben, vid("B2"))
    assert order(s) == ["B1", "A1", "B2"]


def test_singer_can_remove_only_their_own_waiting_song(s):
    ana, ben = s.join("Ana"), s.join("Ben")
    a1, a2 = s.add(ana, vid("A1")), s.add(ana, vid("A2"))
    b1 = s.add(ben, vid("B1"))
    assert not s.remove(b1.id, singer_id=ana.id)  # not hers
    assert s.remove(a2.id, singer_id=ana.id)
    s.mark_playing(a1.id)
    assert not s.remove(a1.id, singer_id=ana.id)  # already playing: admins skip instead
    assert order(s) == ["B1"]


def test_api_guest_remove_and_fair_turns_setting(monkeypatch):
    async def fake_lookup(self, video_id):
        return Video(video_id=video_id, title=video_id)

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    app = create_app(Settings(db_path=":memory:", admin_pin="9999"), screen=OfflineTV())
    with TestClient(app) as c:
        store = app.state.store
        ben = store.join("Ben")
        b1 = store.add(ben, vid("B1"))

        c.post("/api/join", json={"name": "Ana"})
        mine = c.post("/api/requests", json={"video_id": "aaaaaaaaaaa"}).json()
        assert c.delete(f"/api/requests/{b1.id}").status_code == 404  # Ben's song
        assert c.delete(f"/api/requests/{mine['id']}").status_code == 200
        assert [r.title for r in store.upcoming()] == ["B1"]
        c.cookies.clear()
        assert c.delete(f"/api/requests/{b1.id}").status_code == 401  # not joined

        assert c.get("/api/state").json()["fair_turns"] is True
        assert c.post("/api/admin/settings", json={"fair_turns": False}).status_code == 401
        c.post("/api/admin/login", json={"pin": "9999"})
        assert c.post("/api/admin/settings", json={"fair_turns": False}).status_code == 200
        state = c.get("/api/state").json()
        assert state["fair_turns"] is False and state["max_waiting"] == 3  # limit untouched
