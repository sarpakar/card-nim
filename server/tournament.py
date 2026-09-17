"""
Knockout (single-elimination) tournaments for Card Nim.

This module holds the bracket and nothing else: no games, no sockets.  The
server (cardnim_server.py) creates a Game for every match that is ready,
reserves its two seats for the entrants, and reports the result back here
with `record_result()`; the bracket then says which matches are ready next.

Lifecycle:  open  --(start)-->  running  -->  finished

  * While open, anyone joins by name: a person at the browser, a program
    that will talk to the API, or a bot the server plays itself.
  * start() shuffles the entrants into a bracket of the next power of two.
    Entrants without an opponent in round one get a bye.  Byes are spread
    over the bracket so no match is a bye against a bye.
  * Each match is one game.  Who moves first is decided by a coin toss when
    the game is created.  The winner moves on, the loser is out.  A game
    that ends with no winner (aborted) is played again.
  * The organiser can hand a match to one side without playing it
    (walkover) when the other side never shows up.
"""

from __future__ import annotations

import random
import secrets
import time
from typing import Callable, Optional

from engine import NUM_AVATARS, GameError, clean_text, new_game_id

MAX_ENTRANTS = 64
STATUS_OPEN = "open"          # taking entries
STATUS_RUNNING = "running"    # bracket started
STATUS_FINISHED = "finished"  # champion known, or aborted

KIND_HUMAN = "human"          # plays from the browser
KIND_API = "api"              # a program that will join each game over HTTP

GameInfo = Callable[[str], Optional[dict]]   # game id -> Game.summary() or None


class Entrant:
    """One name in the bracket.

    id:             1-based number, stable for the life of the tournament.
    name, avatar:   shown on the board and in the bracket.
    kind:           "human", "api", or a bot key the server can play itself
                    (see GET /api/bots).
    is_bot:         True unless kind is human or api.
    token:          the entrant's secret.  Sitting down in a match with this
                    token opens the reserved seat; it then also works as the
                    seat token for that game.
    wins:           games won (byes do not count).
    eliminated_in:  round number of the lost match, or None while still in.
    """

    def __init__(self, number: int, name: str, avatar: int, kind: str) -> None:
        self.id = number
        self.name = name
        self.avatar = avatar
        self.kind = kind
        self.is_bot = kind not in (KIND_HUMAN, KIND_API)
        self.token = secrets.token_urlsafe(18)
        self.wins = 0
        self.eliminated_in: Optional[int] = None
        self.joined_at = time.time()

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "avatar": self.avatar, "kind": self.kind,
                "bot": self.is_bot, "wins": self.wins, "eliminated_in": self.eliminated_in}


class Match:
    """One pairing.  `slots` holds entrant ids as they become known (None until
    the feeding match is decided); `seats` maps seat number -> entrant id once
    a game exists; `games` lists every game created for it (a replayed match
    has more than one); `game_id` is the one in progress or last played."""

    def __init__(self, round_number: int, index: int) -> None:
        self.round = round_number
        self.index = index
        self.slots: list[Optional[int]] = [None, None]
        self.bye = False
        self.seats: dict[int, int] = {}
        self.game_id: Optional[str] = None
        self.games: list[str] = []
        self.winner: Optional[int] = None
        self.walkover = False

    @property
    def ready(self) -> bool:
        return self.winner is None and not self.bye and all(s is not None for s in self.slots)

    def other(self, entrant_id: int) -> Optional[int]:
        for s in self.slots:
            if s is not None and s != entrant_id:
                return s
        return None


class Tournament:
    """A bracket of Card Nim games with the same s, k and clock."""

    def __init__(self, stones: int, cards: int, time_limit: float, label: str = "",
                 tournament_id: Optional[str] = None, bot_delay: float = 1.5) -> None:
        """Purpose: create an empty tournament that is taking entries.
        Inputs:  the game settings every match will use, a label, an optional
                 fixed id, and the pause (seconds) held after each move so
                 people can follow the games (default 1.5; 0 for no pause).
        Raises ValueError for out-of-range settings (same limits as Game)."""
        from engine import MAX_CARDS, MAX_STONES   # local import keeps the module header short
        if not (1 <= stones <= MAX_STONES):
            raise ValueError(f"stones must be between 1 and {MAX_STONES}")
        if not (1 <= cards <= MAX_CARDS):
            raise ValueError(f"cards must be between 1 and {MAX_CARDS}")
        if not (1 <= time_limit <= 24 * 3600):
            raise ValueError("time_limit must be between 1 second and 24 hours")
        self.id = tournament_id or new_game_id()
        self.label = clean_text(label, 60)
        self.stones = int(stones)
        self.cards = int(cards)
        self.time_limit = float(time_limit)
        self.bot_delay = max(0.0, min(30.0, float(bot_delay)))
        self.status = STATUS_OPEN
        self.entrants: dict[int, Entrant] = {}
        self.rounds: list[list[Match]] = []
        self.size = 0
        self.champion: Optional[int] = None
        self.successor: Optional[str] = None   # id of the bracket that replaced this one
        self.aborted = False
        self.reason = ""
        self.version = 0
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    def _touch(self) -> None:
        """Side effects: bumps version so long-pollers wake up."""
        self.version += 1

    @property
    def title(self) -> str:
        return self.label or f"Tournament {self.id}"

    # ------------------------------------------------------------------ entries

    def add_entrant(self, name: str, avatar: Optional[int] = None, kind: str = KIND_HUMAN) -> Entrant:
        """Purpose: enter the tournament.
        Inputs:  a display name, an optional avatar (1..16), and the kind:
                 human, api, or a bot key.
        Outputs: the Entrant (read .id and .token).
        Side effects: adds the entrant; bumps version.  A bot whose name is
                      already used gets a number appended; a person with a
                      used name is refused so seats can be claimed by name.
        Raises GameError when entries are closed, the bracket is full, the
        name is taken, or the avatar is out of range."""
        if self.status != STATUS_OPEN:
            raise GameError("entries are closed; the bracket has started")
        if len(self.entrants) >= MAX_ENTRANTS:
            raise GameError(f"the bracket is full ({MAX_ENTRANTS} entrants)")
        if avatar is not None and not (1 <= int(avatar) <= NUM_AVATARS):
            raise GameError(f"avatar must be between 1 and {NUM_AVATARS}")
        name = clean_text(name, 36) or "anonymous"
        kind = clean_text(kind, 40) or KIND_HUMAN
        taken = {e.name.lower() for e in self.entrants.values()}
        if name.lower() in taken:
            if kind in (KIND_HUMAN, KIND_API):
                raise GameError(f"the name {name} is already in the bracket; pick another")
            n = 2
            while f"{name} {n}".lower() in taken:
                n += 1
            name = f"{name} {n}"
        number = max(self.entrants, default=0) + 1
        entrant = Entrant(number, name, int(avatar) if avatar else self._pick_avatar(name), kind)
        self.entrants[number] = entrant
        self._touch()
        return entrant

    def _pick_avatar(self, name: str) -> int:
        """Purpose: a stable face for a name, avoiding faces already in use
        while there are spares.  Outputs: 1..NUM_AVATARS."""
        h = sum(ord(ch) * (i + 1) for i, ch in enumerate(name.lower()))
        pick = h % NUM_AVATARS + 1
        used = {e.avatar for e in self.entrants.values()}
        for _ in range(NUM_AVATARS):
            if pick not in used:
                break
            pick = pick % NUM_AVATARS + 1
        return pick

    def remove_entrant(self, entrant_id: int) -> Entrant:
        """Purpose: withdraw before the bracket starts.
        Raises GameError if the bracket has started or the id is unknown."""
        if self.status != STATUS_OPEN:
            raise GameError("the bracket has started; entrants can no longer withdraw")
        entrant = self.entrants.pop(entrant_id, None)
        if entrant is None:
            raise GameError("no such entrant")
        self._touch()
        return entrant

    def entrant_for_token(self, token: Optional[str]) -> Optional[Entrant]:
        """Purpose: identify an entrant by their secret.  Outputs: Entrant or None."""
        if not token:
            return None
        for e in self.entrants.values():
            if secrets.compare_digest(e.token, token):
                return e
        return None

    # ------------------------------------------------------------------ bracket

    def start(self, rng: Optional[random.Random] = None) -> None:
        """Purpose: close entries and draw the bracket.
        Inputs:  an optional random source (tests pass a seeded one).
        Side effects: status -> running; entrants are shuffled into round one;
                      byes are decided at once and their winners moved on.
        Raises GameError with fewer than two entrants or if already started."""
        if self.status != STATUS_OPEN:
            raise GameError("the bracket has already started")
        if len(self.entrants) < 2:
            raise GameError("a tournament needs at least two entrants")
        rng = rng or random
        order = list(self.entrants.values())
        rng.shuffle(order)
        n = len(order)
        size = 1
        while size < n:
            size *= 2
        self.size = size
        self.rounds = []
        matches = size // 2
        r = 1
        while matches >= 1:
            self.rounds.append([Match(r, i) for i in range(matches)])
            matches //= 2
            r += 1
        first = self.rounds[0]
        byes = size - n
        bye_at = {int(i * len(first) / byes) for i in range(byes)} if byes else set()
        it = iter(order)
        for i, match in enumerate(first):
            match.slots[0] = next(it).id
            if i in bye_at:
                match.bye = True
            else:
                match.slots[1] = next(it).id
        self.status = STATUS_RUNNING
        self.started_at = time.time()
        self._touch()
        for match in first:
            if match.bye:
                self._set_winner(match, match.slots[0], played=False)

    def round_name(self, round_number: int) -> str:
        """Purpose: "Final", "Semifinals", "Quarterfinals" or "Round n"."""
        left = len(self.rounds) - round_number
        if left == 0:
            return "Final"
        if left == 1:
            return "Semifinals"
        if left == 2:
            return "Quarterfinals"
        return f"Round {round_number}"

    def next_match(self, match: Match) -> Optional[Match]:
        """Purpose: the match the winner goes to.  Outputs: Match or None (final)."""
        if match.round >= len(self.rounds):
            return None
        return self.rounds[match.round][match.index // 2]

    def _set_winner(self, match: Match, entrant_id: int, played: bool = True) -> None:
        """Purpose: decide a match and move the winner on.
        Side effects: the loser is out; the winner fills the next match's
                      slot, or becomes champion after the final.  Bumps version."""
        match.winner = entrant_id
        winner = self.entrants[entrant_id]
        if played:
            winner.wins += 1
        loser_id = match.other(entrant_id)
        if loser_id is not None:
            self.entrants[loser_id].eliminated_in = match.round
        nxt = self.next_match(match)
        if nxt is not None:
            nxt.slots[match.index % 2] = entrant_id
        else:
            self.champion = entrant_id
            self.status = STATUS_FINISHED
            self.finished_at = time.time()
            self.reason = f"{winner.name} won the final"
        self._touch()

    def matches_needing_games(self) -> list[Match]:
        """Purpose: the matches whose two entrants are known and that have no
        game in progress.  The server creates one game per entry."""
        if self.status != STATUS_RUNNING:
            return []
        return [m for r in self.rounds for m in r if m.ready and m.game_id is None]

    def attach_game(self, match: Match, game_id: str, seats: dict[int, int]) -> None:
        """Purpose: remember the game created for a match and who sits where.
        Inputs:  the match, the game id, {seat number: entrant id}."""
        match.game_id = game_id
        match.games.append(game_id)
        match.seats = dict(seats)
        self._touch()

    def detach_game(self, match: Match) -> None:
        """Purpose: forget a game that ended with no winner so the match is
        played again.  The old id stays in match.games."""
        match.game_id = None
        match.seats = {}
        self._touch()

    def match_for_game(self, game_id: str) -> Optional[Match]:
        """Purpose: find the match a game belongs to.  Outputs: Match or None."""
        for r in self.rounds:
            for m in r:
                if game_id in m.games:
                    return m
        return None

    def record_result(self, game_id: str, winner_seat: int) -> Optional[Match]:
        """Purpose: apply a finished game to the bracket.
        Inputs:  the game id and the winning seat (1 or 2).
        Outputs: the decided Match, or None if the game is not the current
                 game of an undecided match (already handled, or replayed).
        Side effects: as _set_winner."""
        if self.status != STATUS_RUNNING:
            return None
        match = self.match_for_game(game_id)
        if match is None or match.winner is not None or match.game_id != game_id:
            return None
        entrant_id = match.seats.get(winner_seat)
        if entrant_id is None:
            return None
        self._set_winner(match, entrant_id)
        return match

    def walkover(self, round_number: int, index: int, winner_id: int) -> Match:
        """Purpose: give an undecided match to one side without playing it.
        Inputs:  round number (1-based), match index (0-based), entrant id.
        Outputs: the Match (the server aborts its game, if any).
        Raises GameError if the match is unknown, decided, or the entrant is
        not in it."""
        if self.status != STATUS_RUNNING:
            raise GameError("the tournament is not running")
        try:
            match = self.rounds[round_number - 1][index]
        except IndexError:
            raise GameError("no such match") from None
        if match.winner is not None:
            raise GameError("that match is already decided")
        if winner_id not in match.slots:
            raise GameError("that entrant is not in this match")
        if any(s is None for s in match.slots):
            raise GameError("the other side of this match is not known yet")
        match.walkover = True
        self._set_winner(match, winner_id)
        return match

    def abort(self, reason: str = "") -> None:
        """Purpose: end the tournament with no champion.
        Side effects: status -> finished, aborted flag set."""
        if self.status == STATUS_FINISHED:
            return
        self.status = STATUS_FINISHED
        self.aborted = True
        self.reason = reason or "tournament aborted"
        self.finished_at = time.time()
        self._touch()

    # ------------------------------------------------------------------ views

    def current_match(self, entrant: Entrant) -> Optional[Match]:
        """Purpose: the undecided match an entrant is in, if any."""
        for r in reversed(self.rounds):
            for m in r:
                if entrant.id in m.slots and m.winner is None:
                    return m
        return None

    def entrant_view(self, entrant: Entrant, game_info: Optional[GameInfo] = None) -> dict:
        """Purpose: what one entrant needs to know: whether to wait, which game
        to sit down in (and at which seat), or that they are out or champion.
        Inputs:  the entrant and a lookup for game summaries.
        Outputs: a dict with status open | waiting | play | eliminated |
                 champion | finished, plus round, game, seat, opponent."""
        view = {"entrant": entrant.to_dict(), "status": "open", "round": None, "round_name": None,
                "match": None, "game": None, "seat": None, "opponent": None, "claimed": False,
                "lost_to": None}
        if self.status == STATUS_OPEN:
            return view
        if self.champion == entrant.id:
            view["status"] = "champion"
            return view
        if entrant.eliminated_in is not None:
            view["status"] = "eliminated"
            view["round"] = entrant.eliminated_in
            view["round_name"] = self.round_name(entrant.eliminated_in)
            for m in self.rounds[entrant.eliminated_in - 1]:
                if entrant.id in m.slots and m.winner is not None:
                    view["match"] = m.index
                    view["lost_to"] = self.entrants[m.winner].name
            return view
        if self.status == STATUS_FINISHED:
            view["status"] = "finished"
            return view
        match = self.current_match(entrant)
        if match is None:
            view["status"] = "waiting"
            return view
        view["round"] = match.round
        view["round_name"] = self.round_name(match.round)
        view["match"] = match.index
        opponent = match.other(entrant.id)
        view["opponent"] = self.entrants[opponent].name if opponent is not None else None
        if match.game_id is None:
            view["status"] = "waiting"
            return view
        view["status"] = "play"
        view["game"] = match.game_id
        for seat, eid in match.seats.items():
            if eid == entrant.id:
                view["seat"] = seat
        info = game_info(match.game_id) if game_info else None
        if info and view["seat"]:
            view["claimed"] = bool(info.get("occupied", [False, False])[view["seat"] - 1])
        return view

    def _match_dict(self, match: Match, game_info: Optional[GameInfo]) -> dict:
        info = game_info(match.game_id) if (game_info and match.game_id) else None
        if match.bye:
            status = "bye"
        elif match.winner is not None:
            status = "done"
        elif any(s is None for s in match.slots):
            status = "pending"
        elif info is None:
            status = "ready"
        elif info["status"] == "finished":
            status = "ready"          # no winner: a new game is on its way
        else:
            status = info["status"]   # waiting | playing
        return {
            "round": match.round, "index": match.index, "slots": list(match.slots), "bye": match.bye,
            "seats": [match.seats.get(1), match.seats.get(2)] if match.seats else None,
            "game": match.game_id, "games": list(match.games), "winner": match.winner,
            "walkover": match.walkover, "status": status, "game_state": info,
        }

    def summary(self) -> dict:
        """Purpose: the short form for the lobby list."""
        return {
            "id": self.id, "label": self.label, "status": self.status, "aborted": self.aborted,
            "successor": self.successor,
            "stones": self.stones, "cards": self.cards, "time_limit": self.time_limit,
            "entrants": len(self.entrants), "rounds": len(self.rounds),
            "champion": self.entrants[self.champion].name if self.champion else None,
            "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
            "version": self.version,
        }

    def to_dict(self, game_info: Optional[GameInfo] = None, viewer: Optional[Entrant] = None) -> dict:
        """Purpose: the whole bracket for the page and the API.
        Inputs:  a lookup for game summaries (embedded per match) and the
                 entrant the caller is, if they sent a token.
        Outputs: a JSON-serialisable dict."""
        d = self.summary()
        d.update({
            "reason": self.reason, "bot_delay": self.bot_delay, "size": self.size,
            "entrants": [e.to_dict() for e in sorted(self.entrants.values(), key=lambda e: e.id)],
            "rounds": [{"number": r + 1, "name": self.round_name(r + 1),
                        "matches": [self._match_dict(m, game_info) for m in matches]}
                       for r, matches in enumerate(self.rounds)],
            "champion": self.champion,
            "champion_name": self.entrants[self.champion].name if self.champion else None,
        })
        if viewer is not None:
            d["you"] = self.entrant_view(viewer, game_info)
        return d
