/* Board page: live view of one game for players and observers.

   How it works
   - The game id comes from the URL (/game/K7PX).
   - We long-poll GET /api/games/{id}?since=<version> so every change from any
     player arrives within milliseconds without a WebSocket.
   - Seats this tab holds are kept in sessionStorage as {seat: token}.  It is
     per tab, so two tabs of the same browser can be the two players, and one
     tab may hold both seats to test alone.  The token is sent as the X-Token
     header; for a held seat the playable cards become buttons on its turn.
     Everyone else is an observer and sees exactly the same board.
   - Clocks tick locally between polls: the server tells us the remaining time
     at the moment it answered, we subtract the time since.
   - Anything the server refuses is shown in place; the page never pops up a
     browser dialog. */

(function () {
  "use strict";

  const rawId = (window.location.pathname.match(/\/game\/([A-Za-z0-9]+)/) || [])[1];
  const gameId = rawId ? rawId.toUpperCase() : null;
  const $ = (id) => document.getElementById(id);
  const tokenKey = "cardnim_tokens_" + gameId;

  let state = null;          // last state from the server
  let receivedAt = 0;        // performance.now() when `state` arrived
  let lastPileSize = null;   // to animate pebbles that just disappeared
  let pebbleEls = [];        // one element per original stone, in order
  let pendingError = {};     // seat -> move rejection text shown under that hand
  let confirmResign = {};    // seat -> true while the resign confirmation is shown
  let draftName = {};        // seat -> name typed into an open seat's pod
  let chosenAvatar = {};     // seat -> avatar picked for an open seat
  let popOpen = {};          // seat -> true while the avatar picker is open
  let joining = {};          // seat -> true while a sit-down request is in flight
  let seatMode = {};         // seat -> "human" | "bot" in the sign-in pod
  let botKind = {};          // seat -> chosen bot kind
  let botKinds = [];         // [{kind, label}] from /api/bots
  fetch("/api/bots", { cache: "no-store" }).then((r) => r.ok ? r.json() : { bots: [] })
    .then((b) => { botKinds = b.bots || []; if (state) render(); }).catch(() => { botKinds = []; });
  // Replay of a finished game: the board is redrawn from the first n moves,
  // advancing one move every `speed` ms.  Nothing is sent to the server.
  let replay = { active: false, index: 0, playing: false, speed: 2000, timer: null };
  let replayView = null;     // the derived state currently on screen while replaying
  let celebrated = null;     // key of the finish we already celebrated (fireworks once per finish)
  let winnerDismissed = false;
  let confirmAbort = false;  // true while the abort confirmation is shown
  let moveInFlight = false;  // a move was sent and not yet answered
  let pollController = null; // lets join()/leave() cancel a poll sent with the old identity
  let tokens = {};           // seat -> token, for the seats this tab holds
  try {
    const raw = sessionStorage.getItem(tokenKey);
    tokens = raw ? JSON.parse(raw) : {};
    if (typeof tokens !== "object" || tokens === null || Array.isArray(tokens)) tokens = {};
  } catch (e) { tokens = {}; }

  function saveTokens() {
    try { sessionStorage.setItem(tokenKey, JSON.stringify(tokens)); } catch (e) { /* memory only */ }
  }
  function held(seat) { return typeof tokens[seat] === "string" && tokens[seat].length > 0; }
  function heldSeats() { return [1, 2].filter(held); }
  function dropToken(seat) { delete tokens[seat]; saveTokens(); }

  /* Purpose: headers for a request.  With a seat, that seat's token; without,
     the token of any seat we hold (so `you` in the state is meaningful). */
  function authHeaders(seat, extra) {
    const h = Object.assign({}, extra || {});
    const s = seat || heldSeats()[0];
    if (s && tokens[s]) h["X-Token"] = tokens[s];
    return h;
  }

  const els = {
    banner: $("banner"), bannerText: $("banner-text"), bannerActions: $("banner-actions"),
    count: $("count"), countLabel: $("count-label"), pebbles: $("pebbles"),
    logBody: $("log-body"), sideInfo: $("side-info"),
    players: { 1: $("player-1"), 2: $("player-2") },
  };

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  function fmtClock(seconds) {
    // Round to tenths first so 119.96 shows as 2:00.0, never 1:60.0.
    const tenths = Math.max(0, Math.round((Number(seconds) || 0) * 10));
    const m = Math.floor(tenths / 600);
    const rest = (tenths % 600) / 10;
    return m + ":" + (rest < 10 ? "0" : "") + rest.toFixed(1);
  }

  /* Purpose: the state to draw: the live one, or the replay's derived one. */
  function current() { return replay.active && replayView ? replayView : state; }

  function nameOf(seat) {
    const p = current().players[seat - 1];
    return p.name || ("seat " + seat);
  }

  /* ------------------------------------------------------------ replay */

  /* Purpose: rebuild the position after the first n moves of a finished game.
     Inputs: the real (final) state and n.  Outputs: a state-shaped object. */
  function replayState(real, n) {
    const total = real.moves.length;
    n = Math.max(0, Math.min(n, total));
    const moves = real.moves.slice(0, n);
    const played = { 1: new Set(), 2: new Set() };
    const used = { 1: 0, 2: 0 };
    let stones = real.initial_stones;
    for (const m of moves) {
      played[m.seat].add(m.card);
      used[m.seat] += Number(m.elapsed) || 0;
      if (!m.overdraw) stones = m.stones_after;
    }
    const done = n >= total;
    const last = moves[moves.length - 1] || null;
    const all = Array.from({ length: real.num_cards }, (_, i) => i + 1);
    const players = real.players.map((p, i) => Object.assign({}, p, {
      cards: all.filter((c) => !played[i + 1].has(c)),
      playable: [],
      time_remaining: Math.max(0, real.time_limit - used[i + 1]),
    }));
    return Object.assign({}, real, {
      stones, moves, players,
      last_move: last ? { seat: last.seat, card: last.card } : null,
      status: done ? "finished" : "playing",
      turn: done ? null : real.moves[n].seat,
      winner: done ? real.winner : null,
      reason: done ? real.reason : "",
      your_turn: false,
    });
  }

  function replayShow(n) {
    replay.index = Math.max(0, Math.min(n, state.moves.length));
    replayView = replayState(state, replay.index);
    receivedAt = performance.now();
    render();
  }

  function replayTick() {
    if (!replay.active || !replay.playing) return;
    if (replay.index >= state.moves.length) { replayPause(); return; }
    replayShow(replay.index + 1);
    if (replay.index >= state.moves.length) replayPause();
  }

  function replayPlay() {
    if (replay.index >= state.moves.length) replay.index = 0;   // play again from the start
    replay.playing = true;
    clearInterval(replay.timer);
    replay.timer = setInterval(replayTick, replay.speed);
    replayShow(replay.index);
  }

  function replayPause() {
    replay.playing = false;
    clearInterval(replay.timer);
    replay.timer = null;
    render();
  }

  function replayStart() {
    if (!state || state.status !== "finished" || !state.moves.length) return;
    replay.active = true;
    replay.index = 0;
    winnerDismissed = false;
    replayPlay();
  }

  function replayStop() {
    clearInterval(replay.timer);
    replay = { active: false, index: 0, playing: false, speed: replay.speed, timer: null };
    replayView = null;
    lastPileSize = null;
    winnerDismissed = true;      // back on the final board without the card in the way
    render();
  }

  const NUM_AVATARS = 16;
  function avatarSrc(n) { return "/avatars/av" + String(n).padStart(2, "0") + ".png"; }

  /* Purpose: true if the object looks like a game state (guards against an
     error body or a half-broken proxy answer reaching render()). */
  function looksLikeState(obj) {
    return obj && typeof obj === "object" && Array.isArray(obj.players) && obj.players.length === 2
      && typeof obj.version === "number" && Array.isArray(obj.moves);
  }

  /* ------------------------------------------------------------ polling */

  /* Purpose: accept a fresh state from the server and redraw if it changed.
     When only the clocks moved, keep the current state object. */
  function accept(next, sentSeat) {
    if (!looksLikeState(next)) throw new Error("unexpected answer from the server");
    // The server answers `you` for the token we sent.  If it does not
    // recognise it (server restarted, seat freed elsewhere), forget it.
    const lostSeat = Boolean(sentSeat && next.you !== sentSeat);
    if (lostSeat) dropToken(sentSeat);
    const changed = !state || next.version !== state.version;
    if (changed) {
      if (!state || state.status !== "finished") winnerDismissed = false;
      state = next;
      pendingError = {};
      moveInFlight = false;
    } else {
      for (let i = 0; i < 2; i++) state.players[i].time_remaining = next.players[i].time_remaining;
    }
    receivedAt = performance.now();
    if (changed || lostSeat) render();
  }

  let pollFailures = 0;
  async function poll() {
    if (!gameId) return;
    const since = state ? state.version : -1;
    const url = `/api/games/${gameId}?since=${since}&timeout=25`;
    const controller = new AbortController();
    pollController = controller;
    const sentSeat = heldSeats()[0] || null;
    try {
      const res = await fetch(url, { cache: "no-store", headers: authHeaders(sentSeat), signal: controller.signal });
      if (controller !== pollController) return;      // superseded by restartPolling()
      if (res.status === 404) {
        els.banner.className = "panel banner";
        els.bannerText.textContent = `There is no game ${gameId}. The server may have been restarted.`;
        els.bannerActions.innerHTML = '<a class="btn quiet small" href="/">Back to the lobby</a>';
        return;
      }
      if (!res.ok) throw new Error("server answered " + res.status);
      accept(await res.json(), sentSeat);
      pollFailures = 0;
      // A finished game never changes again: stop holding a server thread.
      if (state.status !== "finished") poll();
    } catch (err) {
      if (err.name === "AbortError") return;   // restarted by join()/leave()
      pollFailures += 1;
      if (pollFailures >= 2) els.bannerText.textContent = "Lost contact with the server. Retrying…";
      setTimeout(poll, Math.min(1500 * pollFailures, 8000));
    }
  }

  /* Purpose: cancel the poll in flight and start a fresh one (after our set
     of tokens changed, the old request carries the wrong identity). */
  function restartPolling() {
    if (pollController) pollController.abort();
    pollController = null;
    poll();
  }

  /* Purpose: fetch the state right now (after a rejected move, for instance)
     so the page shows the truth instead of waiting for the next change. */
  async function refreshNow() {
    try {
      const sentSeat = heldSeats()[0] || null;
      const res = await fetch(`/api/games/${gameId}`, { cache: "no-store", headers: authHeaders(sentSeat) });
      if (!res.ok) return;
      const next = await res.json();
      if (!looksLikeState(next)) return;
      if (sentSeat && next.you !== sentSeat) dropToken(sentSeat);
      state = next; receivedAt = performance.now(); render();
    } catch (e) { /* the long-poll will catch up */ }
  }

  /* ------------------------------------------------------------ actions */

  async function api(method, path, body, seat) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: authHeaders(seat, { "Content-Type": "application/json" }),
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new Error("cannot reach the server");
    }
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(payload.error || ("server answered " + res.status));
      err.status = res.status;
      throw err;
    }
    return payload;
  }

  async function join(seat, name, avatar) {
    const payload = await api("POST", `/api/games/${gameId}/join`, { name, seat, avatar });
    tokens[payload.seat] = payload.token;
    saveTokens();
    if (looksLikeState(payload.state)) { state = payload.state; receivedAt = performance.now(); render(); }
    restartPolling();
  }

  async function seatBot(seat) {
    const kind = botKind[seat] || (botKinds[0] && botKinds[0].kind);
    if (!kind) throw new Error("no bots available on this server");
    const next = await api("POST", `/api/games/${gameId}/bot`, { seat, kind, avatar: chosenAvatar[seat] });
    if (looksLikeState(next)) { state = next; receivedAt = performance.now(); render(); }
  }

  async function leave(seat) {
    try {
      await api("POST", `/api/games/${gameId}/leave`, null, seat);
    } catch (err) {
      if (err.status !== 401) throw err;     // 401: the seat was already gone
    }
    dropToken(seat);
    confirmResign = {};
    await refreshNow();
    restartPolling();
  }

  async function playCard(seat, card) {
    if (moveInFlight) return;                // a double click must not send two moves
    moveInFlight = true;
    delete pendingError[seat];
    // Freeze the hand at once so the second click of a double click hits nothing.
    els.players[seat].querySelectorAll(".card.can-play").forEach((b) => { b.disabled = true; });
    try {
      const next = await api("POST", `/api/games/${gameId}/move`, { card }, seat);
      moveInFlight = false;
      if (looksLikeState(next)) { if (state.status !== "finished") winnerDismissed = false; state = next; receivedAt = performance.now(); render(); }
      else await refreshNow();
    } catch (err) {
      moveInFlight = false;
      if (err.status === 401) dropToken(seat);
      pendingError[seat] = err.message;
      await refreshNow();
      if (pendingError[seat]) render();
    }
  }

  async function rematch() {
    const payload = await api("POST", "/api/games", {
      stones: state.initial_stones, cards: state.num_cards, time_limit: state.time_limit,
      // "Round 1 (again)" no matter how many rematches deep we are
      label: (state.label || "Table " + state.id).replace(/(\s*\(again\))+$/, "") + " (again)",
    });
    window.location.href = "/game/" + payload.id;
  }

  async function abort() {
    await api("POST", `/api/games/${gameId}/abort`, { reason: "aborted from the board page" });
    confirmAbort = false;
  }

  /* ------------------------------------------------------------ rendering */

  /* Purpose: redraw everything from `state`.  Keeps keyboard focus on the
     same control when the DOM is rebuilt underneath it. */
  function render() {
    const s = current();
    const active = document.activeElement;
    const focusCard = active && active.classList && active.classList.contains("card")
      ? { seat: active.closest(".player") === els.players[2] ? 2 : 1, card: active.dataset.card } : null;
    const focusName = active && active.matches && active.matches("input[data-name]")
      ? { seat: active.closest(".player") === els.players[2] ? 2 : 1, pos: active.selectionStart } : null;

    document.title = `Card Nim ${s.id} — ${s.stones} stones`;
    $("meta-id").textContent = s.id;
    $("meta-stones").textContent = s.initial_stones;
    $("meta-cards").textContent = s.num_cards;
    $("meta-clock").textContent = fmtClock(s.time_limit).replace(/\.0$/, "");
    $("meta-label").textContent = s.label || "";
    const cover = $("cover");
    if (cover && !cover.dataset.done) {
      // a stable two-colour gradient from the id: every table gets its own cover
      let h = 0;
      for (const ch of s.id) h = (h * 31 + ch.charCodeAt(0)) % 360;
      cover.style.setProperty("--tile-a", `hsl(${h} 70% 55%)`);
      cover.style.setProperty("--tile-b", `hsl(${(h + 60) % 360} 75% 45%)`);
      cover.textContent = s.id;
      cover.dataset.done = "1";
    }

    renderBanner();
    renderPlayer(1);
    renderPlayer(2);
    renderPile();
    renderLog();
    renderSide();
    renderWinner();

    if (focusCard) {
      const again = els.players[focusCard.seat].querySelector(`.card[data-card="${focusCard.card}"]`);
      if (again && !again.disabled) again.focus();
    }
    if (focusName) {
      const again = els.players[focusName.seat].querySelector("input[data-name]");
      if (again) { again.focus(); try { again.setSelectionRange(focusName.pos, focusName.pos); } catch (e) { /* fine */ } }
    }
  }

  function renderBanner() {
    const s = current();
    els.banner.className = "panel banner";
    let text = "";
    let actions = "";
    const mine = heldSeats();
    if (replay.active) {
      const total = state.moves.length;
      const last = s.last_move;
      if (s.turn) els.banner.classList.add("turn-" + s.turn);
      els.banner.classList.add("replay");
      text = `<strong>Replay</strong> <span class="muted">move ${replay.index} of ${total}` +
        (last ? ` · ${esc(nameOf(last.seat))} played ${last.card}` : " · start") +
        (replay.index >= total && s.winner ? ` · ${esc(nameOf(s.winner))} wins` : "") + "</span>";
      actions = `<button class="btn small" id="rp-restart" title="Restart">|&lt;</button>` +
        `<button class="btn small black" id="rp-toggle">${replay.playing ? "Pause" : (replay.index >= total ? "Play again" : "Play")}</button>` +
        `<button class="btn small" id="rp-next" title="Next move"${replay.index >= total ? " disabled" : ""}>&gt;|</button>` +
        `<select class="speed" id="rp-speed" aria-label="Seconds per move">` +
        [1, 2, 4].map((v) => `<option value="${v * 1000}"${replay.speed === v * 1000 ? " selected" : ""}>${v}s / move</option>`).join("") +
        `</select><button class="btn small" id="rp-exit">Exit replay</button>`;
    } else if (s.status === "waiting") {
      const open = s.players.filter((p) => !p.occupied).length;
      text = open === 2 ? "Waiting for two players to sit down." : "Waiting for one more player.";
      if (mine.length === 1) text += ` You are seat ${mine[0]}.`;
      if (mine.length === 2) text += " You hold both seats.";
    } else if (s.status === "playing") {
      els.banner.classList.add("turn-" + s.turn);
      text = held(s.turn) ? `Your move, ${esc(nameOf(s.turn))}.` : `${esc(nameOf(s.turn))} to move.`;
      actions = confirmAbort
        ? '<span class="muted" style="font-size:15px">End this game with no winner?</span>' +
          '<button class="btn small danger" id="abort-yes">Yes, abort</button>' +
          '<button class="btn quiet small" id="abort-no">Keep playing</button>'
        : '<button class="btn quiet small" id="abort-btn">Abort game</button>';
    } else {
      els.banner.classList.add("final");
      text = s.winner
        ? `<strong>${esc(nameOf(s.winner))} wins.</strong> <span class="muted">${esc(s.reason)}</span>`
        : `<strong>No winner.</strong> <span class="muted">${esc(s.reason)}</span>`;
      actions = (s.moves.length ? '<button class="btn small" id="replay-btn">Replay</button>' : "") +
                '<button class="btn gold small" id="rematch-btn">Play again (same settings)</button>' +
                '<a class="btn quiet small" href="/">Lobby</a>';
    }
    els.bannerText.innerHTML = text;
    els.bannerActions.innerHTML = actions;
    const showError = (e) => { els.bannerText.textContent = e.message; };
    const ab = $("abort-btn");
    if (ab) ab.addEventListener("click", () => { confirmAbort = true; renderBanner(); });
    const ay = $("abort-yes");
    if (ay) ay.addEventListener("click", () => { ay.disabled = true; abort().catch(showError); });
    const an = $("abort-no");
    if (an) an.addEventListener("click", () => { confirmAbort = false; renderBanner(); });
    const rb = $("rematch-btn");
    if (rb) rb.addEventListener("click", () => { rb.disabled = true; rematch().catch((e) => { rb.disabled = false; showError(e); }); });
    const rp = $("replay-btn");
    if (rp) rp.addEventListener("click", replayStart);
    const rs = $("rp-restart");
    if (rs) rs.addEventListener("click", () => { replay.index = 0; if (replay.playing) replayPlay(); else replayShow(0); });
    const rt = $("rp-toggle");
    if (rt) rt.addEventListener("click", () => { if (replay.playing) replayPause(); else replayPlay(); });
    const rn = $("rp-next");
    if (rn) rn.addEventListener("click", () => { replayPause(); replayShow(replay.index + 1); });
    const rsp = $("rp-speed");
    if (rsp) rsp.addEventListener("change", () => { replay.speed = Number(rsp.value) || 2000; if (replay.playing) replayPlay(); });
    const rx = $("rp-exit");
    if (rx) rx.addEventListener("click", replayStop);
  }

  function renderPlayer(seat) {
    const s = current();
    const p = s.players[seat - 1];
    const root = els.players[seat];
    const onTurn = s.status === "playing" && s.turn === seat;
    const isMine = held(seat);
    root.classList.toggle("on-turn", onTurn);
    root.classList.toggle("winner", s.status === "finished" && s.winner === seat);
    root.classList.toggle("loser", s.status === "finished" && !!s.winner && s.winner !== seat);

    const nameEl = root.querySelector(".name");
    const avatar = root.querySelector(".avatar");
    const cardsLeft = root.querySelector(".cards-left");
    const joinMode = s.status === "waiting" && !p.occupied;
    root.classList.toggle("join-mode", joinMode);

    if (joinMode) {
      // The pod is the sign-in: the circle picks a face, the name goes where
      // the player's name will be, and a small button sits you down.
      if (!chosenAvatar[seat]) {
        const taken = s.players[2 - seat].avatar || 0;
        let pick = 1 + Math.floor(Math.random() * NUM_AVATARS);
        if (pick === taken) pick = (pick % NUM_AVATARS) + 1;
        chosenAvatar[seat] = pick;
      }
      if (avatar) {
        avatar.classList.add("pickable");
        avatar.dataset.src = "";
        avatar.innerHTML = `<img src="${avatarSrc(chosenAvatar[seat])}" alt=""><span class="edit-badge" aria-hidden="true">+</span>`;
        avatar.setAttribute("role", "button");
        avatar.setAttribute("aria-label", "Choose your avatar");
        avatar.setAttribute("tabindex", "0");
        avatar.onclick = (ev) => {
          if (ev) ev.stopPropagation();          // the outside-click closer must not see this
          popOpen[seat] = !popOpen[seat];
          renderPlayer(seat);
        };
        avatar.onkeydown = (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); avatar.onclick(ev); } };
      }
      const mode = seatMode[seat] === "bot" && botKinds.length ? "bot" : "human";
      if (mode === "bot") {
        if (!botKind[seat] || !botKinds.some((b) => b.kind === botKind[seat])) botKind[seat] = botKinds[0].kind;
        nameEl.innerHTML = `<select class="name-input bot-select" data-bot aria-label="Which bot">` +
          botKinds.map((b) => `<option value="${esc(b.kind)}"${b.kind === botKind[seat] ? " selected" : ""}>${esc(b.label)}</option>`).join("") +
          `</select>`;
        nameEl.querySelector("[data-bot]").addEventListener("change", (ev) => { botKind[seat] = ev.target.value; });
      } else {
        nameEl.innerHTML = `<input type="text" class="name-input" data-name maxlength="40" placeholder="Your name" autocomplete="off" value="${esc(draftName[seat] || "")}">`;
        nameEl.querySelector("input").addEventListener("input", (ev) => { draftName[seat] = ev.target.value; });
      }
      const switcher = botKinds.length
        ? `<span class="seg-mini" role="group" aria-label="Who sits here">` +
          `<button type="button" data-mode="human" class="${mode === "human" ? "active" : ""}" aria-pressed="${mode === "human"}">Human</button>` +
          `<button type="button" data-mode="bot" class="${mode === "bot" ? "active" : ""}" aria-pressed="${mode === "bot"}">Bot</button></span>`
        : "";
      cardsLeft.innerHTML = switcher +
        `<button class="btn black small" type="button" data-join${joining[seat] ? " disabled" : ""}>${mode === "bot" ? "Seat bot" : "Sit here"}</button>` +
        `<span class="error" data-err></span>`;
      cardsLeft.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
        seatMode[seat] = b.dataset.mode;
        renderPlayer(seat);
      }));
      const btn = cardsLeft.querySelector("[data-join]");
      const err = cardsLeft.querySelector("[data-err]");
      const go = () => {
        if (joining[seat]) return;
        joining[seat] = true;
        btn.disabled = true;
        const done = () => { joining[seat] = false; popOpen[seat] = false; };
        const fail = (e) => { joining[seat] = false; err.textContent = e.message; btn.disabled = false; };
        if (mode === "bot") { seatBot(seat).then(done).catch(fail); return; }
        const name = (draftName[seat] || "").trim();
        if (!name) { joining[seat] = false; btn.disabled = false; err.textContent = "Type a name first."; nameEl.querySelector("input").focus(); return; }
        join(seat, name, chosenAvatar[seat]).then(done).catch(fail);
      };
      btn.addEventListener("click", go);
      const input = nameEl.querySelector("input[data-name]");
      if (input) input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); go(); } });

      // the picker: a small grid of faces under (or above) the circle
      let pop = root.querySelector(".avatar-pop");
      if (!pop) {
        pop = document.createElement("div");
        pop.className = "avatar-pop";
        pop.setAttribute("role", "group");
        pop.setAttribute("aria-label", "Choose your avatar");
        root.querySelector(".pod").appendChild(pop);
      }
      pop.hidden = !popOpen[seat];
      if (popOpen[seat]) {
        pop.innerHTML = Array.from({ length: NUM_AVATARS }, (_, i) => i + 1).map((n) =>
          `<button type="button" class="pick${n === chosenAvatar[seat] ? " selected" : ""}" data-av="${n}" aria-label="avatar ${n}" aria-pressed="${n === chosenAvatar[seat]}" style="background-image:url('${avatarSrc(n)}')"></button>`).join("");
        pop.onclick = (ev) => {
          ev.stopPropagation();
          const b = ev.target.closest(".pick");
          if (!b) return;
          chosenAvatar[seat] = Number(b.dataset.av);
          popOpen[seat] = false;
          renderPlayer(seat);
        };
      }
    } else {
      nameEl.innerHTML = p.occupied
        ? esc(p.name) + (isMine ? ' <span class="you">you</span>' : "") + (p.bot ? ' <span class="botbadge">bot</span>' : "")
          + (s.status === "finished" && s.winner === seat ? ' <span class="crown">winner</span>' : "")
        : '<span class="open">open seat</span>';
      // the avatar picture (or the seat number while open) and the cards-in-hand count
      if (avatar) {
        avatar.classList.remove("pickable");
        avatar.removeAttribute("role"); avatar.removeAttribute("tabindex"); avatar.removeAttribute("aria-label");
        avatar.onclick = null; avatar.onkeydown = null;
        if (p.occupied && p.avatar) {
          const src = avatarSrc(p.avatar);
          if (avatar.dataset.src !== src) {
            avatar.innerHTML = `<img src="${src}" alt="">`;
            avatar.dataset.src = src;
          }
        } else {
          avatar.textContent = String(seat);
          avatar.dataset.src = "";
        }
      }
      const pop = root.querySelector(".avatar-pop");
      if (pop) pop.remove();
      if (cardsLeft) cardsLeft.textContent = p.occupied ? `${p.cards.length} of ${s.num_cards} cards` : "";
    }

    // Hand.  Keep every card in place: played ones are struck through so the
    // eye can see what is gone.  Our playable cards become buttons on our turn.
    const hand = root.querySelector(".hand");
    const inHand = new Set(p.cards);
    const last = s.last_move && s.last_move.seat === seat ? s.last_move.card : null;
    const canAct = isMine && onTurn && !moveInFlight && !replay.active;
    hand.className = "hand" + (s.num_cards > 120 ? " very-dense" : s.num_cards > 45 ? " dense" : "");
    const parts = [];
    for (let c = 1; c <= s.num_cards; c++) {
      const cls = ["card"];
      let disabled = " disabled";
      if (!inHand.has(c)) {
        cls.push("played");
        if (c === last) cls.push("last");
      } else if (canAct) {
        if (c <= s.stones) { cls.push("can-play"); disabled = ""; }
        else cls.push("too-big");
        if (c === s.stones) cls.push("exact");
      }
      parts.push(`<button class="${cls.join(" ")}" data-card="${c}"${disabled} aria-label="card ${c}">${c}</button>`);
    }
    hand.innerHTML = parts.join("");
    if (canAct) {
      hand.querySelectorAll(".can-play").forEach((b) => b.addEventListener("click", () => playCard(seat, Number(b.dataset.card))));
    }

    // Note under the hand: instructions, rejection, or a plain count.
    const note = root.querySelector(".hand-note");
    note.style.color = "";
    if (replay.active) {
      note.textContent = onTurn ? "To move" : "";
    } else if (pendingError[seat]) {
      note.textContent = "Rejected: " + pendingError[seat];
      note.style.color = "var(--red-btn)";
    } else if (isMine && onTurn && moveInFlight) {
      note.textContent = "Sending your move…";
    } else if (canAct) {
      note.textContent = `Your turn: click a card. Cards larger than ${s.stones} would lose and are greyed out.`;
    } else if (s.status === "playing") {
      note.textContent = "";
    } else if (s.status === "waiting" && !p.occupied) {
      note.textContent = heldSeats().length
        ? "Open seat. Another player can sit here from their own tab or laptop, or take it too to play both sides."
        : "Open seat. Click the circle to pick a face, type a name, and sit here.";
    } else {
      note.textContent = "";
    }

    renderSeatControls(seat, p);
    tickClocks();
  }

  /* Purpose: the controls below a hand: sit down (open seat, game waiting),
     leave the seat (held seat, game waiting), or resign (held seat, playing). */
  function renderSeatControls(seat, p) {
    const s = current();
    const box = els.players[seat].querySelector(".seat-join");
    let mode = "";
    if (replay.active) { box.hidden = true; box.dataset.mode = ""; return; }
    if (s.status === "waiting" && held(seat)) mode = "leave";
    else if (s.status === "waiting" && p.occupied && p.bot) mode = "removebot";
    else if (s.status === "playing" && held(seat)) mode = "resign";
    if (!mode) { box.hidden = true; box.dataset.mode = ""; return; }
    box.hidden = false;
    if (box.dataset.mode === mode && mode !== "resign") return;   // keep what the user typed
    box.dataset.mode = mode;

    if (mode === "removebot") {
      box.innerHTML = `
        <button class="btn small" type="button" data-removebot>Remove bot</button>
        <span class="muted" style="font-size:12px">The game starts when both seats are taken.</span>
        <span class="error" data-err></span>`;
      const btn = box.querySelector("[data-removebot]");
      btn.addEventListener("click", () => {
        btn.disabled = true;
        api("POST", `/api/games/${gameId}/leave`, { seat }).then(refreshNow)
          .catch((e) => { btn.disabled = false; box.querySelector("[data-err]").textContent = e.message; });
      });
    } else if (mode === "leave") {
      box.innerHTML = `
        <button class="btn quiet small" type="button" data-leave>Leave seat ${seat}</button>
        <span class="muted" style="font-size:14px;font-weight:700">The game starts when both seats are taken.</span>
        <span class="error" data-err></span>`;
      const btn = box.querySelector("[data-leave]");
      btn.addEventListener("click", () => {
        btn.disabled = true;
        leave(seat).catch((e) => { btn.disabled = false; box.querySelector("[data-err]").textContent = e.message; });
      });
    } else {
      if (confirmResign[seat]) {
        box.innerHTML = `
          <span style="font-size:15px;font-weight:700">Resign? ${esc(nameOf(3 - seat))} wins.</span>
          <button class="btn small danger" type="button" data-yes>Yes, resign</button>
          <button class="btn quiet small" type="button" data-no>Keep playing</button>
          <span class="error" data-err></span>`;
        const yes = box.querySelector("[data-yes]");
        yes.addEventListener("click", () => {
          yes.disabled = true;
          leave(seat).catch((e) => { yes.disabled = false; box.querySelector("[data-err]").textContent = e.message; });
        });
        box.querySelector("[data-no]").addEventListener("click", () => { confirmResign[seat] = false; render(); });
      } else {
        box.innerHTML = `<button class="btn quiet small" type="button" data-resign>Resign</button>`;
        box.querySelector("[data-resign]").addEventListener("click", () => { confirmResign[seat] = true; render(); });
      }
    }
  }

  /* Purpose: the finish.  Shows the winner card over the table whenever the
     game on screen is over (live or at the end of a replay) and lights the
     fireworks once per finish.  "No winner" (aborted) gets a quiet card. */
  function renderWinner() {
    const s = current();
    const card = $("winner");
    if (!card) return;
    const over = s.status === "finished" && !(replay.active && replay.index < state.moves.length);
    const key = over ? `${s.id}:${s.version}:${replay.active ? "replay" + replay.index : "live"}` : null;
    if (!over || winnerDismissed) { card.hidden = true; return; }
    const w = s.winner;
    card.className = "winner-card " + (w ? "s" + w : "none");
    const av = $("winner-avatar");
    if (w && s.players[w - 1].avatar) av.innerHTML = `<img src="${avatarSrc(s.players[w - 1].avatar)}" alt="">`;
    else av.textContent = w ? String(w) : "–";
    $("winner-label").textContent = w ? "Winner" : "No winner";
    $("winner-name").textContent = w ? nameOf(w) : "Game over";
    $("winner-why").textContent = s.reason || "";
    $("winner-actions").innerHTML =
      (replay.active ? '<button class="btn small" id="w-exit">Exit replay</button>'
                     : (state.moves.length ? '<button class="btn small" id="w-replay">Replay</button>' : "") +
                       '<button class="btn gold small" id="w-again">Play again</button>') +
      '<button class="btn small" id="w-close">Close</button>';
    const rp = $("w-replay"); if (rp) rp.addEventListener("click", () => { winnerDismissed = false; replayStart(); });
    const ag = $("w-again"); if (ag) ag.addEventListener("click", () => { ag.disabled = true; rematch().catch((e) => { ag.disabled = false; $("winner-why").textContent = e.message; }); });
    const ex = $("w-exit"); if (ex) ex.addEventListener("click", replayStop);
    $("w-close").addEventListener("click", () => { winnerDismissed = true; card.hidden = true; });
    card.hidden = false;
    if (celebrated !== key) {
      celebrated = key;
      if (w) fireworks($("fireworks"), 4200);
    }
  }

  /* Purpose: a few seconds of fireworks on the canvas that covers the table.
     Skipped when the viewer prefers reduced motion. */
  function fireworks(canvas, durationMs) {
    if (!canvas || !canvas.getContext) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const colors = ["#ffd166", "#34c759", "#4cc2ff", "#ff9500", "#ff6b9d", "#ffffff", "#d6b25e"];
    const sparks = [];
    const start = performance.now();
    let nextBurst = start;
    if (canvas._raf) cancelAnimationFrame(canvas._raf);
    function burst(x, y) {
      const color = colors[Math.floor(Math.random() * colors.length)];
      const n = 70 + Math.floor(Math.random() * 40);
      for (let i = 0; i < n; i++) {
        const a = (Math.PI * 2 * i) / n + Math.random() * 0.2;
        const sp = 1.6 + Math.random() * 3.2;
        sparks.push({ x, y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: 1, decay: 0.009 + Math.random() * 0.010, color, r: 2.2 + Math.random() * 2.2 });
      }
    }
    function frame(now) {
      const t = now - start;
      if (t < durationMs && now >= nextBurst) {
        burst(w * (0.15 + Math.random() * 0.7), h * (0.12 + Math.random() * 0.45));
        nextBurst = now + 200 + Math.random() * 260;
      }
      ctx.clearRect(0, 0, w, h);
      for (let i = sparks.length - 1; i >= 0; i--) {
        const p = sparks[i];
        p.x += p.vx; p.y += p.vy; p.vy += 0.045; p.vx *= 0.985; p.vy *= 0.985; p.life -= p.decay;
        if (p.life <= 0) { sparks.splice(i, 1); continue; }
        ctx.globalAlpha = Math.max(0, p.life);
        ctx.fillStyle = p.color;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (t < durationMs || sparks.length) canvas._raf = requestAnimationFrame(frame);
      else { ctx.clearRect(0, 0, w, h); canvas._raf = null; }
    }
    canvas._raf = requestAnimationFrame(frame);
  }

  function renderPile() {
    const s = current();
    if (els.count.textContent !== String(s.stones)) {
      els.count.textContent = s.stones;
      // a small bump when the number changes (respects reduced motion via CSS)
      els.count.classList.remove("bump");
      void els.count.offsetWidth;
      els.count.classList.add("bump");
    }
    const pct = s.initial_stones ? (s.stones / s.initial_stones) * 100 : 0;
    $("stones-fill").style.width = pct + "%";
    $("stones-bar-label").textContent = `${s.stones} of ${s.initial_stones} stones left`;
    $("stones-bar").setAttribute("aria-valuenow", String(Math.round(pct)));
    if (s.status === "finished" && s.winner && s.stones === 0) {
      els.countLabel.innerHTML = `no stones left — <b>${esc(nameOf(s.winner))}</b> took the last one`;
    } else {
      els.countLabel.innerHTML = `${s.stones === 1 ? "stone" : "stones"} on the table, <b>${s.initial_stones - s.stones}</b> removed`;
    }

    // One pebble per original stone, in rows of 25 grouped in fives so the
    // room can count them.  Removed pebbles turn into outlines; the ones
    // removed by the latest move flash in the mover's colour for a moment.
    const total = s.initial_stones;
    if (pebbleEls.length !== total) {
      const rows = [];
      for (let r = 0; r * 25 < total; r++) {
        const groups = [];
        for (let g = 0; g < 5 && r * 25 + g * 5 < total; g++) {
          const n = Math.min(5, total - (r * 25 + g * 5));
          groups.push('<span class="pgroup">' + '<span class="pebble"></span>'.repeat(n) + "</span>");
        }
        rows.push('<div class="prow">' + groups.join("") + "</div>");
      }
      els.pebbles.innerHTML = rows.join("");
      els.pebbles.classList.toggle("many", total > 250);
      pebbleEls = Array.from(els.pebbles.querySelectorAll(".pebble"));
      lastPileSize = null;
    }
    const justGoneFrom = lastPileSize === null ? s.stones : Math.min(lastPileSize, total);
    for (let i = 0; i < total; i++) {
      const gone = i >= s.stones;
      pebbleEls[i].className = "pebble" + (gone ? (i < justGoneFrom ? " just-gone" : " gone") : "");
    }
    if (justGoneFrom > s.stones) {
      setTimeout(() => {
        for (let i = s.stones; i < justGoneFrom && i < total; i++) pebbleEls[i].className = "pebble gone";
      }, 900);
    }
    lastPileSize = s.stones;
  }

  function renderLog() {
    const s = current();
    if (!s.moves.length) {
      els.logBody.innerHTML = '<tr><td class="empty" colspan="5">No moves yet.</td></tr>';
      return;
    }
    els.logBody.innerHTML = s.moves.map((m) => `<tr>
        <td class="num muted">${m.number}</td>
        <td class="who s${m.seat}">${esc(nameOf(m.seat))}</td>
        <td class="num">${m.card}</td>
        <td class="num">${m.overdraw ? '<span class="muted">over</span>' : m.stones_after}</td>
        <td class="num muted">${Number(m.elapsed).toFixed(1)}s</td>
      </tr>`).join("");
    const scroll = els.logBody.closest(".log-scroll");
    scroll.scrollTop = scroll.scrollHeight;
  }

  function renderSide() {
    const s = current();
    const base = window.location.origin;
    const used = [1, 2].map((n) => {
      const secs = s.moves.filter((m) => m.seat === n).reduce((a, m) => a + Number(m.elapsed), 0);
      return `${esc(nameOf(n))}: ${secs.toFixed(1)}s over ${plural(s.moves.filter((m) => m.seat === n).length, "move")}`;
    });
    els.sideInfo.innerHTML = `
      <div>Thinking time so far<br>${used.join("<br>")}</div>
      <div style="margin-top:10px">Bots join with game id <code>${s.id}</code> at <code>${esc(base)}</code></div>
      <div style="margin-top:6px"><a href="/api/games/${s.id}" target="_blank" rel="noopener">Raw state (JSON)</a></div>`;
  }

  /* Clocks tick between polls.  Only the player on turn is charged. */
  function tickClocks() {
    if (!state) return;
    const s = current();
    const elapsed = replay.active ? 0 : (performance.now() - receivedAt) / 1000;
    for (const seat of [1, 2]) {
      const p = s.players[seat - 1];
      let remaining = p.time_remaining;
      if (s.status === "playing" && s.turn === seat) remaining -= elapsed;
      const el = els.players[seat].querySelector(".clock");
      el.textContent = fmtClock(remaining);
      const low = remaining < 15 && s.status === "playing";
      el.classList.toggle("low", low);
      // thick bar under the clock: share of the time limit still available
      const fill = els.players[seat].querySelector(".clock-fill");
      if (fill) {
        const pct = s.time_limit ? Math.max(0, Math.min(100, (remaining / s.time_limit) * 100)) : 0;
        fill.style.width = pct + "%";
        fill.parentElement.classList.toggle("red", low);
      }
    }
  }
  setInterval(tickClocks, 100);

  // Clicking outside an open avatar picker closes it.
  document.addEventListener("click", (ev) => {
    // composedPath() still knows the original ancestors even if the click
    // handler above re-rendered the pod and detached the target
    const path = ev.composedPath ? ev.composedPath() : [];
    if (path.some((el) => el.classList && (el.classList.contains("avatar-pop") || el.classList.contains("pickable")))) return;
    let any = false;
    for (const seat of [1, 2]) if (popOpen[seat]) { popOpen[seat] = false; any = true; }
    if (any && state) { renderPlayer(1); renderPlayer(2); }
  });

  // If the tab was hidden for a long time the clocks may be stale: refresh on return.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state && state.status !== "finished") refreshNow();
  });

  if (!gameId) {
    els.banner.className = "panel banner";
    els.bannerText.textContent = "No game id in the address.";
    els.bannerActions.innerHTML = '<a class="btn quiet small" href="/">Back to the lobby</a>';
  } else {
    poll();
  }
})();
