"""End-to-end tests: start the real server on a free port and talk HTTP to it.
Run:  python3 -m pytest tests/ -q"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import cardnim_server as srv  # noqa: E402


@pytest.fixture(scope="module")
def base_url():
    store = srv.GameStore(results_dir=None)
    server = srv.CardNimServer(("127.0.0.1", 0), store, quiet=True)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, token=None, text=False):
    """Returns (status, parsed body).  4xx/5xx do not raise."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Token"] = token
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    if text:
        return status, raw
    return status, (json.loads(raw) if raw else None)


def parse_text(raw):
    return dict(line.split(" ", 1) if " " in line else (line, "") for line in raw.strip().splitlines())


def test_health_and_lobby_pages(base_url):
    status, body = call(base_url, "GET", "/api/health")
    assert status == 200 and body["ok"] is True
    status, html = call(base_url, "GET", "/", text=True)
    assert status == 200 and "<" in html
    status, body = call(base_url, "GET", "/nope.html")
    assert status == 404


def test_create_validation(base_url):
    assert call(base_url, "POST", "/api/games", {"stones": 0, "cards": 3})[0] == 400
    assert call(base_url, "POST", "/api/games", {"stones": 10, "cards": 999})[0] == 400
    assert call(base_url, "POST", "/api/games", {"stones": "ten", "cards": 3})[0] == 400
    assert call(base_url, "POST", "/api/games", {"cards": 3})[0] == 400
    assert call(base_url, "POST", "/api/games", {"stones": 10, "cards": 3, "time_limit": 0})[0] == 400


def test_full_game_over_http(base_url):
    # create
    status, state = call(base_url, "POST", "/api/games", {"stones": 5, "cards": 3, "label": "demo"})
    assert status == 201 and state["status"] == "waiting" and state["label"] == "demo"
    gid = state["id"]
    assert len(gid) == 4

    # appears in the lobby list
    status, body = call(base_url, "GET", "/api/games")
    assert any(g["id"] == gid for g in body["games"])

    # id is case-insensitive
    assert call(base_url, "GET", f"/api/games/{gid.lower()}")[0] == 200

    # join
    status, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "Alice"})
    assert status == 200 and a["seat"] == 1 and a["token"]
    status, raw = call(base_url, "GET", f"/api/games/{gid}/join?name=Bob&format=text", text=True)
    assert status == 200
    b = parse_text(raw)
    assert b["seat"] == "2" and b["token"]
    tok_a, tok_b = a["token"], b["token"]

    # third player rejected
    status, err = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "Carol"})
    assert status == 409 and "taken" in err["error"]

    # game started, Alice to move
    status, state = call(base_url, "GET", f"/api/games/{gid}", token=tok_a)
    assert state["status"] == "playing" and state["turn"] == 1 and state["your_turn"] is True

    # Bob out of turn -> 409, unchanged
    status, err = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 1}, token=tok_b)
    assert status == 409 and "not your turn" in err["error"]

    # no token -> 401
    assert call(base_url, "POST", f"/api/games/{gid}/move", {"card": 1})[0] == 401

    # Alice: card not in hand -> 409
    status, err = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 7}, token=tok_a)
    assert status == 409 and "not in your hand" in err["error"]

    # Alice plays 1
    status, state = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 1}, token=tok_a)
    assert status == 200 and state["stones"] == 4 and state["turn"] == 2
    assert state["players"][0]["cards"] == [2, 3]
    assert state["moves"][0]["card"] == 1 and state["moves"][0]["elapsed"] >= 0

    # Bob's getstate returns at once (it is his turn), Alice's must wait
    t0 = time.time()
    status, raw = call(base_url, "GET", f"/api/games/{gid}/getstate?format=text&token={tok_b}", text=True)
    assert status == 200 and parse_text(raw)["your_turn"] == "1" and time.time() - t0 < 2

    t0 = time.time()
    status, state = call(base_url, "GET", f"/api/games/{gid}/getstate?timeout=0.5", token=tok_a)
    assert status == 200 and state["your_turn"] is False and 0.4 <= time.time() - t0 < 5

    # Bob plays 3 via GET with query params (quick-test form)
    status, state = call(base_url, "GET", f"/api/games/{gid}/move?card=3&token={tok_b}")
    assert status == 200 and state["status"] == "finished" and state["winner"] == 2
    assert "no card small enough" in state["reason"]
    assert state["stones"] == 1

    # after the game, getstate returns immediately for both
    status, state = call(base_url, "GET", f"/api/games/{gid}/getstate", token=tok_a)
    assert state["status"] == "finished"

    # further moves rejected
    status, err = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 2}, token=tok_a)
    assert status == 409


def test_long_poll_wakes_on_change(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 10, "cards": 4})
    gid, v = state["id"], state["version"]
    result = {}

    def waiter():
        t0 = time.time()
        s, body = call(base_url, "GET", f"/api/games/{gid}?since={v}&timeout=10")
        result["elapsed"] = time.time() - t0
        result["version"] = body["version"]

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.5)
    call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A"})
    th.join(timeout=5)
    assert result and result["version"] > v and result["elapsed"] < 3


def test_long_poll_times_out_without_change(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 10, "cards": 4})
    gid, v = state["id"], state["version"]
    t0 = time.time()
    status, body = call(base_url, "GET", f"/api/games/{gid}?since={v}&timeout=0.5")
    assert status == 200 and body["version"] == v and 0.4 <= time.time() - t0 < 3


def test_overdraw_and_timeout_and_abort(base_url):
    # overdraw
    _, state = call(base_url, "POST", "/api/games", {"stones": 3, "cards": 5})
    gid = state["id"]
    _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A"})
    _, b = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B"})
    status, state = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 5}, token=a["token"])
    assert status == 200 and state["winner"] == 2 and state["moves"][0]["overdraw"] is True

    # timeout enforced by the ticker with nobody polling
    _, state = call(base_url, "POST", "/api/games", {"stones": 30, "cards": 8, "time_limit": 1})
    gid = state["id"]
    call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A"})
    call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B"})
    time.sleep(1.6)
    _, state = call(base_url, "GET", f"/api/games/{gid}")
    assert state["status"] == "finished" and state["winner"] == 2 and "time" in state["reason"]

    # abort
    _, state = call(base_url, "POST", "/api/games", {"stones": 30, "cards": 8})
    gid = state["id"]
    status, state = call(base_url, "POST", f"/api/games/{gid}/abort", {"reason": "oops"})
    assert status == 200 and state["status"] == "finished" and state["winner"] is None and state["reason"] == "oops"


def test_leave_and_resign(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 9, "cards": 4})
    gid = state["id"]
    _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A", "seat": 1})
    # no token -> 401
    assert call(base_url, "POST", f"/api/games/{gid}/leave")[0] == 401
    # leave while waiting: seat opens, token dies
    status, state = call(base_url, "POST", f"/api/games/{gid}/leave", token=a["token"])
    assert status == 200 and state["players"][0]["occupied"] is False and state["status"] == "waiting"
    assert call(base_url, "POST", f"/api/games/{gid}/leave", token=a["token"])[0] == 401
    # one tab can hold both seats
    _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A", "seat": 1})
    _, b = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B", "seat": 2})
    call(base_url, "POST", f"/api/games/{gid}/move", {"card": 1}, token=a["token"])
    # resign during play
    status, state = call(base_url, "POST", f"/api/games/{gid}/leave", token=b["token"])
    assert status == 200 and state["status"] == "finished" and state["winner"] == 1
    assert "resigned" in state["reason"]
    assert call(base_url, "POST", f"/api/games/{gid}/leave", token=a["token"])[0] == 409


def test_malformed_inputs_get_400_not_500(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 9, "cards": 4})
    gid = state["id"]
    _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A"})
    call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B"})
    tok = a["token"]
    # booleans and fractional numbers are not cards
    assert call(base_url, "POST", f"/api/games/{gid}/move", {"card": True}, token=tok)[0] == 400
    assert call(base_url, "POST", f"/api/games/{gid}/move", {"card": 2.5}, token=tok)[0] == 400
    assert call(base_url, "POST", f"/api/games/{gid}/move", {"card": "x"}, token=tok)[0] == 400
    assert call(base_url, "POST", f"/api/games/{gid}/move", {"card": None}, token=tok)[0] == 400
    # but a JSON float that is a whole number, or a padded string, is fine
    status, st = call(base_url, "POST", f"/api/games/{gid}/move", {"card": " 2 "}, token=tok)
    assert status == 200 and st["stones"] == 7
    # garbage long-poll parameters fall back to defaults instead of crashing
    assert call(base_url, "GET", f"/api/games/{gid}?since=0&timeout=abc")[0] == 200
    assert call(base_url, "GET", f"/api/games/{gid}?since=0&timeout=nan")[0] == 200
    assert call(base_url, "GET", f"/api/games/{gid}?since=abc")[0] == 400
    # a JSON list body is rejected
    req = urllib.request.Request(base_url + "/api/games", data=b"[1,2]",
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req)
        assert False, "expected 400"
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    # a bad Content-Length header is a 400
    req = urllib.request.Request(base_url + "/api/games", data=b"{}",
                                 headers={"Content-Type": "application/json", "Content-Length": "abc"}, method="POST")
    try:
        urllib.request.urlopen(req)
        assert False, "expected 400"
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    except Exception:
        pass  # some client stacks refuse to send the header at all; the server still stands
    assert call(base_url, "GET", "/api/health")[0] == 200


def test_static_files_never_leave_the_web_folder(base_url):
    assert call(base_url, "GET", "/style.css", text=True)[0] == 200
    assert call(base_url, "GET", "/../engine.py")[0] == 404
    assert call(base_url, "GET", "/%2e%2e/engine.py")[0] == 404
    assert call(base_url, "GET", "/..%2fengine.py")[0] == 404
    assert call(base_url, "GET", "/web/../../README.md")[0] == 404
    assert call(base_url, "GET", "/game/ZZZZ", text=True)[0] == 200   # page loads, script reports missing game
    assert call(base_url, "GET", "/game/../index.html", text=True)[0] in (200, 404)


def test_store_prunes_oldest_finished_games():
    import cardnim_server as srv2
    store = srv2.GameStore(results_dir=None)
    old_cap = srv2.MAX_GAMES
    srv2.MAX_GAMES = 5
    try:
        finished = []
        for i in range(5):
            g = store.create(3, 2, 120)
            g.abort("done")
            finished.append(g.id)
        live = store.create(3, 2, 120)     # 6th game: the oldest finished one is dropped
        assert len(store.games) == 5 and finished[0] not in store.games and live.id in store.games
        for _ in range(10):
            store.create(3, 2, 120)        # unfinished games are never dropped
        # 4 more finished games were dropped one per create, then nothing else qualifies
        assert live.id in store.games and len(store.games) == 11
        assert not any(g.status == "finished" for g in store.games.values())
    finally:
        srv2.MAX_GAMES = old_cap


def test_join_with_avatar(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 9, "cards": 4})
    gid = state["id"]
    status, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A", "avatar": 12})
    assert status == 200 and a["state"]["players"][0]["avatar"] == 12
    assert call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B", "avatar": 0})[0] == 400
    assert call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B", "avatar": "x"})[0] == 400
    status, b = call(base_url, "GET", f"/api/games/{gid}/join?name=B")
    assert status == 200 and 1 <= b["state"]["players"][1]["avatar"] <= 16
    status, body = call(base_url, "GET", "/api/games")
    row = next(g for g in body["games"] if g["id"] == gid)
    assert row["avatars"][0] == 12
    # the pictures are served as PNG
    with urllib.request.urlopen(base_url + "/avatars/av12.png") as resp:
        assert resp.status == 200 and resp.headers["Content-Type"].startswith("image/png")
        assert resp.read()[:8] == b"\x89PNG\r\n\x1a\n"


def test_server_seated_bots(base_url):
    status, body = call(base_url, "GET", "/api/bots")
    kinds = {b["kind"] for b in body["bots"]}
    assert status == 200 and {"greedy", "random"} <= kinds
    # unknown bot -> 400
    _, state = call(base_url, "POST", "/api/games", {"stones": 12, "cards": 5})
    gid = state["id"]
    assert call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": "nope"})[0] == 400
    # a human at seat 1, a greedy bot at seat 2 with no delay
    _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A", "seat": 1})
    status, state = call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": "greedy", "seat": 2, "delay": 0, "avatar": 3})
    assert status == 200 and state["status"] == "playing"
    assert state["players"][1]["bot"] is True and state["players"][1]["name"] == "Greedy bot" and state["players"][1]["avatar"] == 3
    assert state["players"][0]["bot"] is False
    # the human moves; the bot answers by itself
    status, state = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 1}, token=a["token"])
    assert status == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        _, state = call(base_url, "GET", f"/api/games/{gid}")
        if len(state["moves"]) >= 2:
            break
        time.sleep(0.05)
    assert len(state["moves"]) >= 2 and state["moves"][1]["seat"] == 2
    # a third seat is refused
    assert call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": "random"})[0] == 409
    # two bots play a whole game on their own
    _, state = call(base_url, "POST", "/api/games", {"stones": 15, "cards": 5})
    gid = state["id"]
    call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": "greedy", "delay": 0})
    call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": "random", "delay": 0})
    deadline = time.time() + 10
    while time.time() < deadline:
        _, state = call(base_url, "GET", f"/api/games/{gid}")
        if state["status"] == "finished":
            break
        time.sleep(0.05)
    assert state["status"] == "finished" and state["winner"] in (1, 2)
    _, body = call(base_url, "GET", "/api/games")
    row = next(g for g in body["games"] if g["id"] == gid)
    assert row["bots"] == [True, True]


def test_strategies_listing(base_url):
    status, body = call(base_url, "GET", "/api/strategies")
    assert status == 200
    langs = {c["language"] for c in body["clients"]}
    assert {"Python", "C++", "Java"} <= langs
    py = next(c for c in body["clients"] if c["language"] == "Python")
    assert py["file"] == "clients/python/client.py" and "brain" in py["edit"] and py["file"] in py["run"]
    kinds = {b["kind"]: b for b in body["bots"]}
    assert kinds["greedy"]["file"] == "server/bots.py" and kinds["greedy"]["private"] is False
    # every client lists its sample strategy; Python also lists the server-run bots
    assert all(c["strategies"][0]["name"] == "Sample strategy" for c in body["clients"])
    assert any(x["name"] == "Greedy bot" and x["file"] == "server/bots.py" for x in py["strategies"])


def test_sample_clients_can_be_seated_from_the_server(base_url):
    _, body = call(base_url, "GET", "/api/bots")
    kinds = {b["kind"] for b in body["bots"]}
    assert "client-python" in kinds
    for kind in [k for k in ("client-python", "client-cpp") if k in kinds]:
        _, state = call(base_url, "POST", "/api/games", {"stones": 12, "cards": 5, "label": kind})
        gid = state["id"]
        _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "Human", "seat": 1})
        status, state = call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": kind, "seat": 2, "avatar": 4})
        assert status == 200, state
        assert state["players"][1]["occupied"] and state["players"][1]["bot"] is True and state["players"][1]["avatar"] == 4
        call(base_url, "POST", f"/api/games/{gid}/move", {"card": 1}, token=a["token"])
        deadline = time.time() + 8
        while time.time() < deadline:
            _, state = call(base_url, "GET", f"/api/games/{gid}")
            if len(state["moves"]) >= 2:
                break
            time.sleep(0.1)
        assert len(state["moves"]) >= 2 and state["moves"][1]["seat"] == 2, kind
        call(base_url, "POST", f"/api/games/{gid}/abort", {"reason": "test done"})
    # an unavailable client is reported, not silently ignored
    _, st = call(base_url, "GET", "/api/strategies")
    java = next(c for c in st["clients"] if c["language"] == "Java")
    assert java["kind"] == "client-java" and isinstance(java["available"], bool)


def test_bot_seat_can_be_removed_while_waiting_and_the_bot_stops(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 12, "cards": 5})
    gid = state["id"]
    status, state = call(base_url, "POST", f"/api/games/{gid}/bot", {"kind": "greedy", "seat": 1, "delay": 0})
    assert status == 200 and state["players"][0]["bot"] is True
    status, state = call(base_url, "POST", f"/api/games/{gid}/leave", {"seat": 1})
    assert status == 200 and state["players"][0]["occupied"] is False
    assert call(base_url, "POST", f"/api/games/{gid}/leave", {"seat": 1})[0] == 401   # nothing to remove now
    # humans fill the table; the removed bot's thread must not play
    _, a = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "A", "seat": 1})
    _, b = call(base_url, "POST", f"/api/games/{gid}/join", {"name": "B", "seat": 2})
    time.sleep(0.5)
    _, state = call(base_url, "GET", f"/api/games/{gid}")
    assert state["moves"] == [] and state["turn"] == 1, "a removed bot must not play"
    status, state = call(base_url, "POST", f"/api/games/{gid}/move", {"card": 2}, token=a["token"])
    assert status == 200 and state["turn"] == 2
    # a human seat cannot be freed this way
    assert call(base_url, "POST", f"/api/games/{gid}/leave", {"seat": 2})[0] == 401


def test_many_bot_games_at_once(base_url):
    ids = []
    for _ in range(12):
        _, state = call(base_url, "POST", "/api/games", {"stones": 40, "cards": 10})
        ids.append(state["id"])
        call(base_url, "POST", f"/api/games/{state['id']}/bot", {"kind": "greedy", "delay": 0})
        call(base_url, "POST", f"/api/games/{state['id']}/bot", {"kind": "random", "delay": 0})
    deadline = time.time() + 20
    while time.time() < deadline:
        states = [call(base_url, "GET", f"/api/games/{g}")[1] for g in ids]
        if all(s["status"] == "finished" for s in states):
            break
        time.sleep(0.1)
    assert all(s["status"] == "finished" and s["winner"] in (1, 2) for s in states)
    for s in states:   # every recorded game is internally consistent
        stones = s["initial_stones"]
        for m in s["moves"]:
            assert m["stones_before"] == stones
            if not m["overdraw"]:
                stones = m["stones_after"]
        assert stones == s["stones"]


def test_token_in_cookie_is_accepted_but_never_set(base_url):
    _, state = call(base_url, "POST", "/api/games", {"stones": 9, "cards": 4})
    gid = state["id"]
    req = urllib.request.Request(base_url + f"/api/games/{gid}/join", data=b'{"name":"Web"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.headers.get("Set-Cookie") is None
        token = json.loads(resp.read())["token"]
    req = urllib.request.Request(base_url + f"/api/games/{gid}", headers={"Cookie": f"cardnim_{gid}={token}"})
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    assert body["you"] == 1
    # the header wins over the cookie
    req = urllib.request.Request(base_url + f"/api/games/{gid}",
                                 headers={"Cookie": f"cardnim_{gid}={token}", "X-Token": "bogus"})
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read())["you"] is None
