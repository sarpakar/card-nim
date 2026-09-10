#!/usr/bin/env python3
"""
Card Nim server: hosts any number of games, serves the browser UI, and exposes
the HTTP/JSON API that bots use.  Standard library only; no installs.

Run:
    python3 server/cardnim_server.py                      # lobby at http://localhost:8000
    python3 server/cardnim_server.py --stones 100 --cards 25   # also creates a game and prints its URL
    python3 server/cardnim_server.py --host 0.0.0.0 --port 8000  # reachable from the classroom LAN

Layout of this file:
    GameStore      keeps the games, one lock, one Condition per game, a ticker thread
    Handler        routes HTTP requests (API + static files)
    main()         command line

The engine (rules) lives in engine.py.  The browser pages live in web/.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import (  # noqa: E402
    DEFAULT_TIME_LIMIT, MAX_CARDS, MAX_STONES, NUM_AVATARS, STATUS_FINISHED, STATUS_PLAYING,
    Game, GameError, IllegalMove, new_game_id,
)
from bots import BotRunner, external_clients, registry as bot_registry  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_LONG_POLL = 60.0          # seconds a single long-poll request may hang
TICK_SECONDS = 0.2            # how often the ticker checks clocks
MAX_BODY = 64 * 1024          # request bodies larger than this are rejected
MAX_GAMES = 1000              # oldest finished games are forgotten past this


# ============================================================================ store

class GameStore:
    """All live games plus the machinery to wait for changes.

    One re-entrant lock guards every game.  Each game gets a Condition built on
    that lock; mutations call `changed(game)` to wake long-pollers.  A daemon
    thread ticks every TICK_SECONDS to enforce clocks even when nobody is
    sending requests (a hung bot must still lose on time, and observers must
    see it happen).
    """

    def __init__(self, results_dir: Optional[str]) -> None:
        """Inputs: directory where finished games are written as JSON (None = don't).
        Side effects: starts the ticker thread."""
        self.lock = threading.RLock()
        self.games: dict[str, Game] = {}
        self.conds: dict[str, threading.Condition] = {}
        self.results_dir = results_dir
        self._saved: set[str] = set()
        self.bots = bot_registry(REPO_ROOT)     # key -> (label, function)
        self.externals = external_clients(REPO_ROOT)   # key -> info about a launchable sample client
        self._children: list = []                # child processes started for external clients
        if results_dir:
            try:
                os.makedirs(results_dir, exist_ok=True)
            except OSError as exc:
                print(f"[warn] cannot create {results_dir} ({exc}); results will not be saved", file=sys.stderr)
                self.results_dir = None
        t = threading.Thread(target=self._ticker, name="clock-ticker", daemon=True)
        t.start()

    def create(self, stones: int, cards: int, time_limit: float, label: str = "") -> Game:
        """Purpose: make a new game with a fresh unique id.
        Outputs: the Game.  Side effects: registers it; forgets the oldest
        finished games once there are more than MAX_GAMES; may raise ValueError."""
        with self.lock:
            self._prune()
            game_id = new_game_id()
            while game_id in self.games:
                game_id = new_game_id()
            game = Game(stones, cards, time_limit, label=label, game_id=game_id)
            self.games[game_id] = game
            self.conds[game_id] = threading.Condition(self.lock)
            return game

    def _prune(self) -> None:
        """Purpose: keep memory bounded on a long-running server.
        Side effects: drops the oldest finished games above MAX_GAMES."""
        if len(self.games) < MAX_GAMES:
            return
        finished = sorted((g for g in self.games.values() if g.status == STATUS_FINISHED),
                          key=lambda g: g.finished_at or 0)
        for game in finished[: max(0, len(self.games) - MAX_GAMES + 1)]:
            self.games.pop(game.id, None)
            self.conds.pop(game.id, None)

    def seat_bot(self, game: Game, seat_number: Optional[int], kind: str, avatar: Optional[int],
                 name: Optional[str], delay: float):
        """Purpose: let the server itself play a seat.
        Inputs:  the game, an optional seat, the bot key (see self.bots), an
                 optional avatar and display name, and the seconds the bot
                 pauses before each move so people can follow.
        Outputs: the Seat.  Side effects: joins the seat and starts a daemon
                 thread that plays until the game is over.  The bot's token
                 stays inside the server, so nobody else can move for it.
        Raises KeyError for an unknown bot, GameError from join()."""
        label, fn = self.bots[kind]
        with self.lock:
            seat = game.join(name or label, seat_number, avatar=avatar, is_bot=True)
            BotRunner(self, game, seat.number, fn, delay).start()
            self.changed(game)
            return seat

    def seat_external(self, game: Game, seat_number: Optional[int], kind: str, avatar: Optional[int],
                      name: Optional[str], server_url: str):
        """Purpose: seat one of the sample clients by launching it as a child
        process that joins over HTTP, exactly as a team would run it.
        Inputs:  the game, optional seat, the client kind (see self.externals),
                 optional avatar and name, and the URL the child should use.
        Outputs: the Seat once the child has joined.
        Side effects: starts a process; marks the seat as a bot.
        Raises RuntimeError if the client cannot start or does not join in time."""
        info = self.externals[kind]
        if not info["available"]:
            raise RuntimeError(info["reason"])
        with self.lock:
            if seat_number is None:
                free = [n for n in (1, 2) if not game.seats[n].occupied]
                if not free:
                    raise GameError("both seats are taken")
                seat_number = free[0]
            elif game.seats[seat_number].occupied:
                raise GameError(f"seat {seat_number} is already taken by {game.seats[seat_number].name}")
            self._children = [c for c in self._children if c.poll() is None]
        label = name or info["label"]
        try:
            child = info["launch"](server_url, game.id, seat_number, label, avatar)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"could not build {kind}: {(exc.stderr or '')[-400:]}") from None
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"could not start {kind}: {exc}") from None
        self._children.append(child)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            with self.lock:
                seat = game.seats[seat_number]
                if seat.occupied:
                    seat.is_bot = True
                    if avatar:
                        seat.avatar = int(avatar)
                    game._touch()
                    self.changed(game)
                    return seat
            if child.poll() is not None:
                err = ""
                try:
                    with open(getattr(child, "log_path", ""), encoding="utf-8", errors="replace") as fh:
                        err = fh.read()[-400:]
                except OSError:
                    pass
                raise RuntimeError(f"{kind} exited before joining: {err.strip() or 'no output'}")
            time.sleep(0.1)
        child.kill()
        raise RuntimeError(f"{kind} did not join within 8 seconds")

    def get(self, game_id: str) -> Optional[Game]:
        """Purpose: look a game up by id (case-insensitive).  Outputs: Game or None.
        Side effects: enforces the clock on the way out."""
        with self.lock:
            game = self.games.get(game_id.upper())
            if game is not None and game.check_timeout():
                self.changed(game)
            return game

    def changed(self, game: Game) -> None:
        """Purpose: wake everyone long-polling this game and persist it if over.
        Side effects: notifies the Condition; may write a results file."""
        with self.lock:
            self.conds[game.id].notify_all()
            if game.status == STATUS_FINISHED:
                self._save(game)

    def wait_for(self, game: Game, predicate, timeout: float) -> None:
        """Purpose: block until predicate() is true or the timeout elapses.
        Inputs:  a game, a zero-argument callable, seconds.  Must be called
                 with self.lock held (the Condition releases it while waiting)."""
        self.conds[game.id].wait_for(predicate, timeout=max(0.0, min(timeout, MAX_LONG_POLL)))

    def _ticker(self) -> None:
        """Background loop: enforce clocks, wake pollers when a clock expires."""
        while True:
            time.sleep(TICK_SECONDS)
            try:
                with self.lock:
                    for game in list(self.games.values()):
                        if game.status == STATUS_PLAYING and game.check_timeout():
                            self.changed(game)
            except Exception as exc:  # noqa: BLE001 - never let the clock thread die
                print(f"[warn] clock ticker: {exc}", file=sys.stderr)

    def _save(self, game: Game) -> None:
        """Purpose: write a finished game to results_dir/<id>.json once."""
        if not self.results_dir or game.id in self._saved:
            return
        self._saved.add(game.id)
        path = os.path.join(self.results_dir, f"{game.id}.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(game.to_dict(), fh, indent=2)
        except OSError as exc:  # never let bookkeeping kill a request
            print(f"[warn] could not save {path}: {exc}", file=sys.stderr)


# ============================================================================ http

class ApiError(Exception):
    """An HTTP error to send to the client: status code + message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


ROUTES = [
    # (method, regex, handler-name).  {id} captures a game id.
    ("GET",  r"^/api/games$",                         "list_games"),
    ("POST", r"^/api/games$",                         "create_game"),
    ("GET",  r"^/api/games/(?P<id>[A-Za-z0-9]+)$",            "get_state"),
    ("GET",  r"^/api/games/(?P<id>[A-Za-z0-9]+)/state$",      "get_state"),
    ("GET",  r"^/api/games/(?P<id>[A-Za-z0-9]+)/getstate$",   "getstate"),
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/join$",       "join"),
    ("GET",  r"^/api/games/(?P<id>[A-Za-z0-9]+)/join$",       "join"),
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/move$",       "move"),
    ("GET",  r"^/api/games/(?P<id>[A-Za-z0-9]+)/move$",       "move"),
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/leave$",      "leave"),
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/bot$",        "seat_bot"),
    ("GET",  r"^/api/bots$",                          "bots"),
    ("GET",  r"^/api/strategies$",                    "strategies"),
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/abort$",      "abort"),
    ("GET",  r"^/api/health$",                        "health"),
]
COMPILED_ROUTES = [(m, re.compile(p), h) for m, p, h in ROUTES]


class Handler(BaseHTTPRequestHandler):
    """One instance per request.  `self.server.store` is the GameStore."""

    server_version = "CardNim/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt: str, *args) -> None:  # quieter, timestamped log
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # CORS pre-flight, for pages hosted elsewhere
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")

    def _dispatch(self, method: str) -> None:
        """Purpose: route one request to an API handler or the static server.
        Side effects: writes the whole HTTP response."""
        parsed = urllib.parse.urlsplit(self.path)
        self.route_path = parsed.path
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            for m, regex, name in COMPILED_ROUTES:
                match = regex.match(parsed.path)
                if match and m == method:
                    self.body = self._read_body() if method == "POST" else {}
                    getattr(self, "api_" + name)(**match.groupdict())
                    return
                if match and m != method and not any(
                    r.match(parsed.path) and mm == method for mm, r, _ in COMPILED_ROUTES
                ):
                    raise ApiError(405, f"{method} not allowed here")
            if method == "GET":
                self._serve_static(parsed.path)
            else:
                raise ApiError(404, "no such endpoint")
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - last resort, keep the server alive
            self.log_message("500 %r", exc)
            try:
                self._send_json({"error": f"server error: {exc}"}, 500)
            except Exception:
                pass

    def _read_body(self) -> dict:
        """Purpose: parse a POST body (JSON or form-encoded) into a dict.
        Outputs: dict of string keys; JSON values keep their types.
        Raises ApiError 400 on malformed input."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length header") from None
        if length < 0:
            raise ApiError(400, "bad Content-Length header")
        if length > MAX_BODY:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        text = raw.decode("utf-8", errors="replace")
        if ctype == "application/x-www-form-urlencoded":
            return {k: v[-1] for k, v in urllib.parse.parse_qs(text).items()}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # be forgiving: allow form-encoded bodies with no content-type
            data = {k: v[-1] for k, v in urllib.parse.parse_qs(text).items()}
            if not data:
                raise ApiError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data

    def param(self, name: str, default=None):
        """Purpose: read a parameter from the JSON body, then the query string."""
        if name in self.body:
            return self.body[name]
        return self.query.get(name, default)

    def int_param(self, name: str, default=None, lo=None, hi=None) -> Optional[int]:
        """Purpose: read an integer parameter with range checking.
        Raises ApiError 400 if it is missing (and no default) or out of range."""
        raw = self.param(name, default)
        if raw is None or raw == "":
            if default is None:
                raise ApiError(400, f"missing parameter: {name}")
            return default
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            raise ApiError(400, f"{name} must be an integer")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ApiError(400, f"{name} must be an integer") from None
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ApiError(400, f"{name} must be between {lo} and {hi}")
        return value

    def float_param(self, name: str, default: float, lo: float, hi: float) -> float:
        """Purpose: read a number parameter, clamped to [lo, hi]; garbage or
        NaN falls back to the default instead of failing the request."""
        raw = self.param(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN
            return default
        return max(lo, min(hi, value))

    def token(self, game: Game) -> Optional[str]:
        """Purpose: find the caller's seat token.  Looks in the X-Token header,
        the `token` body/query parameter, then a cookie named cardnim_{id}
        (accepted for clients that prefer cookies; the server never sets one)."""
        tok = self.headers.get("X-Token") or self.param("token")
        if tok:
            return str(tok)
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == f"cardnim_{game.id}":
                return urllib.parse.unquote(v)
        return None

    def wants_text(self) -> bool:
        return (self.query.get("format") or "").lower() in ("text", "txt", "plain")

    def _send_json(self, payload, status: int = 200, extra_headers=()) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, status, "application/json; charset=utf-8", extra_headers)

    def _send_text(self, text: str, status: int = 200, extra_headers=()) -> None:
        self._send_bytes(text.encode("utf-8"), status, "text/plain; charset=utf-8", extra_headers)

    def _send_bytes(self, body: bytes, status: int, ctype: str, extra_headers=()) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_state(self, game: Game, viewer_seat: Optional[int], status: int = 200, extra_headers=()) -> None:
        """Purpose: send the game state in JSON or, with ?format=text, plain text."""
        if self.wants_text():
            self._send_text(game.to_text(viewer_seat=viewer_seat), status, extra_headers)
        else:
            self._send_json(game.to_dict(viewer_seat=viewer_seat), status, extra_headers)

    def _game(self, game_id: str) -> Game:
        game = self.server.store.get(game_id)
        if game is None:
            raise ApiError(404, f"no game with id {game_id.upper()}")
        return game

    # ------------------------------------------------------------ API handlers

    def api_health(self) -> None:
        """GET /api/health -> {"ok": true, "games": n, "lobby_url": "http://10.1.2.3:8000/"}
        lobby_url is the address other devices on the network should use."""
        self._send_json({"ok": True, "games": len(self.server.store.games), "time": time.time(),
                         "lobby_url": getattr(self.server, "public_url", "")})

    def api_list_games(self) -> None:
        """GET /api/games -> {"games": [summary, ...]} newest first."""
        store = self.server.store
        with store.lock:
            games = sorted(store.games.values(), key=lambda g: g.created_at, reverse=True)
            self._send_json({"games": [g.summary() for g in games]})

    def api_create_game(self) -> None:
        """POST /api/games {stones, cards, time_limit?, label?} -> full state (201).
        Side effects: creates the game."""
        stones = self.int_param("stones", lo=1, hi=MAX_STONES)
        cards = self.int_param("cards", lo=1, hi=MAX_CARDS)
        raw_limit = self.param("time_limit", DEFAULT_TIME_LIMIT)
        try:
            time_limit = float(raw_limit)
        except (TypeError, ValueError):
            raise ApiError(400, "time_limit must be a number of seconds") from None
        if not (1 <= time_limit <= 24 * 3600):
            raise ApiError(400, "time_limit must be between 1 and 86400 seconds")
        label = str(self.param("label", "") or "")
        try:
            game = self.server.store.create(stones, cards, time_limit, label)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        self.log_message("created game %s (s=%d k=%d clock=%gs)", game.id, stones, cards, time_limit)
        self._send_state(game, None, 201)

    def api_get_state(self, id: str) -> None:
        """GET /api/games/{id}[/state]?since=V&timeout=S[&format=text]
        Observer/long-poll endpoint.  With `since`, the response is delayed until
        the game's version exceeds V or S seconds pass (default 25, max 60)."""
        game = self._game(id)
        store = self.server.store
        since = self.int_param("since", default=-1)
        timeout = self.float_param("timeout", 25.0, 0.0, MAX_LONG_POLL)
        with store.lock:
            if since >= 0:
                store.wait_for(game, lambda: game.version > since, timeout)
            seat = game.seat_for_token(self.token(game))
            self._send_state(game, seat.number if seat else None)

    def api_getstate(self, id: str) -> None:
        """GET /api/games/{id}/getstate  (token required)
        The course's `getstate`: returns only when it is the caller's turn or the
        game is over.  Waits at most `timeout` seconds (default 60); if it gives
        up early the state is returned with your_turn = false and the caller
        simply calls again.  The caller's clock is unaffected by waiting."""
        game = self._game(id)
        store = self.server.store
        timeout = self.float_param("timeout", MAX_LONG_POLL, 0.0, MAX_LONG_POLL)
        with store.lock:
            seat = game.seat_for_token(self.token(game))
            if seat is None:
                raise ApiError(401, "missing or unknown token; join the game first")

            def ready() -> bool:
                return game.status == STATUS_FINISHED or (
                    game.status == STATUS_PLAYING and game.turn == seat.number)

            store.wait_for(game, ready, timeout)
            self._send_state(game, seat.number)

    def api_join(self, id: str) -> None:
        """POST /api/games/{id}/join {name, seat?, avatar?} -> {seat, token, state}
        avatar is 1..16 (see /avatars/avNN.png); omitted = picked from the name.
        Side effects: seats the player; starts the game when full.
        No cookie is set: a cookie is shared by every tab of a browser, so two
        people playing from two tabs would overwrite each other's seat.  The
        page keeps its token per tab instead; bots send it as a header."""
        game = self._game(id)
        store = self.server.store
        name = str(self.param("name", "") or "")
        seat_pref = self.param("seat")
        seat_number = None
        if seat_pref not in (None, "", "any"):
            seat_number = self.int_param("seat", lo=1, hi=2)
        avatar = None
        if self.param("avatar") not in (None, ""):
            avatar = self.int_param("avatar", lo=1, hi=NUM_AVATARS)
        with store.lock:
            try:
                seat = game.join(name, seat_number, avatar=avatar)
            except GameError as exc:
                raise ApiError(409, str(exc)) from None
            store.changed(game)
            self.log_message("game %s: %s sat down at seat %d", game.id, seat.name, seat.number)
            payload = {"seat": seat.number, "token": seat.token, "state": game.to_dict(viewer_seat=seat.number)}
            if self.wants_text():
                self._send_text(f"seat {seat.number}\ntoken {seat.token}\n" + game.to_text(viewer_seat=seat.number))
            else:
                self._send_json(payload)

    def api_move(self, id: str) -> None:
        """POST /api/games/{id}/move {card} (token required) -> state
        Validates the move; on rejection returns 4xx with {"error": ...} and the
        game is unchanged (the mover's clock keeps running)."""
        game = self._game(id)
        store = self.server.store
        card = self.int_param("card")
        with store.lock:
            seat = game.seat_for_token(self.token(game))
            if seat is None:
                raise ApiError(401, "missing or unknown token; join the game first")
            try:
                move = game.play(seat.number, card)
            except IllegalMove as exc:
                raise ApiError(409, str(exc)) from None
            store.changed(game)
            self.log_message("game %s: seat %d (%s) plays %d -> %d stones [%.2fs]",
                             game.id, seat.number, seat.name, move.card, max(0, move.stones_after), move.elapsed)
            if game.status == STATUS_FINISHED:
                self.log_message("game %s: over. %s", game.id, game.reason)
            self._send_state(game, seat.number)

    def api_bots(self) -> None:
        """GET /api/bots -> {"bots": [{"kind": "greedy", "label": "Greedy bot"}, ...]}
        Server-run bots first, then the sample clients this machine can launch."""
        store = self.server.store
        bots = [{"kind": k, "label": v[0]} for k, v in store.bots.items()]
        bots += [{"kind": k, "label": v["label"]} for k, v in store.externals.items() if v["available"]]
        self._send_json({"bots": bots})

    def api_strategies(self) -> None:
        """GET /api/strategies -> {"clients": [...], "bots": [...]}
        The sample clients found under clients/ (language, file, the function
        to replace, how to run) and the bots the server can seat with the file
        each lives in.  Shown in the lobby so teams know where to start."""
        langs = {".py": ("Python", "brain(stones, my_cards, opp_cards, state)",
                         "python3 {file} --game ID --name NAME --bot greedy"),
                 ".cpp": ("C++", "choose_card(stones, my_cards, opp_cards)",
                          "g++ -std=c++17 -O2 {file} -o client && ./client --game ID --name NAME"),
                 ".java": ("Java", "chooseCard(stones, myCards, oppCards)",
                           "javac {file} -d out && java -cp out Client --game ID --name NAME"),
                 ".sh": ("Shell", "the card chosen in the loop", "sh {file}")}
        clients = []
        root = os.path.join(REPO_ROOT, "clients")
        if os.path.isdir(root):
            for folder in sorted(os.listdir(root)):
                sub = os.path.join(root, folder)
                if not os.path.isdir(sub):
                    continue
                for name in sorted(os.listdir(sub)):
                    ext = os.path.splitext(name)[1].lower()
                    if ext in langs:
                        rel = f"clients/{folder}/{name}"
                        lang, edit, run = langs[ext]
                        clients.append({"language": lang, "file": rel, "folder": f"clients/{folder}/",
                                        "edit": edit, "run": run.format(file=rel)})
        bots = []
        for kind, (label, fn) in self.server.store.bots.items():
            try:
                rel = os.path.relpath(fn.__code__.co_filename, REPO_ROOT)
            except (AttributeError, ValueError):
                rel = "server/bots.py"
            bots.append({"kind": kind, "label": label, "file": rel,
                         "private": rel.startswith("private/")})
        # what each client folder contains, strategy-wise: its own sample
        # strategy, and for Python also the bots the server can run (they are
        # Python too, so that is where a team would look for them)
        ext = self.server.store.externals
        for c in clients:
            kind = {"Python": "client-python", "C++": "client-cpp", "Java": "client-java"}.get(c["language"])
            info = ext.get(kind or "", {})
            c["kind"] = kind if info else None
            c["available"] = bool(info.get("available"))
            c["reason"] = info.get("reason", "")
            c["strategies"] = [{"name": "Sample strategy", "file": c["file"],
                                "note": f"in {c['edit'].split('(')[0]}(): wins if it can, avoids giving an exact match, else the smallest fitting card"}]
            if c["language"] == "Python":
                c["strategies"] += [{"name": b["label"], "file": b["file"],
                                     "note": "server-run bot" + (", not distributed" if b["private"] else "")}
                                    for b in bots]
        self._send_json({"clients": clients, "bots": bots})

    def api_seat_bot(self, id: str) -> None:
        """POST /api/games/{id}/bot {kind, seat?, avatar?, name?, delay?} -> state
        Seats a server-run bot (see GET /api/bots).  delay is the pause in
        seconds before each of its moves (default 0.8, so people can follow)."""
        game = self._game(id)
        store = self.server.store
        kind = str(self.param("kind", "") or "")
        if kind not in store.bots and kind not in store.externals:
            raise ApiError(400, f"unknown bot '{kind}'; see /api/bots")
        seat_pref = self.param("seat")
        seat_number = None
        if seat_pref not in (None, "", "any"):
            seat_number = self.int_param("seat", lo=1, hi=2)
        avatar = None
        if self.param("avatar") not in (None, ""):
            avatar = self.int_param("avatar", lo=1, hi=NUM_AVATARS)
        name = self.param("name")
        delay = self.float_param("delay", 0.8, 0.0, 30.0)
        try:
            if kind in store.bots:
                seat = store.seat_bot(game, seat_number, kind, avatar, str(name) if name else None, delay)
            else:
                url = f"http://127.0.0.1:{self.server.server_address[1]}"
                seat = store.seat_external(game, seat_number, kind, avatar, str(name) if name else None, url)
        except GameError as exc:
            raise ApiError(409, str(exc)) from None
        except RuntimeError as exc:
            raise ApiError(502, str(exc)) from None
        self.log_message("game %s: bot '%s' sat down at seat %d as %s", game.id, kind, seat.number, seat.name)
        self._send_state(game, None)

    def api_leave(self, id: str) -> None:
        """POST /api/games/{id}/leave (token required) -> state
        Before the game starts: frees the seat (the token stops working).
        During play: the caller resigns and the opponent wins."""
        game = self._game(id)
        store = self.server.store
        with store.lock:
            seat = game.seat_for_token(self.token(game))
            if seat is None and self.param("seat") not in (None, ""):
                # a bot seat has no owner: anyone may free it while the table is waiting
                number = self.int_param("seat", lo=1, hi=2)
                candidate = game.seats[number]
                if candidate.occupied and candidate.is_bot and game.status == "waiting":
                    seat = candidate
            if seat is None:
                raise ApiError(401, "missing or unknown token")
            name, number = seat.name, seat.number
            try:
                game.leave(number)
            except GameError as exc:
                raise ApiError(409, str(exc)) from None
            store.changed(game)
            self.log_message("game %s: %s left seat %d (%s)", game.id, name, number,
                             "resigned" if game.status == STATUS_FINISHED else "seat is open again")
            self._send_state(game, None)

    def api_abort(self, id: str) -> None:
        """POST /api/games/{id}/abort {reason?} -> state.  No auth: classroom tool."""
        game = self._game(id)
        store = self.server.store
        with store.lock:
            game.abort(str(self.param("reason", "") or "aborted by the architects")[:120])
            store.changed(game)
            self.log_message("game %s aborted", game.id)
            self._send_state(game, None)

    # ------------------------------------------------------------ static files

    def _serve_static(self, path: str) -> None:
        """Purpose: serve the browser UI from web/.
        /            -> index.html (lobby)
        /game/{id}   -> game.html (board; the page reads the id from the URL)
        /<file>      -> web/<file>"""
        if path in ("", "/"):
            rel = "index.html"
        elif re.match(r"^/game/[A-Za-z0-9]+/?$", path):
            rel = "game.html"
        else:
            rel = path.lstrip("/")
        rel = urllib.parse.unquote(rel)
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        inside = os.path.commonpath([WEB_DIR, full]) == WEB_DIR
        if not inside or not os.path.isfile(full):
            raise ApiError(404, f"not found: {path}")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        self._send_bytes(body, 200, ctype)


class CardNimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, store: GameStore, quiet: bool = False, public_url: str = "") -> None:
        super().__init__(addr, Handler)
        self.store = store
        self.quiet = quiet
        # the address other devices on the network should use (shown as a QR code)
        host = addr[0] if addr[0] not in ("", "0.0.0.0") else lan_ip()
        self.public_url = public_url or f"http://{host}:{self.server_address[1]}/"


def lan_ip() -> str:
    """Purpose: best-effort guess of this machine's LAN address to print for teams."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Card Nim game server")
    parser.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--stones", type=int, help="also create a game with this many stones")
    parser.add_argument("--cards", type=int, help="...and cards 1..k for each player")
    parser.add_argument("--clock", type=float, default=DEFAULT_TIME_LIMIT,
                        help="seconds per player for the auto-created game (default 120)")
    parser.add_argument("--label", default="", help="label for the auto-created game")
    parser.add_argument("--results", default=os.path.join(os.path.dirname(WEB_DIR), "..", "results"),
                        help="directory for finished-game JSON files ('' to disable)")
    parser.add_argument("--quiet", action="store_true", help="do not log every request")
    parser.add_argument("--public-url", default="", help="address to show in the lobby's QR code (default: this machine's LAN address)")
    args = parser.parse_args(argv)

    results_dir = os.path.abspath(args.results) if args.results else None
    store = GameStore(results_dir)
    try:
        server = CardNimServer((args.host, args.port), store, quiet=args.quiet, public_url=args.public_url)
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc}\n"
              f"another server is probably running; stop it or pass --port {args.port + 1}", file=sys.stderr)
        return 1

    shown_host = lan_ip() if args.host in ("0.0.0.0", "") else args.host
    print(f"Card Nim server listening on http://{shown_host}:{args.port}/  (lobby)")
    if results_dir:
        print(f"finished games are saved to {results_dir}")
    if args.stones or args.cards:
        if not (args.stones and args.cards):
            parser.error("--stones and --cards go together")
        game = store.create(args.stones, args.cards, args.clock, args.label)
        print(f"created game {game.id}: {args.stones} stones, cards 1..{args.cards}, {args.clock:g}s per player")
        print(f"  board:   http://{shown_host}:{args.port}/game/{game.id}")
        print(f"  api:     http://{shown_host}:{args.port}/api/games/{game.id}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
        for child in getattr(store, "_children", []):      # sample clients we launched
            if child.poll() is None:
                child.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
