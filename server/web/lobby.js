/* Lobby page: a three-pane window.  Left: searchable, filterable list of
   tables.  Middle: the selected table with a Join / Watch action.  Right: the
   form to create a table.  Talks to /api/games. */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const form = $("create-form");
  const errorEl = $("create-error");
  const hintEl = $("sum-hint");
  const listEl = $("games-body");
  const countEl = $("games-count");
  const serverInfo = $("server-info");
  const segment = $("segment");
  const search = $("search");
  const detail = $("detail");
  const detailTitle = $("detail-title");

  let filter = "all";
  let query = "";
  let selectedId = null;
  let lastGames = [];

  /* ------------------------------------------------------------ create */

  let creating = false;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (creating) return;                       // a double click must not make two games
    creating = true;
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    errorEl.textContent = "";
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      const res = await fetch("/api/games", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || res.statusText);
      window.location.href = "/game/" + payload.id;
    } catch (err) {
      errorEl.textContent = "Could not create the table: " + err.message;
      creating = false;
      submit.disabled = false;
    }
  });

  /* Purpose: warn when the precondition (sum of cards > stones) fails. */
  function updateHint() {
    const s = Number(form.elements.stones.value);
    const k = Number(form.elements.cards.value);
    if (!s || !k) { hintEl.textContent = ""; return; }
    const sum = (k * (k + 1)) / 2;
    hintEl.textContent = sum > s
      ? `Cards 1..${k} sum to ${sum}, more than ${s} stones, so the game always ends.`
      : `Cards 1..${k} only sum to ${sum}, less than ${s} stones. The rules assume the sum exceeds the pile.`;
  }
  form.elements.stones.addEventListener("input", updateHint);
  form.elements.cards.addEventListener("input", updateHint);
  updateHint();

  /* The QR code and address for joining from another device.  It encodes
     the lobby URL this page was opened with; when the page was opened on
     localhost it still works on this machine, so the printed server address
     is what to share with the room. */
  function drawQr(url) {
    const box = $("qr");
    const urlEl = $("qr-url");
    if (!box || !urlEl || !window.QR || box.dataset.url === url) return;
    urlEl.textContent = url;
    try { box.innerHTML = QR.svg(url, { ecl: "M", module: 4, margin: 2 }); box.dataset.url = url; }
    catch (e) { box.textContent = ""; }
  }
  drawQr(window.location.origin + "/");
  fetch("/api/health", { cache: "no-store" }).then((r) => r.json())
    .then((h) => { if (h.lobby_url) drawQr(h.lobby_url); })
    .catch(() => { /* keep the page's own address */ });

  /* The strategies list: sample clients under clients/ and the bots the
     server can seat, each with the file it lives in. */
  (async function drawStrategies() {
    const box = $("strategies");
    if (!box) return;
    try {
      const res = await fetch("/api/strategies", { cache: "no-store" });
      const st = await res.json();
      const cls = { "Python": "py", "C++": "cpp", "Java": "java" };
      box.innerHTML = (st.clients || []).map((c, i) => {
        const subs = (c.strategies || []).map((x) => `<div class="strat sub" title="${esc(x.note)}">
            <span class="lang ${/bot/i.test(x.note) ? "bot" : "fn"}"><svg><use href="#${/bot/i.test(x.note) ? "i-bot" : "i-code"}"/></svg></span>
            <span class="text"><span class="title">${esc(x.name)}</span>
            <span class="sub"><code>${esc(x.file)}</code> · ${esc(x.note)}</span></span></div>`).join("");
        const n = (c.strategies || []).length;
        return `<div class="strat-group" data-i="${i}">
          <button type="button" class="strat head" aria-expanded="false" title="${esc(c.run)}">
            <span class="lang ${cls[c.language] || ""}">${esc(c.language === "C++" ? "C++" : c.language.slice(0, 2))}</span>
            <span class="text"><span class="title">${esc(c.language)} client</span>
            <span class="sub"><code>${esc(c.file)}</code> · edit ${esc(c.edit.split("(")[0])}()${c.available ? " · in the Bot picker" : (c.reason ? " · " + esc(c.reason) : "")}</span></span>
            <span class="count">${n}</span>
            <svg class="chev"><use href="#i-chevron"/></svg>
          </button>
          <div class="strat-subs" hidden>${subs}</div>
        </div>`;
      }).join("") || '<div class="list-empty">No sample clients found.</div>';
      box.querySelectorAll(".strat.head").forEach((h) => h.addEventListener("click", () => {
        const group = h.closest(".strat-group");
        const open = group.classList.toggle("open");
        h.setAttribute("aria-expanded", open ? "true" : "false");
        group.querySelector(".strat-subs").hidden = !open;
      }));
    } catch (e) {
      box.innerHTML = '<div class="list-empty">Cannot list strategies.</div>';
    }
  })();

  /* ------------------------------------------------------------ filters */

  segment.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return;
    filter = btn.dataset.filter || "all";
    segment.querySelectorAll("button").forEach((b) => {
      const on = b === btn;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    lastSignature = null;
    render(lastGames);
  });

  search.addEventListener("input", () => {
    query = search.value.trim().toLowerCase();
    lastSignature = null;
    render(lastGames);
  });

  listEl.addEventListener("click", (ev) => {
    const row = ev.target.closest(".row");
    if (!row) return;
    ev.preventDefault();
    selectedId = row.dataset.id;
    lastSignature = null;
    render(lastGames);
  });

  /* ------------------------------------------------------------ helpers */

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* Purpose: a stable colour pair for a game id, so every table gets its own
     coloured tile without any images. */
  function blobStyle(id) {
    let h = 0;
    for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `--a:hsl(${h} 85% 66%);--b:hsl(${(h + 25) % 360} 75% 50%)`;
  }

  function fmtTime(epoch) {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    const sameDay = new Date().toDateString() === d.toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function statusText(g) {
    if (g.status === "waiting") {
      const seated = g.players.filter(Boolean).length;
      return seated === 0 ? "2 open seats" : "1 open seat";
    }
    if (g.status === "playing") return `Seat ${g.turn} to move`;
    if (g.winner) return `${g.players[g.winner - 1] || "Seat " + g.winner} won`;
    return "No winner";
  }

  function titleOf(g) { return g.label ? g.label : "Table " + g.id; }

  function matches(g) {
    if (filter !== "all" && g.status !== filter) return false;
    if (!query) return true;
    const hay = [g.id, g.label || "", ...g.players.filter(Boolean)].join(" ").toLowerCase();
    return hay.includes(query);
  }

  /* ------------------------------------------------------------ rendering */

  function rowHtml(g) {
    const names = g.players.map((n) => n || "open").join(" · ");
    const av = (g.avatars || []).find((a, i) => a && g.players[i]);
    const face = av
      ? `<span class="blob" style="${blobStyle(g.id)}"><img src="/avatars/av${String(av).padStart(2, "0")}.png" alt=""></span>`
      : `<span class="blob" style="${blobStyle(g.id)}"><svg class="table"><use href="#i-table"/></svg></span>`;
    return `<a class="row${g.id === selectedId ? " selected" : ""}" href="/game/${g.id}" data-id="${g.id}">
      ${face}
      <span class="text">
        <span class="title">${esc(titleOf(g))}</span>
        <span class="sub">${statusText(g)} · ${names ? esc(names) : ""}</span>
      </span>
      <span class="time">${fmtTime(g.finished_at || g.created_at)}</span>
    </a>`;
  }

  function detailHtml(g) {
    const pct = g.initial_stones ? Math.round((g.stones / g.initial_stones) * 100) : 0;
    const seats = [1, 2].map((n) => {
      const name = g.players[n - 1];
      const av = (g.avatars || [])[n - 1];
      const won = g.status === "finished" && g.winner === n;
      const onTurn = g.status === "playing" && g.turn === n;
      const face = name && av
        ? `<img class="seat-img s${n}" src="/avatars/av${String(av).padStart(2, "0")}.png" alt="">`
        : `<span class="seat-dot ${name ? "s" + n : "open"}">${n}</span>`;
      return `<div class="seat">
        ${face}
        <span class="who">
          <span class="name">${name ? esc(name) : "Open seat"}</span>
          <span class="hint">${name ? ((won ? "Winner" : onTurn ? "To move" : "Seated") + ((g.bots || [])[n - 1] ? " · bot" : "")) : "Anyone can sit here from the board"}</span>
        </span>
        ${won ? '<svg class="icon check"><use href="#i-trophy"/></svg>' : onTurn ? '<svg class="icon check"><use href="#i-play"/></svg>' : ""}
      </div>`;
    }).join("");
    const open = g.status === "waiting";
    return `
      <div class="detail-head">
        <span class="blob lg" style="${blobStyle(g.id)}"><svg class="table"><use href="#i-table"/></svg></span>
        <div class="text">
          <h1>${esc(titleOf(g))}</h1>
          <div class="sub"><span class="status ${g.status}"><span class="dot"></span>${statusText(g)}</span><span>Table ${g.id}</span><span>${open ? "Created" : g.status === "finished" ? "Finished" : "Started"} ${fmtTime(g.finished_at || g.created_at)}</span></div>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="label"><svg class="icon"><use href="#i-stones"/></svg>Stones</div><div class="value">${g.stones} <small>of ${g.initial_stones}</small></div><div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div></div>
        <div class="stat"><div class="label"><svg class="icon"><use href="#i-cards"/></svg>Cards</div><div class="value">1..${g.cards}</div></div>
        <div class="stat"><div class="label"><svg class="icon"><use href="#i-clock"/></svg>Clock</div><div class="value">${Math.round(g.time_limit)}<small>s each</small></div></div>
      </div>
      <div>
        <div class="group-title">Seats</div>
        <div class="seats">${seats}</div>
      </div>
      <div class="actions">
        <a class="btn blue" href="/game/${g.id}"><svg class="icon"><use href="#${open ? "i-person" : "i-eye"}"/></svg>${open ? "Join table" : "Watch table"}</a>
        <button class="btn" type="button" id="copy-link"><svg class="icon"><use href="#i-link"/></svg>Copy link</button>
        <span class="copied" id="copied"></span>
        <span class="secondary">${g.moves} move${g.moves === 1 ? "" : "s"} so far</span>
      </div>`;
  }

  /* Purpose: redraw the list and the detail pane.  Skipped when nothing
     changed, so a row is never rebuilt under the pointer mid-click. */
  let lastSignature = null;
  function render(games) {
    lastGames = games;
    const visible = games.filter(matches);
    if (selectedId && !games.some((g) => g.id === selectedId)) selectedId = null;
    if (!selectedId && visible.length) selectedId = visible[0].id;
    const signature = [filter, query, selectedId, JSON.stringify(games.map((g) => [g.id, g.version, g.status]))].join("|");
    if (signature === lastSignature) return;
    lastSignature = signature;

    countEl.textContent = `${games.length} table${games.length === 1 ? "" : "s"}`;
    listEl.innerHTML = visible.length
      ? visible.map(rowHtml).join("")
      : `<div class="list-empty">${games.length ? "No tables match." : "No tables yet. Create one on the right."}</div>`;

    const g = games.find((x) => x.id === selectedId);
    if (!g) {
      detailTitle.textContent = "Tables";
      detail.innerHTML = `<div class="detail-empty">
        <div class="blob lg" style="--a:#bfc3cc;--b:#8e929c"><svg class="table"><use href="#i-table"/></svg></div>
        <div>Select a table on the left, or create one on the right.</div></div>`;
      return;
    }
    detailTitle.textContent = titleOf(g);
    detail.innerHTML = detailHtml(g);
    const copy = $("copy-link");
    copy.addEventListener("click", async () => {
      const url = window.location.origin + "/game/" + g.id;
      try {
        await navigator.clipboard.writeText(url);
        $("copied").textContent = "Copied";
      } catch (e) {
        $("copied").textContent = url;           // clipboard blocked: show it instead
      }
      setTimeout(() => { const c = $("copied"); if (c) c.textContent = ""; }, 2500);
    });
  }

  /* Purpose: poll the list every two seconds (the lobby is low traffic). */
  async function refresh() {
    try {
      const res = await fetch("/api/games", { cache: "no-store" });
      if (!res.ok) throw new Error("server answered " + res.status);
      const payload = await res.json();
      render(payload.games || []);
      serverInfo.textContent = window.location.host;
    } catch (err) {
      lastSignature = null;
      listEl.innerHTML = `<div class="list-empty">Cannot reach the server (${esc(err.message)}). Retrying…</div>`;
    }
  }
  refresh();
  setInterval(refresh, 2000);
})();
