// Guest page: join, search, request, and watch the queue update live.
let me = null;
let canSearch = false;

const isMine = (song) => !!me && song.singer_id === me.id;

// --- "You're up next" alerts -------------------------------------------------

const PAGE_TITLE = document.title;
let lastAlert = null;
let turnInitialized = false; // first render after opening the page: show, don't buzz

async function systemNotify(text) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    new Notification("Karaoke", { body: text, tag: "kq-turn" });
  } catch {
    // Android Chrome only allows notifications from a service worker.
    const reg = await navigator.serviceWorker?.getRegistration("/static/");
    reg?.showNotification("Karaoke", { body: text, tag: "kq-turn" });
  }
}

function renderTurn(state) {
  const now = state.now_playing && isMine(state.now_playing) ? state.now_playing : null;
  const next = !now && state.upcoming[0] && isMine(state.upcoming[0]) ? state.upcoming[0] : null;
  const banner = $("#turn-banner");
  const song = now || next;
  const quiet = !turnInitialized;
  if (me) turnInitialized = true; // the first render may come before we know who "me" is
  if (!song) {
    banner.hidden = true;
    document.title = PAGE_TITLE;
    lastAlert = null;
    return;
  }
  const text = now ? `🎤 You're on! "${song.title}"` : `🎤 You're up next: "${song.title}". Get the mic ready!`;
  banner.textContent = text;
  banner.className = "turn-banner" + (now ? " on" : "");
  banner.hidden = false;
  document.title = now ? "🎤 You're on!" : "🎤 You're next!";

  const key = `${song.id}:${now ? "now" : "next"}`;
  if (key === lastAlert) return;
  lastAlert = key;
  if (quiet) return; // don't buzz just for opening the page
  navigator.vibrate?.(now ? [300, 100, 300] : [200, 100, 200]);
  if (document.hidden) systemNotify(text);
}

function updateAlertsButton() {
  const btn = $("#alerts-btn");
  // Needs HTTPS (the tunnel link); iPhones only allow it for home-screen web apps.
  btn.hidden = !(me && window.isSecureContext && "Notification" in window && Notification.permission === "default");
}

$("#alerts-btn").addEventListener("click", async () => {
  await navigator.serviceWorker?.register("/static/sw.js").catch(() => {});
  await Notification.requestPermission();
  updateAlertsButton();
});

function render(state) {
  renderTv(state.tv);
  renderTurn(state);
  renderNotices(state.notices, (n) => isMine(n));
  renderNow(state.now_playing, state.now_playing && isMine(state.now_playing));

  const q = $("#queue");
  q.replaceChildren(
    ...state.upcoming.map((s, i) =>
      songRow(s, { num: i + 1, mine: isMine(s), extra: isMine(s) ? removeButton(s) : null }),
    ),
  );
  if (!state.upcoming.length) q.append(el("li", { class: "item empty" }, "Queue is empty. Add a song!"));
  $("#count").textContent = state.upcoming.length ? `(${state.upcoming.length})` : "";

  const limit = state.max_waiting;
  $("#closed").hidden = state.party_open !== false;
  $("#rules").textContent =
    (state.fair_turns ? "Everyone takes turns." : "Songs play in the order they're added.") +
    (limit ? ` Up to ${limit} song${limit === 1 ? "" : "s"} waiting per person.` : "");
}

function removeButton(song) {
  return el("button", {
    class: "small danger",
    title: "Remove my song",
    onclick: async () => {
      if (!confirm(`Remove "${song.title}" from the queue?`)) return;
      try {
        await api(`/api/requests/${song.id}`, { method: "DELETE" });
      } catch (err) {
        $("#search-msg").className = "msg err";
        $("#search-msg").textContent = err.message;
      }
    },
  }, "✕");
}

function showJoined() {
  updateAlertsButton();
  $("#join").hidden = !!me;
  $("#add").hidden = !me;
  $("#me").textContent = me ? `as ${me.name}` : "";
  $("#q").placeholder = canSearch ? "Song or artist" : "Paste a YouTube link";
  $("#mode-toggle").hidden = !canSearch;
}

async function loadState() {
  let state;
  try {
    state = await api("/api/state");
  } catch (err) {
    if (err.status === 403) lostPartyAccess();
    throw err;
  }
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

// Karaoke (backing track, lyrics on screen) or Lyrics (original vocals + lyrics).
let searchMode = localStorage.getItem("kq_search_mode") === "lyrics" ? "lyrics" : "karaoke";

function renderMode() {
  for (const b of document.querySelectorAll("#mode-toggle button")) {
    b.setAttribute("aria-checked", String(b.dataset.mode === searchMode));
  }
}

for (const b of document.querySelectorAll("#mode-toggle button")) {
  b.addEventListener("click", () => {
    if (b.dataset.mode === searchMode) return;
    searchMode = b.dataset.mode;
    localStorage.setItem("kq_search_mode", searchMode);
    renderMode();
    // Re-run the current search in the new mode.
    if ($("#q").value.trim() && $("#results").children.length) $("#search-form").requestSubmit();
  });
}
renderMode();

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#search-msg");
  const list = $("#results");
  msg.className = "msg";
  msg.textContent = "Searching…";
  list.replaceChildren();
  try {
    const { results } = await api(
      `/api/search?mode=${searchMode}&q=` + encodeURIComponent($("#q").value),
    );
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
    const added = await api("/api/requests", { method: "POST", body: JSON.stringify({ video_id: video.video_id }) });
    btn.textContent = "Added ✓";
    msg.className = "msg";
    msg.textContent = `Added "${video.title}". You're #${added.place} in line.`;
  } catch (err) {
    btn.disabled = err.status === 403;
    msg.className = "msg err";
    msg.textContent = err.message;
    if (err.status === 401) { me = null; showJoined(); }
  }
}

$("#invite-btn").addEventListener("click", showInvite);

loadState().catch(console.error);
liveUpdates(render, () => loadState().catch(() => {}));
// TV status is pushed too, but refresh now and then in case a push was missed.
setInterval(() => loadState().catch(() => {}), 30000);
