#!/usr/bin/env python3
"""
Card Nim server: hosts any number of games, serves the browser UI, and exposes
the HTTP/JSON API that bots use.  Standard library only; no installs.

Run:
    python3 server/cardnim_server.py                      # lobby at http://localhost:8000
    python3 server/cardnim_server.py --stones 100 --cards 25   # also creates a game and prints its URL
    python3 server/cardnim_server.py --host 0.0.0.0 --port 8000  # reachable from the classroom LAN

Layout of this file:
    GameStore      keeps the games and tournaments, one lock, one Condition
                   per game and per tournament, a ticker thread
    Handler        routes HTTP requests (API + static files)
    main()         command line

The engine (rules) lives in engine.py, the bracket in tournament.py.  The
browser pages live in web/.
"""

from __future__ import annotations

import argparse
import email
import json
import mimetypes
import os
import random
import re
import socket
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
from bots import BotRunner, registry as bot_registry  # noqa: E402
from clients import (  # noqa: E402
    ClientRegistry, UploadTooLarge, save_bundle, save_upload, upload_languages,
)
import clients as clients_mod  # noqa: E402
from tournament import KIND_API, KIND_HUMAN, Tournament  # noqa: E402
import tournament as tmod  # noqa: E402

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_LONG_POLL = 60.0          # seconds a single long-poll request may hang
TICK_SECONDS = 0.2            # how often the ticker checks clocks
MAX_BODY = 64 * 1024          # request bodies larger than this are rejected
MAX_GAMES = 1000              # oldest finished games are forgotten past this
MAX_TOURNAMENTS = 200         # same for tournaments


# ============================================================================ store

class GameStore:
    """All live games plus the machinery to wait for changes.

    One re-entrant lock guards every game.  Each game gets a Condition built on
    that lock; mutations call `changed(game)` to wake long-pollers.  A daemon
    thread ticks every TICK_SECONDS to enforce clocks even when nobody is
    sending requests (a hung bot must still lose on time, and observers must
    see it happen).
    """

    def __init__(self, results_dir: Optional[str], client_dirs: Optional[list] = None,
                 uploads_dir: Optional[str] = None) -> None:
        """Inputs: directory where finished games are written as JSON (None = don't),
        extra directories to look for client manifests in, and the folder that
        accepts uploaded strategies (None = uploads are refused).
        Side effects: starts the ticker thread."""
        self.lock = threading.RLock()
        self.games: dict[str, Game] = {}
        self.conds: dict[str, threading.Condition] = {}
        self.tournaments: dict[str, Tournament] = {}
        self.tconds: dict[str, threading.Condition] = {}
        self.local_url = "http://127.0.0.1:8000"   # the server sets this once it knows its port
        self.results_dir = results_dir
        self._saved: set[str] = set()
        self.bots = bot_registry(REPO_ROOT)     # key -> (label, function)
        self.uploads_dir = uploads_dir          # None unless --accept-uploads
        dirs = [os.path.join(REPO_ROOT, "clients")] + list(client_dirs or [])
        if uploads_dir:
            dirs.append(uploads_dir)
        self.externals = ClientRegistry(REPO_ROOT, dirs)   # clients in any language, from clients/*/client.json
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
        spec = self.externals[kind]
        if not spec.available:
            raise RuntimeError(spec.reason)
        with self.lock:
            if seat_number is None:
                free = [n for n in (1, 2) if not game.seats[n].occupied]
                if not free:
                    raise GameError("both seats are taken")
                seat_number = free[0]
            elif game.seats[seat_number].occupied:
                raise GameError(f"seat {seat_number} is already taken by {game.seats[seat_number].name}")
            self._children = [c for c in self._children if c.poll() is None]
        label = name or spec.label
        try:
            child = spec.launch(server_url, game.id, seat_number, label, avatar)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(str(exc)) from None
        self._children.append(child)
        deadline = time.time() + spec.join_timeout
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
        raise RuntimeError(f"{kind} did not join within {spec.join_timeout:g} seconds")

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
        Side effects: notifies the Condition; may write a results file.  A
        game that belongs to a tournament also wakes the bracket's pollers,
        and when it is over the bracket moves on (next games are created)."""
        with self.lock:
            self.conds[game.id].notify_all()
            if game.status == STATUS_FINISHED:
                self._save(game)
            if game.tournament:
                t = self.tournaments.get(game.tournament["id"])
                if t is not None:
                    if game.status == STATUS_FINISHED:
                        self._tournament_game_over(t, game)
                    t._touch()
                    self.tournament_changed(t)

    def wait_for(self, game: Game, predicate, timeout: float) -> None:
        """Purpose: block until predicate() is true or the timeout elapses.
        Inputs:  a game, a zero-argument callable, seconds.  Must be called
                 with self.lock held (the Condition releases it while waiting)."""
        self.conds[game.id].wait_for(predicate, timeout=max(0.0, min(timeout, MAX_LONG_POLL)))

    # ------------------------------------------------------------ tournaments

    def create_tournament(self, stones: int, cards: int, time_limit: float, label: str = "",
                          bot_delay: float = 1.5) -> Tournament:
        """Purpose: open a new tournament for entries.
        Outputs: the Tournament.  Side effects: registers it; forgets the
        oldest finished tournaments past MAX_TOURNAMENTS; may raise ValueError."""
        with self.lock:
            finished = sorted((t for t in self.tournaments.values() if t.status == tmod.STATUS_FINISHED),
                              key=lambda t: t.finished_at or 0)
            for old in finished[: max(0, len(self.tournaments) - MAX_TOURNAMENTS + 1)]:
                self.tournaments.pop(old.id, None)
                self.tconds.pop(old.id, None)
            tid = new_game_id()
            while tid in self.tournaments or tid in self.games:
                tid = new_game_id()
            t = Tournament(stones, cards, time_limit, label=label, tournament_id=tid, bot_delay=bot_delay)
            self.tournaments[tid] = t
            self.tconds[tid] = threading.Condition(self.lock)
            return t

    def get_tournament(self, tid: str) -> Optional[Tournament]:
        """Purpose: look a tournament up by id (case-insensitive)."""
        with self.lock:
            return self.tournaments.get(tid.upper())

    def game_info(self, game_id: Optional[str]) -> Optional[dict]:
        """Purpose: the summary of a game for the bracket view, or None."""
        game = self.games.get(game_id or "")
        return game.summary(hands=True) if game is not None else None

    def tournament_changed(self, t: Tournament) -> None:
        """Purpose: wake everyone long-polling this bracket; save it when over."""
        with self.lock:
            cond = self.tconds.get(t.id)
            if cond is not None:
                cond.notify_all()
            if t.status == tmod.STATUS_FINISHED:
                self._save_tournament(t)

    def wait_for_tournament(self, t: Tournament, predicate, timeout: float) -> None:
        """Purpose: block until predicate() is true or the timeout elapses.
        Must be called with self.lock held."""
        self.tconds[t.id].wait_for(predicate, timeout=max(0.0, min(timeout, MAX_LONG_POLL)))

    def start_tournament(self, t: Tournament) -> None:
        """Purpose: draw the bracket and create the first games.
        Side effects: as Tournament.start() plus one game per ready match."""
        with self.lock:
            t.start()
            self._fill_matches(t)
            self.tournament_changed(t)

    def pace_after_move(self, game: Game) -> None:
        """Purpose: hold a tournament match for its bot_delay after each move,
        so a room can follow the play.

        The server's own bots sleep before each move, so they are already
        watchable; this is for the other kind.  A client is a separate process
        and answers as fast as the socket allows, so a whole match goes by in a
        fifth of a second.  Pausing is what slows it down, and pausing stops
        the clocks, so the wait costs neither side any time.

        Called from the move endpoint only, which is the path a client takes -
        pacing a BotRunner here as well would delay it twice."""
        info = game.tournament
        if not info or game.status != STATUS_PLAYING or game.paused:
            return
        t = self.tournaments.get(info["id"])
        if t is None or t.bot_delay <= 0:
            return
        game.pause(paced=True)

        def wake() -> None:
            time.sleep(t.bot_delay)
            with self.lock:
                # only undo our own pause: if the room held the game while we
                # were asleep, it stays held until they say otherwise
                if game.status == STATUS_PLAYING and game.paused and game.paced:
                    game.resume()
                    self.changed(game)
        threading.Thread(target=wake, name=f"pace-{game.id}", daemon=True).start()

    def _live_match_game(self, t: Tournament):
        """Purpose: the tournament's game that is still being played, if any.
        Outputs: the Game, or None when every match so far has finished."""
        for round_ in t.rounds:
            for match in round_:
                game = self.games.get(match.game_id or "")
                if game is not None and game.status != STATUS_FINISHED:
                    return game
        return None

    def _fill_matches(self, t: Tournament) -> None:
        """Purpose: nothing, deliberately.

        Matches are not started by the server.  On competition night the
        organiser announces a match, gets the room's attention and then starts
        it, so a match waits at `ready` until start_match() is called for it.
        The bracket page puts a Start button on each one."""
        return

    def start_match(self, t: Tournament, round_number: int, index: int) -> Game:
        """Purpose: start one particular match, by hand.
        Inputs:  the tournament, the 1-based round and 0-based match index.
        Outputs: the Game that was created.
        Raises GameError if the match is not ready, has already been played,
        or another match of this tournament is still in progress."""
        with self.lock:
            if t.status != tmod.STATUS_RUNNING:
                # an open bracket has no matches yet; a finished one must not
                # gain a game that can never advance it
                raise GameError("the tournament is not running")
            rounds = t.rounds
            if not (1 <= round_number <= len(rounds)):
                raise GameError(f"round {round_number} does not exist")
            matches = rounds[round_number - 1]
            if not (0 <= index < len(matches)):
                raise GameError(f"match {index + 1} does not exist in that round")
            match = matches[index]
            if match.bye or match.winner is not None:
                raise GameError("that match is already decided")
            if any(s is None for s in match.slots):
                raise GameError("that match is waiting for the previous round")
            live = self._live_match_game(t)
            if live is not None:
                raise GameError(f"game {live.id} is still being played")
            existing = self.games.get(match.game_id or "")
            if existing is not None and existing.status != STATUS_FINISHED:
                raise GameError("that match already has a game")
            game = self._make_match_game(t, match)
            self.tournament_changed(t)
            return game

    def _make_match_game(self, t: Tournament, match) -> Game:
        """Purpose: one game for one match.
        Side effects: creates the game with the tournament's settings, tosses
        a coin for who moves first, reserves both seats for the entrants, and
        sits server-run bots down at once (sample clients are launched in the
        background and join over HTTP like a team would)."""
        a, b = (t.entrants[i] for i in match.slots)
        if random.random() < 0.5:
            a, b = b, a
        rname = t.round_name(match.round)
        label = f"{t.title}: {rname}" + (f" {match.index + 1}" if len(t.rounds[match.round - 1]) > 1 else "")
        game = self.create(t.stones, t.cards, t.time_limit, label)
        game.tournament = {"id": t.id, "label": t.title, "round": match.round, "round_name": rname,
                           "match": match.index + 1, "rounds": len(t.rounds)}
        t.attach_game(match, game.id, {1: a.id, 2: b.id})
        for seat_number, entrant in ((1, a), (2, b)):
            game.reserve(seat_number, entrant.name, entrant.avatar, entrant.token)
            if entrant.kind in self.bots:
                _, fn = self.bots[entrant.kind]
                game.join(entrant.name, seat_number, avatar=entrant.avatar, is_bot=True, claim=entrant.token)
                BotRunner(self, game, seat_number, fn, t.bot_delay).start()
            elif entrant.kind in self.externals:
                threading.Thread(target=self._seat_external_quietly, name=f"launch-{game.id}-{seat_number}",
                                 args=(game, seat_number, entrant.kind, entrant.avatar, entrant.name), daemon=True).start()
        self.changed(game)
        return game

    def _seat_external_quietly(self, game: Game, seat_number: int, kind: str, avatar: int, name: str) -> None:
        """Purpose: launch a sample client for a tournament seat off the lock;
        a failure is logged and the seat stays reserved (the organiser can
        hand the match to the other side)."""
        try:
            self.seat_external(game, seat_number, kind, avatar, name, self.local_url)
        except (GameError, RuntimeError) as exc:
            print(f"[warn] game {game.id}: could not seat {kind} as {name}: {exc}", file=sys.stderr)

    def _tournament_game_over(self, t: Tournament, game: Game) -> None:
        """Purpose: feed a finished game back into its bracket.
        Side effects: a winner moves on and the next games are created; a
        game with no winner (aborted) is replaced by a fresh one."""
        if t.status != tmod.STATUS_RUNNING:
            return
        match = t.match_for_game(game.id)
        if match is None or match.game_id != game.id or match.winner is not None:
            return
        if game.winner:
            t.record_result(game.id, game.winner)
        else:
            t.detach_game(match)
        self._fill_matches(t)

    def walkover(self, t: Tournament, round_number: int, index: int, winner_id: int) -> None:
        """Purpose: give a match to one side; its game, if still going, is
        aborted.  Side effects: the bracket moves on."""
        with self.lock:
            match = t.walkover(round_number, index, winner_id)
            game = self.games.get(match.game_id or "")
            if game is not None and game.status != STATUS_FINISHED:
                game.abort(f"walkover: {t.entrants[winner_id].name} advances")
                self.changed(game)
            self._fill_matches(t)
            self.tournament_changed(t)

    def abort_tournament(self, t: Tournament, reason: str) -> None:
        """Purpose: end a tournament early; every unfinished game is aborted."""
        with self.lock:
            t.abort(reason)
            for r in t.rounds:
                for m in r:
                    game = self.games.get(m.game_id or "")
                    if game is not None and game.status != STATUS_FINISHED:
                        game.abort("tournament aborted")
                        self.changed(game)
            self.tournament_changed(t)

    def _save_tournament(self, t: Tournament) -> None:
        """Purpose: write a finished bracket to results_dir/tournament-<id>.json once."""
        key = f"tournament-{t.id}"
        if not self.results_dir or key in self._saved:
            return
        self._saved.add(key)
        path = os.path.join(self.results_dir, f"{key}.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(t.to_dict(self.game_info), fh, indent=2)
        except OSError as exc:
            print(f"[warn] could not save {path}: {exc}", file=sys.stderr)

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


def _parse_multipart(body: bytes, content_type: str) -> tuple:
    """Purpose: pull the files out of a multipart/form-data upload, which is
    how a browser sends a strategy made of several files.
    Inputs:  the raw body and the request's Content-Type (it carries the
             boundary).
    Outputs: ([(path, bytes)], entry) -- every part that came with a file name,
             keeping the path the browser reported (a folder picked in the page
             reports "my-bot/main.py"), and the value of an `entry` field if
             the page said which file starts the bot.
    The stdlib email parser does the boundary work; nothing here decodes or
    interprets the bytes.  A part without a file name is an ordinary form
    field."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    try:
        message = email.message_from_bytes(header + body)
        parts = message.get_payload() if message.is_multipart() else []
    except (ValueError, TypeError) as exc:
        raise ApiError(400, f"that form could not be read ({exc})") from None
    if not parts:
        raise ApiError(400, "that form could not be read (no parts in it)")
    files, entry = [], ""
    for part in parts:
        if not hasattr(part, "get_filename"):
            continue
        name = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if name:
            files.append((name, payload))
        elif part.get_param("name", header="content-disposition") == "entry":
            entry = payload.decode("utf-8", "replace").strip()
    return files, entry


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
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/pause$",      "pause"),
    ("POST", r"^/api/games/(?P<id>[A-Za-z0-9]+)/resume$",     "resume"),
    ("GET",  r"^/api/tournaments$",                                    "list_tournaments"),
    ("POST", r"^/api/tournaments$",                                    "create_tournament"),
    ("GET",  r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)$",               "get_tournament"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/join$",          "join_tournament"),
    ("GET",  r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/join$",          "join_tournament"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/leave$",         "leave_tournament"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/start$",         "start_tournament"),
    ("GET",  r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/getstate$",      "tournament_getstate"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/play$",          "play_match"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/walkover$",      "walkover"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/abort$",         "abort_tournament"),
    ("POST", r"^/api/tournaments/(?P<id>[A-Za-z0-9]+)/restart$",       "restart_tournament"),
    ("POST", r"^/api/uploads$",                       "upload"),
    ("GET",  r"^/api/uploads$",                       "upload_info"),
    ("GET",  r"^/api/health$",                        "health"),
]
# handlers that read the request body themselves: an upload is raw file bytes,
# not JSON or a form, so _read_body() must not touch it.
RAW_BODY_ROUTES = {"upload"}
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
                    self.body = {} if name in RAW_BODY_ROUTES else (
                        self._read_body() if method == "POST" else {})
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

    def _drain(self, length: int, cap: int = 16 * 1024 * 1024) -> None:
        """Purpose: swallow a request body we are about to refuse.
        Without this the server answers and closes while the client is still
        sending, and the client sees a connection reset instead of the 413 we
        went to the trouble of writing.  Reads at most `cap` so an endless
        body cannot tie the connection up."""
        remaining = min(max(0, length), cap)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

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
            self._drain(length)
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

    def token(self, game: Optional[Game] = None) -> Optional[str]:
        """Purpose: find the caller's token.  Looks in the X-Token header,
        the `token` body/query parameter, then (for a game) a cookie named
        cardnim_{id} (accepted for clients that prefer cookies; the server
        never sets one)."""
        tok = self.headers.get("X-Token") or self.param("token")
        if tok:
            return str(tok)
        if game is None:
            return None
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

    def client_ip(self) -> str:
        """The address this request came from, with the IPv6 wrapper that an
        IPv4 client gets on a dual-stack socket taken off."""
        ip = (self.client_address[0] or "") if self.client_address else ""
        return ip[7:] if ip.startswith("::ffff:") else ip

    def is_local_client(self) -> bool:
        """Purpose: true when this request came from the machine the server is
        running on, whether it asked for localhost or for the LAN address.
        Used to keep host-only furniture off the visitors' screens."""
        ip = self.client_ip()
        if ip.startswith("127.") or ip == "::1":
            return True
        return ip in getattr(self.server, "local_addresses", set())

    def is_organiser(self) -> bool:
        """Purpose: true when this request may *run* the event -- draw the
        bracket, start a match, call a no-show, abort, take someone else's
        entry out.  Entering, playing and watching are open to the room; the
        buttons that decide when a match begins are not, or the first team to
        find the page could start every match on the projector.

        The organiser is the machine the server runs on.  --controls-from adds
        another (the laptop you project from), and --open-controls goes back to
        anybody who can reach the page."""
        if getattr(self.server, "open_controls", False):
            return True
        if self.is_local_client():
            return True
        return self.client_ip() in getattr(self.server, "organiser_addresses", set())

    def require_organiser(self, what: str) -> None:
        """Raises 403 unless this request may run the event.  `what` finishes
        the sentence "only the machine running the server can ...", so it reads
        as an explanation rather than a refusal."""
        if not self.is_organiser():
            raise ApiError(403, f"only the machine running the server can {what} "
                                f"(this is {self.client_ip() or 'another device'}; "
                                f"start the server with --open-controls to let anyone)")

    def api_health(self) -> None:
        """GET /api/health -> {"ok": true, "games": n, "lobby_url": "http://10.1.2.3:8000/"}
        lobby_url is the address other devices on the network should use."""
        self._send_json({"ok": True, "games": len(self.server.store.games),
                         "tournaments": len(self.server.store.tournaments), "time": time.time(),
                         "lobby_url": getattr(self.server, "public_url", ""),
                         "uploads": bool(self.server.store.uploads_dir),
                         "local": self.is_local_client(),
                         "organiser": self.is_organiser()})

    def api_pause(self, id: str) -> None:
        """POST /api/games/{id}/pause -> state
        Stops the clock and holds the bots, so a room can talk over a
        position.  The pause costs neither player any time.  No
        authentication: this is a classroom tool, like abort."""
        game = self._game(id)
        store = self.server.store
        with store.lock:
            game.pause()
            store.changed(game)
            self.log_message("game %s: paused", game.id)
            self._send_state(game, None)

    def api_resume(self, id: str) -> None:
        """POST /api/games/{id}/resume -> state
        Starts play again; the mover's clock runs from now."""
        game = self._game(id)
        store = self.server.store
        with store.lock:
            game.resume()
            store.changed(game)
            self.log_message("game %s: resumed", game.id)
            self._send_state(game, None)

    def api_upload_info(self) -> None:
        """GET /api/uploads -> what this server accepts.
        The browser asks before showing its file picker, so a server started
        without --accept-uploads simply never offers the tab.  The limits are
        published rather than hard-coded in the page, so an oversized
        submission is caught on the device instead of on the wire."""
        store = self.server.store
        langs = upload_languages()
        self._send_json({"enabled": bool(store.uploads_dir),
                         "languages": langs,
                         "accept": ",".join(l["extension"] for l in langs) + ",.zip",
                         "max_bytes": clients_mod.UPLOAD_MAX_BYTES,
                         "max_total_bytes": clients_mod.UPLOAD_MAX_TOTAL_BYTES,
                         "max_files": clients_mod.UPLOAD_MAX_FILES,
                         "multifile": True,
                         "entry_names": list(clients_mod.ENTRY_STEMS)})

    def api_upload(self) -> None:
        """POST /api/uploads?team=Team+A -> the strategy, in one of three shapes:

            ?filename=strategy.py      raw body: the file itself
            ?filename=bot.zip          raw body: an archive, unpacked here
            multipart/form-data        several files, each part named by its
                                       path inside the submission

        -> {"kind": "upload-team-a", "language": "Python", "files": 3,
            "file": "main.py", "names": [...], "available": true}

        `?entry=main.py` (or an `entry` field in the form) says which file
        starts the bot; without it the server works it out and says which one
        it chose.  The submission is written into the uploads folder with a
        *generated* manifest, so it becomes a client the server can build,
        launch and seat like any other.  The caller then enters a tournament
        with the returned `kind`.

        The server executes what is uploaded, so this is refused unless it was
        started with --accept-uploads."""
        store = self.server.store
        if not store.uploads_dir:
            raise ApiError(403, "this server does not accept uploaded strategies "
                                "(start it with --accept-uploads)")
        content_type = self.headers.get("Content-Type", "") or ""
        multipart = content_type.lower().startswith("multipart/form-data")
        filename = str(self.param("filename", "") or "")
        bundle = multipart or filename.lower().endswith(".zip")
        limit = (clients_mod.UPLOAD_MAX_TOTAL_BYTES if bundle
                 else clients_mod.UPLOAD_MAX_BYTES)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length header") from None
        if length <= 0:
            raise ApiError(400, "no file was sent")
        if length > limit:
            self._drain(length)
            raise ApiError(413, f"that is {length // 1024} KB; the limit is {limit // 1024} KB")
        data = self.rfile.read(length)
        team = str(self.param("team", "") or "")
        entry = str(self.param("entry", "") or "")

        try:
            if multipart:
                files, form_entry = _parse_multipart(data, content_type)
                if not files:
                    raise ApiError(400, "the form held no files")
                saved = save_bundle(store.uploads_dir, files, team, entry or form_entry)
            else:
                saved = save_upload(store.uploads_dir, filename, data, team, entry)
        except UploadTooLarge as exc:
            raise ApiError(413, str(exc)) from None
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        except OSError as exc:
            raise ApiError(500, f"could not save the strategy: {exc}") from None

        store.externals.scan()          # make it seatable now, not in ten seconds
        spec = store.externals.get(saved["kind"])
        self.log_message("upload: %s (%s, %d file%s) from %s as %s",
                         saved["file"], saved["language"], saved["files"],
                         "" if saved["files"] == 1 else "s",
                         self.client_address[0], saved["kind"])
        self._send_json({"kind": saved["kind"], "language": saved["language"],
                         "file": saved["file"], "files": saved["files"],
                         "names": saved["names"],
                         "available": bool(spec and spec.available),
                         "reason": spec.reason if spec else "the manifest could not be read"}, 201)

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
                # a paused game wakes nobody: a client told it was its turn
                # would move, be refused, and fall back on its safest card
                return game.status == STATUS_FINISHED or (
                    game.status == STATUS_PLAYING and not game.paused
                    and game.turn == seat.number)

            store.wait_for(game, ready, timeout)
            self._send_state(game, seat.number)

    def api_join(self, id: str) -> None:
        """POST /api/games/{id}/join {name, seat?, avatar?} -> {seat, token, state}
        avatar is 1..16 (see /avatars/avNN.png); omitted = picked from the name.
        A seat reserved by a tournament opens for the entrant's token (sent
        as X-Token or `token`) or for a joiner giving the reserved name.
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
                seat = game.join(name, seat_number, avatar=avatar, claim=self.token(game))
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
            store.pace_after_move(game)     # a tournament match plays at a watchable speed
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
        bots += [{"kind": k, "label": v.label, "language": v.language}
                 for k, v in sorted(store.externals.items()) if v.available]
        self._send_json({"bots": bots})

    def api_strategies(self) -> None:
        """GET /api/strategies -> {"clients": [...], "bots": [...], "problems": [...]}
        Every client folder that carries a client.json manifest (its language,
        the file to edit, the function to replace, how to run it and whether
        this machine can) and the bots the server can seat with the file each
        lives in.  Shown in the lobby so teams know where to start; adding a
        language means adding a folder, not changing this file."""
        store = self.server.store
        clients = [spec.to_dict() for spec in sorted(store.externals.values(),
                                                     key=lambda s: (s.language.lower(), s.kind))]
        bots = []
        for kind, (label, fn) in store.bots.items():
            try:
                rel = os.path.relpath(fn.__code__.co_filename, REPO_ROOT)
            except (AttributeError, ValueError):
                rel = "server/bots.py"
            bots.append({"kind": kind, "label": label, "file": rel,
                         "private": rel.startswith("private/")})
        # what each folder holds, strategy-wise: its own sample strategy, and
        # for a client that says server_bots the bots the server runs itself
        # (they are Python, so that is where a team would look for them).
        for spec, entry in zip(sorted(store.externals.values(), key=lambda s: (s.language.lower(), s.kind)), clients):
            note = entry["note"] or "the sample strategy"
            where = entry["edit"].split("(")[0].split("[")[0]
            entry["strategies"] = [{"name": "Sample strategy", "file": entry["file"],
                                    "note": f"in {where}(): {note}" if where else note}]
            if spec.server_bots:
                entry["strategies"] += [{"name": b["label"], "file": b["file"],
                                         "note": "server-run bot" + (", not distributed" if b["private"] else "")}
                                        for b in bots]
        self._send_json({"clients": clients, "bots": bots, "problems": store.externals.errors})

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
                seat = store.seat_external(game, seat_number, kind, avatar, str(name) if name else None, store.local_url)
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

    # ------------------------------------------------------------ tournaments

    def _tournament(self, tid: str) -> Tournament:
        t = self.server.store.get_tournament(tid)
        if t is None:
            raise ApiError(404, f"no tournament with id {tid.upper()}")
        return t

    def _send_tournament(self, t: Tournament, status: int = 200) -> None:
        """Purpose: send the whole bracket; with a token, `you` says what the
        caller should do next."""
        store = self.server.store
        with store.lock:
            viewer = t.entrant_for_token(self.token())
            self._send_json(t.to_dict(store.game_info, viewer), status)

    def api_list_tournaments(self) -> None:
        """GET /api/tournaments -> {"tournaments": [summary, ...]} newest first."""
        store = self.server.store
        with store.lock:
            ts = sorted(store.tournaments.values(), key=lambda t: t.created_at, reverse=True)
            self._send_json({"tournaments": [t.summary() for t in ts]})

    def api_create_tournament(self) -> None:
        """POST /api/tournaments {stones, cards, time_limit?, label?, bot_delay?} -> bracket (201)
        Every match of the tournament uses these settings.  bot_delay is the
        pause after each move, so a room can follow the play (default 1.5 s;
        0 plays a whole match in a blink)."""
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
        delay = self.float_param("bot_delay", 1.5, 0.0, 30.0)
        try:
            t = self.server.store.create_tournament(stones, cards, time_limit, label, delay)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        self.log_message("created tournament %s (s=%d k=%d clock=%gs)", t.id, stones, cards, time_limit)
        self._send_tournament(t, 201)

    def api_get_tournament(self, id: str) -> None:
        """GET /api/tournaments/{id}?since=V&timeout=S -> bracket
        With `since`, waits until the bracket's version exceeds V (any move
        in any of its games counts) or S seconds pass (default 25, max 60)."""
        t = self._tournament(id)
        store = self.server.store
        since = self.int_param("since", default=-1)
        timeout = self.float_param("timeout", 25.0, 0.0, MAX_LONG_POLL)
        with store.lock:
            if since >= 0:
                store.wait_for_tournament(t, lambda: t.version > since, timeout)
            self._send_tournament(t)

    def api_join_tournament(self, id: str) -> None:
        """POST /api/tournaments/{id}/join {name, avatar?, kind?} -> {entrant, token, tournament}
        kind: "human" (default; plays from the browser), "api" (a program that
        will sit down in each of its games over HTTP), or a bot kind from
        GET /api/bots (the server plays it).  Keep the token: it opens your
        reserved seat in every match."""
        t = self._tournament(id)
        store = self.server.store
        name = str(self.param("name", "") or "")
        kind = str(self.param("kind", "") or KIND_HUMAN).strip().lower()
        if kind not in (KIND_HUMAN, KIND_API) and kind not in store.bots and kind not in store.externals:
            raise ApiError(400, f"unknown kind '{kind}'; use human, api, or a bot from /api/bots")
        if kind in store.externals and not store.externals[kind].available:
            raise ApiError(400, store.externals[kind].reason)
        avatar = None
        if self.param("avatar") not in (None, ""):
            avatar = self.int_param("avatar", lo=1, hi=NUM_AVATARS)
        if kind not in (KIND_HUMAN, KIND_API) and not name.strip():
            name = store.bots[kind][0] if kind in store.bots else store.externals[kind].label
        with store.lock:
            try:
                entrant = t.add_entrant(name, avatar, kind)
            except GameError as exc:
                raise ApiError(409, str(exc)) from None
            store.tournament_changed(t)
            self.log_message("tournament %s: %s entered (%s)", t.id, entrant.name, kind)
            self._send_json({"entrant": entrant.to_dict(), "token": entrant.token,
                             "tournament": t.to_dict(store.game_info, entrant)})

    def api_leave_tournament(self, id: str) -> None:
        """POST /api/tournaments/{id}/leave (token, or {entrant} id for a bot) -> bracket
        Withdraws before the bracket starts.  Your own entry needs your token
        and can go from anywhere; taking anyone else out is the organiser's."""
        t = self._tournament(id)
        store = self.server.store
        with store.lock:
            entrant = t.entrant_for_token(self.token())
            if entrant is None and self.param("entrant") not in (None, ""):
                candidate = t.entrants.get(self.int_param("entrant", lo=1))
                if candidate is not None and candidate.is_bot:
                    # a bot and an uploaded strategy have no token to prove
                    # ownership, so the organiser's machine stands in for one
                    self.require_organiser("take an entry out of the bracket")
                    entrant = candidate
            if entrant is None:
                raise ApiError(401, "missing or unknown token")
            try:
                t.remove_entrant(entrant.id)
            except GameError as exc:
                raise ApiError(409, str(exc)) from None
            store.tournament_changed(t)
            self._send_tournament(t)

    def api_start_tournament(self, id: str) -> None:
        """POST /api/tournaments/{id}/start -> bracket.  Draws the bracket and
        creates the first games.  The organiser's machine only."""
        self.require_organiser("draw the bracket")
        t = self._tournament(id)
        try:
            self.server.store.start_tournament(t)
        except GameError as exc:
            raise ApiError(409, str(exc)) from None
        self.log_message("tournament %s started with %d entrants", t.id, len(t.entrants))
        self._send_tournament(t)

    def api_tournament_getstate(self, id: str) -> None:
        """GET /api/tournaments/{id}/getstate?timeout=S (token required)
        For programs: returns only when the caller has a game to sit down in
        (status "play", with the game id and seat), is out ("eliminated"),
        has won ("champion") or the tournament is over.  Waits at most
        `timeout` seconds (default 60) and otherwise answers with the current
        status ("open" or "waiting"); just call again."""
        t = self._tournament(id)
        store = self.server.store
        timeout = self.float_param("timeout", MAX_LONG_POLL, 0.0, MAX_LONG_POLL)
        with store.lock:
            entrant = t.entrant_for_token(self.token())
            if entrant is None:
                raise ApiError(401, "missing or unknown token; join the tournament first")

            def view() -> dict:
                return t.entrant_view(entrant, store.game_info)

            def ready() -> bool:
                v = view()
                return v["status"] in ("eliminated", "champion", "finished") or (
                    v["status"] == "play" and not v["claimed"])

            store.wait_for_tournament(t, ready, timeout)
            v = view()
            v["tournament"] = t.summary()
            self._send_json(v)

    def api_restart_tournament(self, id: str) -> None:
        """POST /api/tournaments/{id}/restart -> the new bracket (201)
        Draws a fresh tournament with the same settings and the same
        server-run entrants, and records it on the old one as `successor`.
        That field is what lets every other screen follow: a phone watching
        the old bracket sees it on its next poll and goes to the new one,
        instead of being left on a finished draw.  People who entered from a
        browser re-enter, since their seat token belongs to the old bracket.
        The organiser's machine only, like every other control."""
        self.require_organiser("restart a tournament")
        old = self._tournament(id)
        store = self.server.store
        if old.successor and store.get_tournament(old.successor):
            return self._send_tournament(store.get_tournament(old.successor), 200)
        label = re.sub(r"(\s*\(again\))+$", "", old.label or "") or f"Tournament {old.id}"
        fresh = store.create_tournament(old.stones, old.cards, old.time_limit,
                                        f"{label} (again)", old.bot_delay)
        carried = []
        with store.lock:
            for e in sorted(old.entrants.values(), key=lambda e: e.id):
                if e.kind == KIND_HUMAN:
                    continue                     # their token opens a seat in the old draw
                try:
                    fresh.add_entrant(e.name, e.avatar, e.kind)
                    carried.append(e.name)
                except GameError:
                    pass                         # a duplicate name just does not carry
            old.successor = fresh.id
            old._touch()
            store.tournament_changed(old)        # wake every screen on the old bracket
            store.tournament_changed(fresh)
        self.log_message("tournament %s restarted as %s (%d entrants carried)",
                         old.id, fresh.id, len(carried))
        self._send_tournament(fresh, 201)

    def api_play_match(self, id: str) -> None:
        """POST /api/tournaments/{id}/play {round, match} -> bracket
        Starts one match: creates its game, reserves the two seats and sits
        any server-run entrants down.  Matches never start themselves, so the
        organiser decides when each one begins.  409 if the match is not
        ready, is already decided, or another match is still being played.
        The organiser's machine only: a match begins when the room is ready
        for it, not when the quickest team on the page presses the button."""
        self.require_organiser("start a match")
        t = self._tournament(id)
        store = self.server.store
        round_number = self.int_param("round", lo=1)
        index = self.int_param("match", lo=0)
        try:
            game = store.start_match(t, round_number, index)
        except GameError as exc:
            raise ApiError(409, str(exc)) from None
        self.log_message("tournament %s: round %s match %s started as game %s",
                         t.id, round_number, index + 1, game.id)
        self._send_tournament(t)

    def api_walkover(self, id: str) -> None:
        """POST /api/tournaments/{id}/walkover {round, match, winner} -> bracket
        Hands an undecided match to entrant `winner` (its id); a game in
        progress is aborted.  For no-shows.  The organiser's machine only."""
        self.require_organiser("decide a match")
        t = self._tournament(id)
        round_number = self.int_param("round", lo=1)
        index = self.int_param("match", lo=0)
        winner = self.int_param("winner", lo=1)
        try:
            self.server.store.walkover(t, round_number, index, winner)
        except GameError as exc:
            raise ApiError(409, str(exc)) from None
        self.log_message("tournament %s: walkover in round %d match %d for entrant %d", t.id, round_number, index, winner)
        self._send_tournament(t)

    def api_abort_tournament(self, id: str) -> None:
        """POST /api/tournaments/{id}/abort {reason?} -> bracket.  The
        organiser's machine only."""
        self.require_organiser("abort the tournament")
        t = self._tournament(id)
        self.server.store.abort_tournament(t, str(self.param("reason", "") or "aborted by the architects")[:120])
        self.log_message("tournament %s aborted", t.id)
        self._send_tournament(t)

    # ------------------------------------------------------------ static files

    def _serve_static(self, path: str) -> None:
        """Purpose: serve the browser UI from web/.
        /                 -> index.html (lobby)
        /game/{id}        -> game.html (board; the page reads the id from the URL)
        /tournament/{id}  -> tournament.html (bracket)
        /<file>           -> web/<file>"""
        if path in ("", "/"):
            rel = "index.html"
        elif re.match(r"^/game/[A-Za-z0-9]+/?$", path):
            rel = "game.html"
        elif re.match(r"^/tournament/[A-Za-z0-9]+/?$", path):
            rel = "tournament.html"
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

    def __init__(self, addr, store: GameStore, quiet: bool = False, public_url: str = "",
                 organiser_addresses=None, open_controls: bool = False) -> None:
        super().__init__(addr, Handler)
        self.store = store
        self.quiet = quiet
        # Who may run the event (draw the bracket, start a match, call a
        # no-show, abort).  The machine the server runs on always may; these
        # two say who else does.
        self.organiser_addresses = set(organiser_addresses or ())
        self.open_controls = bool(open_controls)
        store.local_url = f"http://127.0.0.1:{self.server_address[1]}"   # for sample clients the server launches
        # the address other devices on the network should use (shown as a QR code)
        host = addr[0] if addr[0] not in ("", "0.0.0.0") else lan_ip()
        self.public_url = public_url or f"http://{host}:{self.server_address[1]}/"
        # Every address that means "the machine running this server".  A page
        # asks /api/health whether it is being viewed here, which is how the
        # organiser's screen shows the join QR and a team's phone does not.
        # Worked out once: lan_ip() opens a socket.
        self.local_addresses = {"127.0.0.1", "::1", lan_ip()}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                self.local_addresses.add(info[4][0])
        except OSError:
            pass


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
    parser.add_argument("--clients-dir", action="append", default=[], metavar="DIR",
                        help="extra folder of client manifests to offer as bots; repeatable "
                             "(clients/ is always scanned)")
    parser.add_argument("--accept-uploads", action="store_true",
                        help="let people send a strategy from their own device -- one file, "
                             "several, a folder or a .zip (the tournament page offers it) -- "
                             "and RUN it on this machine")
    parser.add_argument("--uploads-dir", default=os.path.join(REPO_ROOT, "uploads"), metavar="DIR",
                        help="where uploaded strategies are written (default: uploads/)")
    parser.add_argument("--controls-from", action="append", default=[], metavar="ADDR",
                        help="another machine that may run the event (draw the bracket, start "
                             "matches, call no-shows); repeatable. This machine always may")
    parser.add_argument("--open-controls", action="store_true",
                        help="let anyone who can reach the page start matches, the way it "
                             "worked before (default: only the machine running the server)")
    args = parser.parse_args(argv)

    results_dir = os.path.abspath(args.results) if args.results else None
    client_dirs = [os.path.abspath(os.path.expanduser(d)) for d in args.clients_dir]
    for d in client_dirs:
        if not os.path.isdir(d):
            print(f"[warn] --clients-dir {d} is not a folder; ignoring", file=sys.stderr)
    uploads_dir = None
    if args.accept_uploads:
        uploads_dir = os.path.abspath(os.path.expanduser(args.uploads_dir))
        try:
            os.makedirs(uploads_dir, exist_ok=True)
        except OSError as exc:
            print(f"[warn] cannot create {uploads_dir} ({exc}); uploads are off", file=sys.stderr)
            uploads_dir = None
    store = GameStore(results_dir, [d for d in client_dirs if os.path.isdir(d)], uploads_dir)
    try:
        server = CardNimServer((args.host, args.port), store, quiet=args.quiet,
                               public_url=args.public_url,
                               organiser_addresses=args.controls_from,
                               open_controls=args.open_controls)
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc}\n"
              f"another server is probably running; stop it or pass --port {args.port + 1}", file=sys.stderr)
        return 1

    shown_host = lan_ip() if args.host in ("0.0.0.0", "") else args.host
    print(f"Card Nim server listening on http://{shown_host}:{args.port}/  (lobby)")
    if results_dir:
        print(f"finished games are saved to {results_dir}")
    if uploads_dir:
        # Say this plainly: the whole point of the flag is that this machine
        # will execute code written by whoever can reach the page.
        print(f"accepting uploaded strategies into {uploads_dir}")
        print("  ! anyone who can reach this server can upload a program and have it RUN here.")
        print("  ! only use this on a network you trust, and stop the server when the round is over.")
    if args.open_controls:
        print("  ! anyone who can reach this server can draw brackets and start matches.")
    elif args.controls_from:
        print("the event is run from this machine and from " + ", ".join(args.controls_from))
    else:
        print("the event is run from this machine only: other devices enter and watch,"
              " but cannot start matches")
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
