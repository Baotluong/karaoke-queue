// Shared helpers for the guest and admin pages.
const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function songRow(song, { num, extra, mine } = {}) {
  return el(
    "li",
    { class: "item" + (mine ? " mine" : "") },
    num != null ? el("span", { class: "num" }, String(num)) : null,
    song.thumbnail ? el("img", { src: song.thumbnail, alt: "", loading: "lazy" }) : null,
    el(
      "div",
      { class: "meta" },
      el("div", { class: "title" }, song.title),
      el("div", { class: "sub" }, song.singer_name ? `🎤 ${song.singer_name}${mine ? " (you)" : ""}` : song.channel || ""),
    ),
    extra || null,
  );
}

function renderTv(tv = {}) {
  const pill = $("#tv-status");
  pill.textContent = tv.ready ? `TV: ${tv.name || "connected"}${tv.paused ? " (paused)" : ""}` : "TV offline";
  pill.className = "pill " + (tv.ready ? "ok" : "bad");
  pill.title = tv.error || "";
}

function renderNow(song, mine) {
  const now = $("#now-card");
  now.replaceChildren();
  if (song) {
    now.className = "";
    now.append(...songRow(song, { mine }).childNodes);
  } else {
    now.className = "empty";
    now.textContent = "Nothing playing yet";
  }
}

// Calls onMessage(state) for every queue update; reconnects if the server restarts.
function liveUpdates(onMessage, onReconnect) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  const ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => {
    clearInterval(ping);
    setTimeout(() => { onReconnect && onReconnect(); liveUpdates(onMessage, onReconnect); }, 2000);
  };
}
