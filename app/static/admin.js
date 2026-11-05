// Admin page: skip, pause, reorder, remove, play now, and block singers.
let loggedIn = false;

function showLoggedIn(yes) {
  loggedIn = yes;
  $("#login").hidden = yes;
  $("#panel").hidden = !yes;
  $("#logout-wrap").hidden = !yes;
  $("#invite-btn").hidden = !yes;
}

function flash(text, isErr = false) {
  const msg = $("#msg");
  msg.className = "msg" + (isErr ? " err" : "");
  msg.textContent = text;
}

// Run an admin action; the WebSocket push re-renders afterwards.
async function act(path, opts = {}) {
  try {
    await api(path, { method: "POST", ...opts });
    flash("");
    return true;
  } catch (err) {
    if (err.status === 401) showLoggedIn(false);
    else flash(err.message, true);
    return false;
  }
}

function controls(song, index, total) {
  const btn = (label, title, onclick, cls = "") => el("button", { title, class: cls, onclick }, label);
  const move = (to) => () => act(`/api/admin/requests/${song.id}/move`, { body: JSON.stringify({ index: to }) });
  return el(
    "div",
    { class: "controls" },
    btn("▶ Now", "Play now", () => act(`/api/admin/requests/${song.id}/play-now`), "go"),
    index > 0 ? btn("⤒", "Move to top", move(0)) : null,
    index > 0 ? btn("▲", "Move up", move(index - 1)) : null,
    index < total - 1 ? btn("▼", "Move down", move(index + 1)) : null,
    btn("✕", "Remove", () => {
      if (confirm(`Remove "${song.title}" (${song.singer_name})?`)) {
        act(`/api/admin/requests/${song.id}`, { method: "DELETE" });
      }
    }, "danger"),
  );
}

function render(state) {
  renderTv(state.tv);
  renderNow(state.now_playing);
  const paused = !!(state.tv && state.tv.paused);
  $("#pause").hidden = paused;
  $("#resume").hidden = !paused;
  $("#skip").disabled = !state.now_playing;

  const q = $("#queue");
  const total = state.upcoming.length;
  q.replaceChildren(
    ...state.upcoming.map((s, i) => {
      const row = songRow(s, { num: i + 1 });
      row.classList.add("admin");
      row.append(controls(s, i, total));
      return row;
    }),
  );
  if (!total) q.append(el("li", { class: "item empty" }, "Queue is empty"));
  $("#count").textContent = total ? `(${total})` : "";

  if (state.singers) renderSingers(state.singers);
  $("#fair-turns").checked = !!state.fair_turns;
  $("#party-open").checked = state.party_open !== false;
  const input = $("#max-waiting");
  if (document.activeElement !== input) input.value = state.max_waiting;
}

function renderSingers(singers) {
  const box = $("#singers");
  box.replaceChildren(
    ...singers.map((s) =>
      el(
        "div",
        { class: "singer" + (s.blocked ? " blocked" : "") },
        el(
          "div",
          { class: "meta" },
          el("div", { class: "title" }, s.name),
          el("div", { class: "sub" }, `${s.queued} waiting · ${s.sung} sung`),
        ),
        el(
          "button",
          {
            class: s.blocked ? "" : "danger",
            onclick: () => {
              if (s.blocked || confirm(`Block ${s.name}? Their waiting songs will be removed.`)) {
                act(`/api/admin/singers/${s.id}/block`, { body: JSON.stringify({ blocked: !s.blocked }) });
              }
            },
          },
          s.blocked ? "Unblock" : "Block",
        ),
      ),
    ),
  );
  if (!singers.length) box.append(el("div", { class: "item empty" }, "No one has joined yet"));
}

function renderParty(p) {
  $("#join-url").textContent = p.join_url;
  $("#party-open").checked = p.open;
  $("#local-warning").hidden = p.public;
}

async function loadParty() {
  renderParty(await api("/api/admin/party"));
}

async function load() {
  try {
    render(await api("/api/admin/state"));
    if (!loggedIn) await loadParty();
    showLoggedIn(true);
  } catch (err) {
    if (err.status === 401) showLoggedIn(false);
    else throw err;
  }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/login", { method: "POST", body: JSON.stringify({ pin: $("#pin").value }) });
    $("#pin").value = "";
    $("#login-msg").textContent = "";
    await load();
  } catch (err) {
    $("#login-msg").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async (e) => {
  e.preventDefault();
  await api("/api/admin/logout", { method: "POST" });
  showLoggedIn(false);
});

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await act("/api/admin/settings", { body: JSON.stringify({ max_waiting: Number($("#max-waiting").value) }) })) {
    $("#max-waiting").blur();
    flash("Saved");
  }
});

$("#fair-turns").addEventListener("change", async (e) => {
  if (await act("/api/admin/settings", { body: JSON.stringify({ fair_turns: e.target.checked }) })) {
    flash(e.target.checked ? "Fair turns on" : "Fair turns off: new songs go to the bottom");
  }
});

$("#invite-btn").addEventListener("click", showInvite);
$("#show-qr").addEventListener("click", showInvite);

$("#new-link").addEventListener("click", async () => {
  if (!confirm("Make a new party link? Everyone will need to scan the new QR code to keep using the queue.")) return;
  try {
    renderParty(await api("/api/admin/party", { method: "POST", body: JSON.stringify({ new_link: true }) }));
    flash("New link ready.");
    showInvite();
  } catch (err) {
    flash(err.message, true);
  }
});

$("#party-open").addEventListener("change", async (e) => {
  try {
    renderParty(await api("/api/admin/party", { method: "POST", body: JSON.stringify({ open: e.target.checked }) }));
    flash(e.target.checked ? "Open to new songs" : "Closed: no new singers or songs");
  } catch (err) {
    flash(err.message, true);
  }
});

$("#pause").addEventListener("click", () => act("/api/admin/pause"));
$("#resume").addEventListener("click", () => act("/api/admin/resume"));
$("#skip").addEventListener("click", () => {
  if (confirm("Skip the current song?")) act("/api/admin/skip");
});

load().catch(console.error);
// Any queue change: re-fetch the admin view (it includes the singers list).
liveUpdates(() => loggedIn && load().catch(() => {}), () => load().catch(() => {}));
