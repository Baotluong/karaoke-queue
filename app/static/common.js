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

// The party link changed (or never scanned): show the "scan the QR code" page.
function lostPartyAccess() {
  location.replace("/?expired=1");
}

// Calls onMessage(state) for every queue update; reconnects if the server restarts.
function liveUpdates(onMessage, onReconnect, onKicked = lostPartyAccess) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  const ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = (e) => {
    clearInterval(ping);
    if (e.code === 4403) return onKicked();
    setTimeout(() => { onReconnect && onReconnect(); liveUpdates(onMessage, onReconnect, onKicked); }, 2000);
  };
}

// --- invite pop-up: a big QR code anyone at the party can show to others -----

function inviteDialog() {
  let dlg = $("#invite");
  if (dlg) return dlg;
  dlg = el(
    "dialog",
    { id: "invite", class: "invite" },
    el("h2", {}, "Scan to join the party"),
    el("img", { id: "invite-qr", class: "qr", alt: "QR code to join the party" }),
    el("div", { id: "invite-url", class: "join-url" }),
    el(
      "div",
      { class: "row", style: "justify-content: center" },
      el("button", { id: "invite-copy" }, "Copy link"),
      navigator.share ? el("button", { id: "invite-share" }, "Share…") : null,
      el("button", { class: "go", onclick: () => dlg.close() }, "Done"),
    ),
    el("p", { id: "invite-msg", class: "msg" }),
  );
  // Tapping the dark area around the box closes it.
  dlg.addEventListener("click", (e) => e.target === dlg && dlg.close());
  document.body.append(dlg);

  $("#invite-copy").addEventListener("click", async () => {
    const url = $("#invite-url").textContent;
    try {
      await navigator.clipboard.writeText(url); // needs HTTPS or localhost
      $("#invite-msg").textContent = "Link copied";
    } catch {
      prompt("Copy this link:", url);
    }
  });
  if (navigator.share) {
    $("#invite-share").addEventListener("click", () =>
      navigator.share({ title: "Join the karaoke party", url: $("#invite-url").textContent }).catch(() => {}),
    );
  }
  return dlg;
}

async function showInvite() {
  const dlg = inviteDialog();
  try {
    const { join_url } = await api("/api/party"); // fresh, in case an admin made a new link
    $("#invite-qr").src = "/api/qr.svg?u=" + encodeURIComponent(join_url);
    $("#invite-url").textContent = join_url;
    $("#invite-msg").textContent = "";
    dlg.showModal();
  } catch (err) {
    if (err.status === 403) lostPartyAccess();
  }
}
