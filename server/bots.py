"""
Bots the server can seat by itself, so a table can be filled from the browser
without opening a terminal.

Two deliberately simple bots ship with the server: `greedy` and `random`.
If a file `private/server_bots.py` exists next to the repository root it is
loaded too; it must define `BOTS = {"key": ("Label", function)}`.  That folder
is not distributed, which lets the architects keep a stronger demo bot for
themselves.

A bot function has the signature  fn(stones, my_cards, opp_cards) -> card
(lists are sorted).  It runs outside the game lock, so it may take time; the
move is then applied under the lock like any other move, and it is validated
the same way.
"""

from __future__ import annotations

import importlib.util
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from engine import STATUS_FINISHED, STATUS_PLAYING, IllegalMove

BotFn = Callable[[int, list, list], int]


def random_bot(stones: int, my_cards: list, opp_cards: list) -> int:
    """Wins if it can, otherwise plays a random card that fits."""
    if stones in my_cards:
        return stones
    fitting = [c for c in my_cards if c <= stones]
    return random.choice(fitting) if fitting else (min(my_cards) if my_cards else 1)


def greedy_bot(stones: int, my_cards: list, opp_cards: list) -> int:
    """Wins if it can; never hands the opponent an exact match if avoidable;
    otherwise plays the smallest fitting card to keep options open."""
    if stones in my_cards:
        return stones
    fitting = sorted(c for c in my_cards if c <= stones)
    if not fitting:
        return min(my_cards) if my_cards else 1
    safe = [c for c in fitting if (stones - c) not in opp_cards]
    return min(safe) if safe else min(fitting)


BUILTIN: dict[str, tuple[str, BotFn]] = {
    "greedy": ("Greedy bot", greedy_bot),
    "random": ("Random bot", random_bot),
}


def load_private(root: str) -> dict:
    """Purpose: pick up the architects' own bots from private/server_bots.py.
    Inputs: the repository root.  Outputs: {key: (label, fn)} or {}.
    Side effects: imports that file if it exists; a broken file is reported
    on stderr and ignored."""
    path = os.path.join(root, "private", "server_bots.py")
    if not os.path.isfile(path):
        return {}
    try:
        spec = importlib.util.spec_from_file_location("private_server_bots", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        bots = getattr(module, "BOTS", {})
        return {str(k): (str(v[0]), v[1]) for k, v in bots.items() if callable(v[1])}
    except Exception as exc:  # noqa: BLE001 - a private add-on must never stop the server
        print(f"[warn] could not load {path}: {exc}", file=sys.stderr)
        return {}


def registry(root: str) -> dict[str, tuple[str, BotFn]]:
    """Purpose: every bot the server can seat: built-ins plus private ones."""
    bots = dict(BUILTIN)
    bots.update(load_private(root))
    return bots


class BotRunner(threading.Thread):
    """Plays one seat of one game until the game is over.

    Waits on the game's condition until it is the bot's turn, thinks outside
    the lock, sleeps `delay` seconds so people can follow the moves, then
    plays under the lock.  A strategy that raises or returns an illegal card
    falls back to the smallest fitting card, so a bug never forfeits on time.
    """

    def __init__(self, store, game, seat: int, fn: BotFn, delay: float = 0.8) -> None:
        super().__init__(name=f"bot-{game.id}-{seat}", daemon=True)
        self.store = store
        self.game = game
        self.seat = seat
        self.fn = fn
        self.delay = max(0.0, float(delay))
        self.token = game.seats[seat].token      # if the seat changes hands, this bot is done

    def _gone(self) -> bool:
        return self.game.seats[self.seat].token != self.token

    def _my_turn(self) -> bool:
        return self.game.status == STATUS_PLAYING and self.game.turn == self.seat and not self._gone()

    def run(self) -> None:
        game, store, seat = self.game, self.store, self.seat
        other = game.other(seat)
        while True:
            with store.lock:
                store.wait_for(game, lambda: game.status == STATUS_FINISHED or self._gone() or self._my_turn(), 60)
                if game.status == STATUS_FINISHED or self._gone():
                    return
                if not self._my_turn():
                    continue
                stones = game.stones
                mine = sorted(game.seats[seat].cards)
                theirs = sorted(game.seats[other].cards)
            try:
                card = int(self.fn(stones, mine, theirs))
            except Exception:  # noqa: BLE001
                fitting = [c for c in mine if c <= stones]
                card = min(fitting) if fitting else (mine[0] if mine else 1)
            if self.delay:
                time.sleep(self.delay)
            with store.lock:
                if not self._my_turn():
                    continue
                try:
                    game.play(seat, card)
                except IllegalMove:
                    fitting = [c for c in game.seats[seat].cards if c <= game.stones]
                    if fitting:
                        try:
                            game.play(seat, min(fitting))
                        except IllegalMove:
                            pass
                store.changed(game)


# ============================================================ sample clients as bots
#
# The three sample clients under clients/ can also be seated from the browser:
# the server starts the program as a child process pointed at itself, and the
# program joins the seat over HTTP like any team's bot would.  This is exactly
# what a team runs from a terminal, so it doubles as a smoke test of the
# hand-out.

def _compiler() -> Optional[str]:
    return shutil.which("g++") or shutil.which("clang++")


def _tool_works(path: Optional[str], *args: str) -> bool:
    """Purpose: true if a tool exists and really runs.  macOS ships stub
    `java`/`javac` binaries that exist but only print an error, so finding
    the file is not enough."""
    if not path:
        return False
    try:
        return subprocess.run([path, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _child_log(root: str, kind: str) -> str:
    """Purpose: where a launched client's output goes (results/clients/)."""
    folder = os.path.join(root, "results", "clients")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{kind}-{int(time.time())}.log")


def _spawn(cmd: list, log_path: str) -> subprocess.Popen:
    """Purpose: start a client with stdout and stderr appended to a log file,
    never to a pipe nobody reads."""
    log = open(log_path, "a", encoding="utf-8")
    child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    child.log_path = log_path  # type: ignore[attr-defined]
    return child


def external_clients(root: str) -> dict:
    """Purpose: describe the sample clients the server could launch.
    Inputs: the repository root.  Outputs: {kind: info} where info has label,
    file, available (bool), reason (why not, if unavailable) and a launcher
    (server_url, game_id, seat, name, avatar) -> subprocess.Popen."""
    clients = {}
    py = os.path.join(root, "clients", "python", "client.py")
    if os.path.isfile(py):
        def launch_py(url, gid, seat, name, avatar):
            cmd = [sys.executable, py, "--server", url, "--game", gid, "--seat", str(seat), "--name", name, "--bot", "greedy"]
            if avatar:
                cmd += ["--avatar", str(avatar)]
            return _spawn(cmd, _child_log(root, "python"))
        clients["client-python"] = {"label": "Python client (sample strategy)", "file": "clients/python/client.py",
                                    "available": True, "reason": "", "launch": launch_py}
    cpp = os.path.join(root, "clients", "cpp", "client.cpp")
    if os.path.isfile(cpp):
        binary = os.path.join(root, "clients", "cpp", "client")
        cxx = _compiler()
        available = cxx is not None or os.path.isfile(binary)

        def launch_cpp(url, gid, seat, name, avatar):
            stale = not os.path.isfile(binary) or os.path.getmtime(binary) < os.path.getmtime(cpp)
            if stale:
                if cxx is None:
                    raise RuntimeError("no C++ compiler (g++ or clang++) on this machine")
                subprocess.run([cxx, "-std=c++17", "-O2", cpp, "-o", binary], check=True, timeout=180,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            cmd = [binary, "--server", url, "--game", gid, "--seat", str(seat), "--name", name]
            return _spawn(cmd, _child_log(root, "cpp"))
        clients["client-cpp"] = {"label": "C++ client (sample strategy)", "file": "clients/cpp/client.cpp",
                                 "available": available, "reason": "" if available else "needs g++ or clang++",
                                 "launch": launch_cpp}
    java = os.path.join(root, "clients", "java", "Client.java")
    if os.path.isfile(java):
        javac, javabin = shutil.which("javac"), shutil.which("java")
        available = _tool_works(javac, "-version") and _tool_works(javabin, "-version")
        out = os.path.join(root, "clients", "java", "out")

        def launch_java(url, gid, seat, name, avatar):
            cls = os.path.join(out, "Client.class")
            if not os.path.isfile(cls) or os.path.getmtime(cls) < os.path.getmtime(java):
                subprocess.run([javac, "-d", out, java], check=True, timeout=180,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            cmd = [javabin, "-cp", out, "Client", "--server", url, "--game", gid, "--seat", str(seat), "--name", name]
            return _spawn(cmd, _child_log(root, "java"))
        clients["client-java"] = {"label": "Java client (sample strategy)", "file": "clients/java/Client.java",
                                  "available": available, "reason": "" if available else "needs Java 11+ (javac and java)",
                                  "launch": launch_java}
    return clients
