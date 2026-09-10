"""
Card Nim game engine.

This module knows the rules of Card Nim and nothing else: no sockets, no HTTP,
no HTML.  The web server (cardnim_server.py) wraps a Game in an API; the tests
(tests/test_engine.py) drive it directly.

Rules (s and k are announced on competition day):

  * Two players share a pile of s stones (s <= 500).
  * Each player holds cards 1, 2, ..., k (k <= 200), one of each.
  * Players alternate.  On a turn you play one card from your hand and remove
    exactly that many stones.  The card is gone for the rest of the game.
  * If your card matches the number of stones left exactly, you win.
  * If your card exceeds the number of stones left, you lose.
  * If every card in your hand exceeds the number of stones left, you lose
    automatically (the engine detects this the moment it becomes your turn).
  * Each player has a chess-style clock.  Your clock runs only while it is your
    turn.  If it reaches zero, you lose.

Every mutation bumps `Game.version`, which the server uses for long-polling.
All time arguments are seconds from a monotonic clock; the caller passes `now`
so that tests can use a fake clock.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def clean_text(value, limit: int) -> str:
    """Purpose: make user-supplied text safe for logs and the one-line text
    format.  Inputs: any value, maximum length.  Outputs: a single line with
    control characters removed and runs of whitespace collapsed, cut to
    `limit` characters.  Side effects: none."""
    text = "" if value is None else str(value)
    text = _CONTROL_CHARS.sub(" ", text)
    return " ".join(text.split())[:limit]

MAX_STONES = 500
MAX_CARDS = 200
NUM_AVATARS = 16            # server/web/avatars/av01.png .. av16.png
DEFAULT_TIME_LIMIT = 120.0  # seconds per player for the whole game

STATUS_WAITING = "waiting"    # fewer than two players seated
STATUS_PLAYING = "playing"
STATUS_FINISHED = "finished"

# Characters used for short, unambiguous game ids (no 0/O, 1/I/L).
_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class GameError(Exception):
    """Raised for requests that are well formed but not allowed right now
    (seat already taken, game already over, ...).  The server maps it to
    HTTP 409."""


class IllegalMove(GameError):
    """Raised when a move is rejected: not your turn, card not in hand, ...
    The game state is unchanged and the mover's clock keeps running."""


def new_game_id(length: int = 4) -> str:
    """Purpose: produce a short, shareable game id such as "K7PX".
    Inputs:  length of the id.
    Outputs: a string drawn from an alphabet without look-alike characters.
    Side effects: none (uses the secrets module for randomness)."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


@dataclass
class Seat:
    """One side of the table.

    number:          1 or 2.  Seat 1 always moves first.
    name:            display name given at join time ("" while the seat is open).
    token:           secret credential returned at join time; every move must
                     carry it.  None while the seat is open.
    cards:           the cards still in hand.
    time_remaining:  seconds left on this player's clock, as of the last time the
                     clock was stopped (i.e. not counting the current turn).
    avatar:          1..NUM_AVATARS, the picture shown for this player (0 = none).
    is_bot:          True when the server itself plays this seat.
    """

    number: int
    name: str = ""
    token: Optional[str] = None
    cards: set[int] = field(default_factory=set)
    time_remaining: float = DEFAULT_TIME_LIMIT
    avatar: int = 0
    is_bot: bool = False

    @property
    def occupied(self) -> bool:
        return self.token is not None


@dataclass
class Move:
    """One accepted move, kept in Game.moves for the log and the observer UI.

    number:        1-based index in the game.
    seat:          who played (1 or 2).
    card:          the card played.
    stones_before: stones on the table before the move.
    stones_after:  stones after the move.  Negative if the card exceeded the
                   pile (an "overdraw", which loses on the spot).
    elapsed:       wall-clock seconds this player spent thinking, measured on
                   the server from the start of the turn to acceptance.
    at:            wall-clock timestamp (time.time()) when accepted.
    """

    number: int
    seat: int
    card: int
    stones_before: int
    stones_after: int
    elapsed: float
    at: float

    @property
    def overdraw(self) -> bool:
        return self.stones_after < 0


class Game:
    """A single Card Nim match: two seats, a pile of stones, two clocks, a log.

    Lifecycle:  waiting  --(second player joins)-->  playing  -->  finished
    """

    def __init__(
        self,
        stones: int,
        cards: int,
        time_limit: float = DEFAULT_TIME_LIMIT,
        label: str = "",
        game_id: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Purpose: create a new game in the `waiting` state.
        Inputs:  stones (1..500), cards k (1..200), time_limit in seconds per
                 player, an optional human label, an optional fixed id, and an
                 optional monotonic clock function (tests inject a fake one).
        Outputs: a Game.
        Side effects: none beyond building the object.
        Raises ValueError for out-of-range parameters."""
        if not (1 <= stones <= MAX_STONES):
            raise ValueError(f"stones must be between 1 and {MAX_STONES}")
        if not (1 <= cards <= MAX_CARDS):
            raise ValueError(f"cards must be between 1 and {MAX_CARDS}")
        if not (1 <= time_limit <= 24 * 3600):
            raise ValueError("time_limit must be between 1 second and 24 hours")

        self.id = game_id or new_game_id()
        self.label = clean_text(label, 60)
        self.initial_stones = int(stones)
        self.num_cards = int(cards)
        self.time_limit = float(time_limit)
        self.stones = int(stones)
        self.status = STATUS_WAITING
        self.turn = 1                       # seat number whose move it is
        self.winner: Optional[int] = None   # 1, 2, or None (not over / aborted)
        self.reason = ""                    # human-readable explanation of the result
        self.moves: list[Move] = []
        self.version = 0                    # bumps on every change; used for long-poll
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self._clock = clock
        self._turn_started: Optional[float] = None  # monotonic time the current turn began
        self.seats = {
            1: Seat(1, cards=set(range(1, cards + 1)), time_remaining=self.time_limit),
            2: Seat(2, cards=set(range(1, cards + 1)), time_remaining=self.time_limit),
        }

    # ------------------------------------------------------------------ helpers

    def _touch(self) -> None:
        """Purpose: mark the state as changed so long-pollers wake up.
        Side effects: increments self.version."""
        self.version += 1

    def other(self, seat: int) -> int:
        """Purpose: the opponent's seat number.  Inputs: 1 or 2.  Outputs: 2 or 1."""
        return 2 if seat == 1 else 1

    def seat_for_token(self, token: Optional[str]) -> Optional[Seat]:
        """Purpose: find which seat a credential belongs to.
        Inputs:  a token string (or None).
        Outputs: the Seat, or None if the token matches nobody."""
        if not token:
            return None
        for seat in self.seats.values():
            if seat.token is not None and secrets.compare_digest(seat.token, token):
                return seat
        return None

    def playable_cards(self, seat: int) -> list[int]:
        """Purpose: the cards this player could legally play without overdrawing.
        Inputs:  seat number.  Outputs: sorted list of cards <= stones."""
        return sorted(c for c in self.seats[seat].cards if c <= self.stones)

    def elapsed_this_turn(self, now: float) -> float:
        """Purpose: seconds the player on turn has been thinking so far.
        Inputs: current monotonic time.  Outputs: seconds (0 if not playing)."""
        if self.status != STATUS_PLAYING or self._turn_started is None:
            return 0.0
        return max(0.0, now - self._turn_started)

    def time_remaining(self, seat: int, now: float) -> float:
        """Purpose: live clock reading for a seat.
        Inputs:  seat number and current monotonic time.
        Outputs: seconds left, never below zero.  Counts the current turn
                 against the player on turn."""
        remaining = self.seats[seat].time_remaining
        if self.status == STATUS_PLAYING and seat == self.turn:
            remaining -= self.elapsed_this_turn(now)
        return max(0.0, remaining)

    # ------------------------------------------------------------------ joining

    def join(self, name: str, seat_number: Optional[int] = None, now: Optional[float] = None,
             avatar: Optional[int] = None, is_bot: bool = False) -> Seat:
        """Purpose: seat a player and hand them a token.
        Inputs:  display name; optional seat preference (1 or 2); current time;
                 optional avatar number (1..NUM_AVATARS).  Without one, an
                 avatar is picked from the name so the same name always gets
                 the same face, avoiding the opponent's.
        Outputs: the Seat (read .number, .token and .avatar).
        Side effects: fills the seat; when both seats are filled the game starts
                      and seat 1's clock begins.
        Raises GameError if the game is over, the requested seat is taken, the
        table is full, or the avatar number is out of range."""
        now = self._clock() if now is None else now
        if self.status == STATUS_FINISHED:
            raise GameError("this game is over; create a new one")
        name = clean_text(name, 40) or "anonymous"
        if avatar is not None and not (1 <= int(avatar) <= NUM_AVATARS):
            raise GameError(f"avatar must be between 1 and {NUM_AVATARS}")
        if seat_number is None:
            free = [s for s in (1, 2) if not self.seats[s].occupied]
            if not free:
                raise GameError("both seats are taken")
            seat_number = free[0]
        if seat_number not in (1, 2):
            raise GameError("seat must be 1 or 2")
        seat = self.seats[seat_number]
        if seat.occupied:
            raise GameError(f"seat {seat_number} is already taken by {seat.name}")
        seat.name = name
        seat.token = secrets.token_urlsafe(18)
        seat.avatar = int(avatar) if avatar is not None else self._pick_avatar(name, seat_number)
        seat.is_bot = bool(is_bot)
        self._touch()
        if all(s.occupied for s in self.seats.values()):
            self._start(now)
        return seat

    def _pick_avatar(self, name: str, seat_number: int) -> int:
        """Purpose: a stable avatar for a name, different from the opponent's.
        Inputs: the (cleaned) name and the seat being taken.  Outputs: 1..NUM_AVATARS."""
        taken = self.seats[self.other(seat_number)].avatar
        h = sum(ord(ch) * (i + 1) for i, ch in enumerate(name.lower()))
        pick = h % NUM_AVATARS + 1
        if pick == taken:
            pick = pick % NUM_AVATARS + 1
        return pick

    def _start(self, now: float) -> None:
        """Purpose: begin play once both seats are filled.
        Side effects: status -> playing, turn -> 1, seat 1's clock starts."""
        self.status = STATUS_PLAYING
        self.turn = 1
        self._turn_started = now
        self._touch()
        # Degenerate case (k*(k+1)/2 <= s is not allowed by the professor, but
        # a first player with no playable card would lose at once).
        self._check_stranded(now)

    # ------------------------------------------------------------------ playing

    def play(self, seat_number: int, card: int, now: Optional[float] = None) -> Move:
        """Purpose: apply one move.
        Inputs:  the mover's seat, the card, the current monotonic time.
        Outputs: the Move record that was appended to the log.
        Side effects: removes the card from the hand, updates the pile, charges
                      the mover's clock, may finish the game, switches the turn,
                      and starts the opponent's clock.  Bumps version.
        Raises IllegalMove (state unchanged) if the game is not in progress, it
        is not this seat's turn, or the card is not in the hand.  A card larger
        than the pile is *accepted* and loses immediately, as the rules say."""
        now = self._clock() if now is None else now
        self.check_timeout(now)
        if self.status != STATUS_PLAYING:
            raise IllegalMove("the game is not in progress")
        if seat_number != self.turn:
            raise IllegalMove("it is not your turn")
        seat = self.seats[seat_number]
        try:
            card = int(card)
        except (TypeError, ValueError):
            raise IllegalMove("card must be an integer") from None
        if card not in seat.cards:
            raise IllegalMove(f"card {card} is not in your hand")

        elapsed = self.elapsed_this_turn(now)
        seat.time_remaining = max(0.0, seat.time_remaining - elapsed)
        seat.cards.discard(card)
        before = self.stones
        after = before - card
        move = Move(len(self.moves) + 1, seat_number, card, before, after, elapsed, time.time())
        self.moves.append(move)

        if after == 0:
            self.stones = 0
            self._finish(seat_number, f"{seat.name} took the last {card} stone{'s' if card != 1 else ''}")
        elif after < 0:
            # Overdraw: the pile is untouched, the mover loses.
            self._finish(self.other(seat_number),
                         f"{seat.name} played {card} with only {before} stone{'s' if before != 1 else ''} left")
        else:
            self.stones = after
            self.turn = self.other(seat_number)
            self._turn_started = now
            self._touch()
            self._check_stranded(now)
        return move

    def _check_stranded(self, now: float) -> None:
        """Purpose: apply the automatic-loss rule at the start of a turn.
        Side effects: finishes the game if the player on turn has no card that
                      fits the pile."""
        if self.status != STATUS_PLAYING:
            return
        if not self.playable_cards(self.turn):
            loser = self.seats[self.turn]
            self._finish(self.other(self.turn),
                         f"{loser.name} has no card small enough for {self.stones} stone{'s' if self.stones != 1 else ''}")

    def check_timeout(self, now: Optional[float] = None) -> bool:
        """Purpose: enforce the clock.  Called by the server on every request
        and from a background ticker so a stalled bot still loses on time.
        Inputs:  current monotonic time.
        Outputs: True if this call ended the game on time, else False.
        Side effects: may finish the game with reason "ran out of time"."""
        now = self._clock() if now is None else now
        if self.status != STATUS_PLAYING:
            return False
        if self.time_remaining(self.turn, now) <= 0.0:
            loser = self.seats[self.turn]
            loser.time_remaining = 0.0
            self._finish(self.other(self.turn), f"{loser.name} ran out of time")
            return True
        return False

    def leave(self, seat_number: int) -> None:
        """Purpose: let a player give up their seat.
        Inputs:  the seat number (already authenticated by the caller).
        Side effects: before the game starts, the seat becomes open again and
                      its token stops working; during play the player resigns
                      and the opponent wins.  Bumps version.
        Raises GameError if the game is already over."""
        if self.status == STATUS_FINISHED:
            raise GameError("the game is over")
        seat = self.seats[seat_number]
        if self.status == STATUS_WAITING:
            seat.name = ""
            seat.token = None
            seat.avatar = 0
            seat.is_bot = False
            self._touch()
        else:
            self._finish(self.other(seat_number), f"{seat.name} resigned")

    def abort(self, reason: str = "aborted by the architects") -> None:
        """Purpose: stop a game with no winner (wrong parameters, crashed bot...).
        Side effects: status -> finished, winner None."""
        if self.status == STATUS_FINISHED:
            return
        self._finish(None, reason)

    def _finish(self, winner: Optional[int], reason: str) -> None:
        """Purpose: record the result and freeze the clocks.
        Inputs:  winning seat (or None) and a human-readable reason.
        Side effects: status -> finished, clocks stop, version bump."""
        if self.status == STATUS_PLAYING and self._turn_started is not None:
            # Freeze the clock of whoever was on turn.
            now = self._clock()
            seat = self.seats[self.turn]
            seat.time_remaining = max(0.0, seat.time_remaining - self.elapsed_this_turn(now))
        self._turn_started = None
        self.status = STATUS_FINISHED
        self.winner = winner
        self.reason = reason
        self.finished_at = time.time()
        self._touch()

    # ------------------------------------------------------------------ views

    def summary(self, now: Optional[float] = None) -> dict:
        """Purpose: the short form used by the lobby list.
        Outputs: a JSON-serialisable dict."""
        now = self._clock() if now is None else now
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "stones": self.stones,
            "initial_stones": self.initial_stones,
            "cards": self.num_cards,
            "time_limit": self.time_limit,
            "players": [self.seats[1].name or None, self.seats[2].name or None],
            "avatars": [self.seats[1].avatar, self.seats[2].avatar],
            "bots": [self.seats[1].is_bot, self.seats[2].is_bot],
            "turn": self.turn if self.status == STATUS_PLAYING else None,
            "winner": self.winner,
            "moves": len(self.moves),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "version": self.version,
        }

    def to_dict(self, now: Optional[float] = None, viewer_seat: Optional[int] = None) -> dict:
        """Purpose: the full state shown to players, bots and observers.
        Inputs:  current monotonic time; the viewer's seat if they hold one.
        Outputs: a JSON-serialisable dict.  Hands are public information in
                 Card Nim, so both hands are always included.
        Side effects: none."""
        now = self._clock() if now is None else now
        players = []
        for n in (1, 2):
            seat = self.seats[n]
            players.append({
                "seat": n,
                "name": seat.name or None,
                "occupied": seat.occupied,
                "avatar": seat.avatar,
                "bot": seat.is_bot,
                "cards": sorted(seat.cards),
                "playable": self.playable_cards(n) if self.status == STATUS_PLAYING else [],
                "time_remaining": round(self.time_remaining(n, now), 3),
            })
        last = self.moves[-1] if self.moves else None
        state = {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "version": self.version,
            "initial_stones": self.initial_stones,
            "stones": self.stones,
            "num_cards": self.num_cards,
            "time_limit": self.time_limit,
            "turn": self.turn if self.status == STATUS_PLAYING else None,
            "players": players,
            "moves": [
                {
                    "number": m.number, "seat": m.seat, "card": m.card,
                    "stones_before": m.stones_before, "stones_after": m.stones_after,
                    "elapsed": round(m.elapsed, 3), "overdraw": m.overdraw, "at": m.at,
                }
                for m in self.moves
            ],
            "last_move": None if last is None else {"seat": last.seat, "card": last.card},
            "winner": self.winner,
            "reason": self.reason,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "you": viewer_seat,
            "your_turn": bool(viewer_seat and self.status == STATUS_PLAYING and self.turn == viewer_seat),
        }
        return state

    def to_text(self, now: Optional[float] = None, viewer_seat: Optional[int] = None) -> str:
        """Purpose: a whitespace-only rendering of the state for bots written in
        languages without a handy JSON parser (C++, shell, ...).  One `key
        value...` pair per line; see docs/API.md for the exact keys.
        Inputs/Outputs: as to_dict, but returns a string."""
        now = self._clock() if now is None else now
        me = viewer_seat or 1
        opp = self.other(me)
        last = self.moves[-1] if self.moves else None
        lines = [
            f"status {self.status}",
            f"you {viewer_seat or 0}",
            f"turn {self.turn if self.status == STATUS_PLAYING else 0}",
            f"your_turn {1 if (viewer_seat and self.status == STATUS_PLAYING and self.turn == viewer_seat) else 0}",
            f"stones {self.stones}",
            f"initial_stones {self.initial_stones}",
            f"num_cards {self.num_cards}",
            "your_cards " + " ".join(map(str, sorted(self.seats[me].cards))),
            "opp_cards " + " ".join(map(str, sorted(self.seats[opp].cards))),
            f"your_time {self.time_remaining(me, now):.3f}",
            f"opp_time {self.time_remaining(opp, now):.3f}",
            f"last_move {last.seat if last else 0} {last.card if last else 0}",
            f"winner {self.winner or 0}",
            f"reason {self.reason or '-'}",
            f"version {self.version}",
        ]
        return "\n".join(lines) + "\n"
