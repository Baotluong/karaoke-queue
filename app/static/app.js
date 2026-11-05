// Guest page: join, search, request, and watch the queue update live.
const $ = (sel) => document.querySelector(sel);
let me = null;
let canSearch = false;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
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

function songRow(song, { num, extra } = {}) {
  const mine = me && song.singer_id === me.id;
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

function render(state) {
  const tv = state.tv || {};
  const pill = $("#tv-status");
  pill.textContent = tv.ready ? `TV: ${tv.name || "connected"}` : "TV offline";
  pill.className = "pill " + (tv.ready ? "ok" : "bad");
  pill.title = tv.error || "";

  const now = $("#now-card");
  now.replaceChildren();
  if (state.now_playing) {
    now.className = "";
    now.append(...songRow(state.now_playing).childNodes);
  } else {
    now.className = "empty";
    now.textContent = "Nothing playing yet";
  }

  const q = $("#queue");
  q.replaceChildren(...state.upcoming.map((s, i) => songRow(s, { num: i + 1 })));
  if (!state.upcoming.length) q.append(el("li", { class: "item empty" }, "Queue is empty. Add a song!"));
  $("#count").textContent = state.upcoming.length ? `(${state.upcoming.length})` : "";
}

function showJoined() {
  $("#join").hidden = !!me;
  $("#add").hidden = !me;
  $("#me").textContent = me ? `as ${me.name}` : "";
  $("#q").placeholder = canSearch ? "Song or artist" : "Paste a YouTube link";
}

async function loadState() {
  const state = await api("/api/state");
  me = state.me;
  canSearch = state.can_search;
  showJoined();
  render(state);
}

$("#join-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  me = await api("/api/join", { method: "POST", body: JSON.stringify({ name: $("#name").value }) });
  await loadState();
});

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#search-msg");
  const list = $("#results");
  msg.className = "msg";
  msg.textContent = "Searching…";
  list.replaceChildren();
  try {
    const { results } = await api("/api/search?q=" + encodeURIComponent($("#q").value));
    msg.textContent = results.length ? "" : "No results.";
    list.append(
      ...results.map((v) => {
        const btn = el("button", { class: "small" }, "Add");
        btn.addEventListener("click", () => addSong(v, btn));
        return songRow(v, { extra: btn });
      }),
    );
  } catch (err) {
    msg.className = "msg err";
    msg.textContent = err.message;
  }
});

async function addSong(video, btn) {
  btn.disabled = true;
  const msg = $("#search-msg");
  try {
    await api("/api/requests", { method: "POST", body: JSON.stringify({ video_id: video.video_id }) });
    btn.textContent = "Added ✓";
    msg.className = "msg";
    msg.textContent = `Added "${video.title}"`;
  } catch (err) {
    btn.disabled = false;
    msg.className = "msg err";
    msg.textContent = err.message;
    if (err.message.includes("name")) { me = null; showJoined(); }
  }
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  const ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
  ws.onmessage = (e) => render(JSON.parse(e.data));
  ws.onclose = () => {
    clearInterval(ping);
    setTimeout(() => { loadState().catch(() => {}); connect(); }, 2000);
  };
}

loadState().catch(console.error);
connect();
// TV status isn't pushed on its own, so refresh it now and then.
setInterval(() => loadState().catch(() => {}), 15000);
