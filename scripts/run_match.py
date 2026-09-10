#!/usr/bin/env python3
"""
Run a match between two bot programs against a running server.

    python3 scripts/run_match.py --stones 100 --cards 25 \
        "python3 clients/python/client.py --bot greedy" \
        "./clients/cpp/client"

    The architects' private demo bot works the same way:
        python3 scripts/run_match.py --stones 100 --cards 25 "python3 private/minimax_bot.py" "./clients/cpp/client"

Each command is launched with these environment variables set, which every
sample client honours:  CARDNIM_SERVER, CARDNIM_GAME, CARDNIM_SEAT, CARDNIM_NAME.
Use --both-orders to play twice with the seats swapped (the usual competition
format), and --rounds N to repeat.  Results are printed as a small table.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request


def api(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def run_one(base: str, stones: int, cards: int, clock: float, cmds: list[str], names: list[str],
            label: str, show_output: bool) -> dict:
    """Purpose: create a game, start both bots, wait for the end, return the final state."""
    state = api(base, "POST", "/api/games", {"stones": stones, "cards": cards, "time_limit": clock, "label": label})
    gid = state["id"]
    print(f"  game {gid}: {names[0]} (seat 1) vs {names[1]} (seat 2)   board: {base}/game/{gid}")
    procs = []
    for seat, (cmd, name) in enumerate(zip(cmds, names), start=1):
        env = dict(os.environ, CARDNIM_SERVER=base, CARDNIM_GAME=gid, CARDNIM_SEAT=str(seat), CARDNIM_NAME=name)
        out = None if show_output else subprocess.DEVNULL
        try:
            procs.append(subprocess.Popen(shlex.split(cmd), env=env, stdout=out, stderr=out))
        except (OSError, ValueError) as exc:
            for p in procs:
                p.kill()
            api(base, "POST", f"/api/games/{gid}/abort", {"reason": f"could not start {name}: {exc}"})
            raise SystemExit(f"cannot start bot {name!r} with command {cmd!r}: {exc}")
        time.sleep(0.3)  # let seat 1 sit down first so seats match the names
    # wait for the game to finish (the server enforces clocks, so this terminates)
    deadline = time.time() + 2 * clock + 60
    while time.time() < deadline:
        state = api(base, "GET", f"/api/games/{gid}?since={state['version']}&timeout=20")
        if state["status"] == "finished":
            break
    else:
        api(base, "POST", f"/api/games/{gid}/abort", {"reason": "run_match gave up waiting"})
        state = api(base, "GET", f"/api/games/{gid}")
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run bot-vs-bot Card Nim matches")
    ap.add_argument("cmd1", help="command for the first bot (quoted)")
    ap.add_argument("cmd2", help="command for the second bot (quoted)")
    ap.add_argument("--server", default=os.environ.get("CARDNIM_SERVER", "http://localhost:8000"))
    ap.add_argument("--stones", type=int, required=True)
    ap.add_argument("--cards", type=int, required=True)
    ap.add_argument("--clock", type=float, default=120)
    ap.add_argument("--name1", default="Bot A")
    ap.add_argument("--name2", default="Bot B")
    ap.add_argument("--both-orders", action="store_true", help="also play with seats swapped")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--show-output", action="store_true", help="show the bots' stdout/stderr")
    args = ap.parse_args(argv)

    base = args.server.rstrip("/")
    try:
        api(base, "GET", "/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"cannot reach the server at {base}: {exc}\nstart it with: python3 server/cardnim_server.py", file=sys.stderr)
        return 1

    wins = {args.name1: 0, args.name2: 0, "none": 0}
    results = []
    orders = [(0, 1)] + ([(1, 0)] if args.both_orders else [])
    for rnd in range(1, args.rounds + 1):
        for order in orders:
            cmds = [[args.cmd1, args.cmd2][i] for i in order]
            names = [[args.name1, args.name2][i] for i in order]
            label = f"{args.name1} vs {args.name2}, round {rnd}" + (" (swapped)" if order == (1, 0) else "")
            print(f"round {rnd}{' (swapped)' if order == (1, 0) else ''}")
            state = run_one(base, args.stones, args.cards, args.clock, cmds, names, label, args.show_output)
            winner = names[state["winner"] - 1] if state["winner"] else "none"
            wins[winner] += 1
            results.append((state["id"], names[0], names[1], winner, len(state["moves"]), state["reason"]))
            print(f"  -> {winner} wins after {len(state['moves'])} moves: {state['reason']}")

    print("\nsummary")
    print(f"  {'game':6} {'seat 1':14} {'seat 2':14} {'winner':14} {'moves':>5}  reason")
    for gid, s1, s2, w, n, reason in results:
        print(f"  {gid:6} {s1:14} {s2:14} {w:14} {n:5}  {reason}")
    print(f"\n  {args.name1}: {wins[args.name1]}   {args.name2}: {wins[args.name2]}   no winner: {wins['none']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
