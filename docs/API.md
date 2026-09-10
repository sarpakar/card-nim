# Card Nim HTTP API

Everything a bot or a page needs is plain HTTP. The same API drives the browser
UI, so anything the UI can do, a program can do.

Base URL: `http://<host>:<port>` (the server prints it at startup).
All ids are 4 characters like `K7PX` and are case-insensitive.

Responses are JSON unless you add `?format=text`, which gives the
[plain-text format](#plain-text-format) for languages without a JSON parser.
Errors are JSON `{"error": "<message>"}` with a 4xx status, always.

| Status | Meaning |
|---|---|
| 200 / 201 | fine |
| 400 | malformed input (missing or non-integer parameter, out of range) |
| 401 | no seat token, or an unknown one: join first |
| 404 | no such game / endpoint |
| 405 | wrong method for this endpoint |
| 409 | rule violation: seat taken, not your turn, card not in hand, game over |

## Identifying yourself

Joining returns a **token**. Send it with every later call in any one of these
ways (the first one found wins):

1. header `X-Token: <token>`
2. parameter `token=<token>` in the query string or the JSON body
3. cookie `cardnim_<GAMEID>=<token>` (accepted if you set it; the server never sets cookies)

Observers need no token.

## Endpoints

### `GET /api/health`

`{"ok": true, "games": 3, "time": 1757470000.1, "lobby_url": "http://10.18.4.22:8000/"}`

`lobby_url` is the address other devices on the network should use; the
lobby shows it as a QR code. Override it with `--public-url` when the server
sits behind a proxy.

### `GET /api/games`

Lobby list, newest first.

```json
{"games": [{"id": "K7PX", "label": "Round 1", "status": "playing",
            "stones": 37, "initial_stones": 100, "cards": 25, "time_limit": 120,
            "players": ["Alice", "Bob"], "avatars": [12, 3], "turn": 2, "winner": null,
            "moves": 6, "created_at": 1757470000.1, "finished_at": null, "version": 9}]}
```

### `POST /api/games`

Create a game. Body (JSON or form): `stones` (1–500), `cards` (1–200),
`time_limit` seconds per player (optional, default 120), `label` (optional).
Returns **201** and the full [state](#state-object).

```sh
curl -X POST localhost:8000/api/games -H 'Content-Type: application/json' \
     -d '{"stones": 100, "cards": 25, "time_limit": 120, "label": "Round 1"}'
```

### `POST /api/games/{id}/join`

Body: `name` (shown on the board), optional `seat` (1 or 2; default: first free),
optional `avatar` (1..16, the picture shown for you; see `/avatars/av01.png`
to `/avatars/av16.png`; default: picked from your name, never the same as
the opponent's). `GET` with query parameters also works. Returns:

```json
{"seat": 1, "token": "Qm9i...", "state": { ... }}
```

The game starts the moment the second seat is taken; seat 1 moves first and
their clock starts right then. Keep the token: nothing else identifies you.
409 if the seat is taken or the game is over.

### `GET /api/games/{id}/getstate`   (token required)

The course's `getstate`: **returns only when it is your turn or the game is
over.** Waiting does not use your clock. The server holds the request up to
`timeout` seconds (default and maximum 60); if nothing happened by then it
answers anyway with `your_turn: false` and you simply call again. The sample
clients hide this loop from you.

### `POST /api/games/{id}/move`   (token required)

Body: `card`. `GET /api/games/{id}/move?card=7&token=...` also works, for
testing from a browser address bar or `curl`.

The server checks that the game is in progress, that it is your turn, and that
the card is in your hand. If any check fails you get **409** and nothing
changes (your clock keeps running). Otherwise the move is applied and the new
state returned:

* card == stones left: you win;
* card > stones left: you lose at once (the pile is left untouched, the move is
  logged with `overdraw: true`);
* otherwise the turn passes. If the opponent now has no card small enough for
  the pile, they lose automatically and the game ends.

### `GET /api/games/{id}`  (alias `/api/games/{id}/state`)

The state for observers and pages. Add `since=<version>` to **long-poll**: the
response is delayed until the game's version exceeds `since`, or `timeout`
seconds pass (default 25, max 60). A token, if sent, fills in `you`/`your_turn`.

### `GET /api/bots`

`{"bots": [{"kind": "greedy", "label": "Greedy bot"}, {"kind": "random", "label": "Random bot"}]}`

Bots the server can play itself: two simple ones ship with it, the
architects may add their own in a private file that is not distributed, and
the sample clients this machine can build and run (`client-python`,
`client-cpp`, `client-java`) are listed too. Seating one of those starts the
program as a child process that joins over HTTP, exactly as a team would
run it.

### `GET /api/strategies`

The sample clients found under `clients/` (language, file, the function to
replace, a run command) and the bots the server can seat with the file each
lives in. The lobby shows this list so teams know where to start.

### `POST /api/games/{id}/bot`

Body: `kind` (from `/api/bots`), optional `seat`, `avatar`, `name`, and
`delay` (seconds the bot pauses before each move so people can follow;
default 0.8). The server seats the bot and plays for it until the game ends.
The bot's token stays inside the server. Returns the state; 409 if the seat
is taken. `players[i].bot` is true for such a seat, and the lobby summary
carries `bots: [bool, bool]`.

### `POST /api/games/{id}/leave`   (token required)

Before the game starts: the seat becomes open again and the token stops
working. During play: you resign and the opponent wins. 409 once the game is
over.

### `POST /api/games/{id}/abort`

Ends a game with no winner (`reason` optional). Meant for the architects when a
bot is stuck or the parameters were wrong. No authentication: this is a
classroom tool.

## State object

```json
{
  "id": "K7PX", "label": "Round 1", "status": "playing", "version": 9,
  "initial_stones": 100, "stones": 37, "num_cards": 25, "time_limit": 120.0,
  "turn": 2,
  "players": [
    {"seat": 1, "name": "Alice", "occupied": true, "avatar": 12,
     "cards": [1, 2, 4, 5, 6, 8, 9, ...], "playable": [1, 2, 4, ...],
     "time_remaining": 101.532},
    {"seat": 2, "name": "Bob", "occupied": true, "cards": [...], "playable": [...],
     "time_remaining": 97.004}
  ],
  "moves": [
    {"number": 1, "seat": 1, "card": 7, "stones_before": 100, "stones_after": 93,
     "elapsed": 0.412, "overdraw": false, "at": 1757470012.3}
  ],
  "last_move": {"seat": 1, "card": 10},
  "winner": null, "reason": "",
  "created_at": 1757470000.1, "finished_at": null,
  "you": 2, "your_turn": true
}
```

* `status`: `waiting` (fewer than two players), `playing`, `finished`.
* `turn`: seat to move, `null` unless playing.
* `players[i].avatar`: 1..16, the picture for that player (0 while the seat is open).
* `players[i].cards`: cards still in that hand (hands are public in Card Nim).
  `playable`: the subset not larger than the pile (empty unless playing).
* `players[i].time_remaining`: seconds left, live (the player on turn is being
  charged as you read it).
* `moves[i].elapsed`: seconds that player spent on that move, server-measured.
* `winner`: 1, 2, or `null` (game not over, or aborted). `reason` explains it
  in words, e.g. `"Bob played 12 with only 5 stones left"`.
* `you` / `your_turn`: only meaningful when you sent a token.

## Plain-text format

Add `?format=text` to any endpoint that returns a state. One `key value...`
pair per line, always in this order; lists are space-separated.

```
status playing
you 2
turn 2
your_turn 1
stones 37
initial_stones 100
num_cards 25
your_cards 1 2 3 4 5 6 7 8 9 10 11 13 14 15 16 17 18 19 20 21 22 23 24
opp_cards 1 2 4 5 6 8 9 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25
your_time 97.004
opp_time 101.532
last_move 1 10
winner 0
reason -
version 9
```

`join?format=text` prefixes two extra lines, `seat N` and `token T`.
Errors in text mode are still JSON (`{"error": ...}`) with a 4xx status; the
sample clients look at the status code first.

## Minimal bot, in shell

```sh
S=http://localhost:8000; G=K7PX
T=$(curl -s -X POST "$S/api/games/$G/join?name=curl" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
while :; do
  curl -s "$S/api/games/$G/getstate?format=text&token=$T" > state.txt
  grep -q '^status finished' state.txt && { grep reason state.txt; break; }
  grep -q '^your_turn 1' state.txt || continue
  STONES=$(awk '/^stones/{print $2}' state.txt)
  CARD=$(awk '/^your_cards/{for(i=2;i<=NF;i++) if($i<='"$STONES"') {c=$i}; print c}' state.txt)  # biggest fitting card
  curl -s -X POST "$S/api/games/$G/move" -H "X-Token: $T" -d "card=$CARD" > /dev/null
done
```
