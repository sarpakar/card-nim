# Card Nim

Server, web UI and bot clients for the Card Nim game.

Author: Sarp Akar, sa9932@nyu.edu

The server is a single Python file with no dependencies (Python 3.9+). It can host
several games at once, keeps a clock for each player, checks every move, logs the
time per move and decides the winner. Bots talk to it over HTTP. Humans and
spectators use the browser.

## Running it

```sh
python3 server/cardnim_server.py
```

Prints the lobby address (something like `http://10.18.4.22:8000/`). Open it, create
a game with the number of stones and cards, and share the board link (`/game/K7PX`).

You can also create the game from the command line:

```sh
python3 server/cardnim_server.py --stones 100 --cards 25 --clock 120 --label "Round 1"
```

Then start two bots in other terminals:

```sh
python3 clients/python/client.py --game K7PX --name "Team A" --bot greedy
python3 clients/python/client.py --game K7PX --name "Team B" --bot random
```

Finished games are written to `results/<id>.json`.

## The board

`/game/ID` shows one game and updates live for everyone who has it open.

- The page is light like the lobby; the table itself is a felt card table. Seat
  1's pod sits on the top rail (blue), seat 2's on the bottom (amber), each
  with an avatar, name, clock and a time bar. The player on turn has a glow
  around the avatar.
- Hands are drawn as small playing cards that stay in place. Played cards show
  their back, the last one played has a gold edge.
- The pile in the middle of the felt shows the count and one chip per stone,
  in rows of 25.
- The lobby is a light three-pane page: search and filter tables on the
  left, see the selected table's seats and stats in the middle with a Join or
  Watch button, and create a table on the right. A QR code of the lobby
  address sits under the form so people can join from their phones, and a
  Strategies list at the bottom left names every sample client and server
  bot with the file it lives in.
- An open seat's pod is the sign-in: click the circle to pick one of 16
  pixel-art faces, type a name where the player's name will be, and press
  Sit here. Switch the pod to Bot to let the server play that seat: its
  built-in bots (greedy, random) or any of the sample clients, which the
  server builds and runs as a child process exactly as a team would. That
  is the quickest way to fill a table for a demo, and a live check that the
  hand-out works on the machine. Bots joining over the API get a face from their name (or
  pass `--avatar N`). On your turn your playable cards become buttons. Cards
  bigger than the pile are disabled since playing one loses immediately.
- A seat belongs to the browser tab that took it. Two people use two tabs,
  windows or laptops. To test alone, take both seats from one tab.
- Before the game starts you can leave your seat. During play you can resign
  (asks for confirmation).
- The move log lists every move with stones left and seconds used.
- When the game ends the status card shows the winner and why. "Replay"
  plays the recorded moves back one every 2 seconds (pause, step, restart,
  1s/2s/4s per move), which is the way to watch a bot game that finished in
  a blink. "Play again" makes a new game with the same settings. "Abort game"
  ends a stuck game with no winner, after a confirmation.

Screenshots are in `docs/screenshots/`.

## Writing a bot

Three HTTP calls. Details in `docs/API.md`.

1. `POST /api/games/{id}/join` with your name. You get a token and a seat.
2. `GET /api/games/{id}/getstate` with the token. Blocks until it's your turn
   (or the game is over) and returns stones left, both hands, both clocks and
   the move history.
3. `POST /api/games/{id}/move` with the token and your `card`. A rejected move
   gets a 409 with the reason and changes nothing.

Repeat 2 and 3 until `status` is `finished`. Waiting inside `getstate` doesn't
use your clock.

Python:

```python
from client import CardNimClient

def brain(stones, my_cards, opp_cards, state):
    return stones if stones in my_cards else min(c for c in my_cards if c <= stones)

CardNimClient("http://10.18.4.22:8000", "K7PX", "Team A").play(brain)
```

or edit `greedy_bot` in `clients/python/client.py` and run:

```sh
python3 clients/python/client.py --server http://10.18.4.22:8000 --game K7PX --name "Team A" --bot greedy [--seat 1]
```

C++ (no dependencies, uses the text format so there is no JSON to parse; edit `choose_card()`):

```sh
g++ -std=c++17 -O2 clients/cpp/client.cpp -o client
./client --server http://10.18.4.22:8000 --game K7PX --name "Team A" [--seat 1]
```

Java 11+ (edit `chooseCard()`):

```sh
javac clients/java/Client.java -d out
java -cp out Client --server http://10.18.4.22:8000 --game K7PX --name "Team A" [--seat 1]
```

Other languages: use `?format=text` and two or three HTTP requests. There is a
shell bot at the bottom of `docs/API.md`. All sample clients also read
`CARDNIM_SERVER`, `CARDNIM_GAME`, `CARDNIM_NAME` and `CARDNIM_SEAT` from the
environment, which is how `scripts/run_match.py` drives them.

## Competition night

1. Start the server on a machine on the room's network and note the address.
2. Create the game with the announced s and k.
3. Put the board on the projector. Give both teams the game id and the address.
   Team A joins seat 1, team B seat 2 (bots can pass `--seat`). The game starts
   and seat 1's clock runs as soon as both are seated.
4. When it ends, click "Play again" for the return match with seats swapped.

Bot vs bot without humans:

```sh
python3 scripts/run_match.py --stones 100 --cards 25 --both-orders \
    --name1 "Team A" --name2 "Team B" \
    "python3 clients/python/client.py --bot greedy" "./client"
```

## Rules as enforced

1. The game starts when both seats are taken. Seat 1 moves first.
2. A move is a card from your own hand, on your turn. Anything else gets a 409
   and is ignored. Your clock keeps running.
3. Card equal to the pile: you win. Card bigger than the pile: you lose, the
   pile stays as it was.
4. Otherwise the pile shrinks and the turn passes. If the next player has no
   card small enough, they lose right away.
5. Chess clocks. Yours runs from the moment your turn starts until your move is
   accepted. Zero means you lose. A background thread checks this even if
   nobody is polling. Default is 120 seconds per player, set per game.
6. Every move is logged with stones before/after and seconds spent.

Limits: 1 ≤ s ≤ 500, 1 ≤ k ≤ 200. The lobby warns if 1+2+...+k ≤ s.

## Files

```
server/
  engine.py            rules (no I/O)
  cardnim_server.py    HTTP API, long polling, clock thread, static files, results
  bots.py              bots the server can seat itself (greedy, random)
  web/                 index.html + lobby.js + lobby.css + qr.js (lobby), game.html + game.js + style.css (board)
clients/
  python/client.py     library + sample bots (random, greedy)
  cpp/client.cpp
  java/Client.java
scripts/
  run_match.py         bot vs bot matches
tests/
  test_engine.py
  test_api.py          end to end over HTTP
docs/
  API.md               protocol
```

## Tests

```sh
python3 -m pytest tests/ -q
```

28 tests, about 4 seconds. Includes the 5 stones / cards 1-3 example (second
player wins) and full games over HTTP with timeouts and long polling.

## Problems

- Bots on another laptop can't connect: check the printed address and the
  firewall. `curl http://ADDRESS:8000/api/health` should return `{"ok": true}`.
- Port already in use: pass `--port 8001`.
- 401: the bot didn't send its token (`X-Token` header, `token` param, or cookie).
- 409 "not your turn": the bot called `move` without waiting on `getstate`.
- Stuck game: "Abort game" on the board or `POST /api/games/ID/abort`.
- Timing: measured on the server from the previous move to yours, so network
  latency counts against the mover. Both teams should be on the same network.
