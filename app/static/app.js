// Guest page: join, search, request, and watch the queue update live.
let me = null;
let canSearch = false;

const isMine = (song) => !!me && song.singer_id === me.id;

function render(state) {
  renderTv(state.tv);
  renderNow(state.now_playing, state.now_playing && isMine(state.now_playing));

  const q = $("#queue");
  q.replaceChildren(...state.upcoming.map((s, i) => songRow(s, { num: i + 1, mine: isMine(s) })));
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
    btn.disabled = err.status === 403;
    msg.className = "msg err";
    msg.textContent = err.message;
    if (err.status === 401) { me = null; showJoined(); }
  }
}

loadState().catch(console.error);
liveUpdates(render, () => loadState().catch(() => {}));
// TV status is pushed too, but refresh now and then in case a push was missed.
setInterval(() => loadState().catch(() => {}), 30000);
