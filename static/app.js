const COLORS = ["#e0552b", "#2b7de0", "#2ba85a", "#a02be0", "#e0b02b", "#2bd0d0", "#e02b6a", "#7a8a2b"];
const MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];

const VALID_TABS = ["trips", "tags", "ungrouped"];
const savedTab = localStorage.getItem("activeTab");

const state = {
  tab: VALID_TABS.includes(savedTab) ? savedTab : "trips",
  roadtrips: [],
  ungrouped: [],
  tags: [],
  activeTripId: null,
  activeTagId: null,
  activeEntityKind: null, // "trip" or "tag" — tracks what /api/roadtrips|tags/{id} is currently shown in #main
  selected: new Set(),
  map: null,
  rideModalMap: null,
  rideModalId: null,
  showAll: localStorage.getItem("showAll") === "1",
  collapsedYears: new Set(),
  collapsedTripYears: new Set(),
};

async function api(path, opts) {
  const resp = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status}: ${text}`);
  }
  return resp.status === 204 ? null : resp.json();
}

function fmtKm(m) {
  return ((m || 0) / 1000).toFixed(1) + " km";
}
function fmtDuration(s) {
  s = s || 0;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return h > 0 ? `${h}h${String(m).padStart(2, "0")}` : `${m}min`;
}
function fmtDate(d) {
  return new Date(d).toLocaleString("fr-FR", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtDay(d) {
  return new Date(d).toLocaleDateString("fr-FR", { weekday: "long", day: "2-digit", month: "long", year: "numeric" });
}
function fmtTime(d) {
  return new Date(d).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}
function fmtAlt(m) {
  return m == null ? "–" : Math.round(m) + " m";
}
function fmtAvgSpeed(distanceM, movingS) {
  if (!movingS) return "–";
  return Math.round((distanceM / 1000) / (movingS / 3600)) + " km/h";
}
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- auth ---
// The whole app is gated behind a session: #authScreen (intro + login form)
// is the only thing shown until /api/auth/status confirms a session, at
// which point #app (and only then) fetches and renders any personal data.
document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = document.getElementById("loginEmail").value.trim();
  const password = document.getElementById("loginPassword").value;
  const errorEl = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");
  btn.disabled = true;
  errorEl.textContent = "";
  try {
    const result = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    document.getElementById("loginPassword").value = "";
    await enterApp(result.first_name);
  } catch (e) {
    errorEl.textContent = "Connexion impossible : " + e.message;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("logoutBtn").addEventListener("click", async () => {
  document.getElementById("userMenu").classList.remove("open");
  await api("/api/auth/logout", { method: "POST" });
  document.getElementById("app").style.display = "none";
  document.getElementById("authScreen").style.display = "flex";
});

document.getElementById("userMenuBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  document.getElementById("userMenu").classList.toggle("open");
});
document.addEventListener("click", (e) => {
  const menu = document.getElementById("userMenu");
  if (!menu.contains(e.target) && e.target.id !== "userMenuBtn") menu.classList.remove("open");
});

async function enterApp(firstName) {
  document.getElementById("authScreen").style.display = "none";
  document.getElementById("app").style.display = "flex";
  setProfileGreeting(firstName);
  switchTab(state.tab);
  await refresh();
  refreshProfile();
}

function setProfileGreeting(firstName) {
  document.getElementById("userAvatar").textContent = (firstName || "?").charAt(0).toUpperCase();
  document.getElementById("userName").textContent = firstName || "Connecté(e)";
}

async function refreshProfile() {
  try {
    const profile = await api("/api/auth/profile");
    if (profile.first_name) {
      document.getElementById("userAvatar").textContent = profile.first_name.charAt(0).toUpperCase();
      document.getElementById("userName").textContent = profile.first_name;
    }
    document.getElementById("userSub").textContent =
      `${profile.manual_ride_count ?? "?"} trajets Liberty Rider`;
  } catch (e) {
    // Non-fatal — the greeting set at login is still shown.
  }
}

// --- sync ---
document.getElementById("syncBtn").addEventListener("click", () => doSync(false));
document.getElementById("syncFullBtn").addEventListener("click", () => {
  document.getElementById("userMenu").classList.remove("open");
  doSync(true);
});

async function doSync(full) {
  const statusEl = document.getElementById("syncstatus");
  statusEl.textContent = full ? "Synchronisation complète en cours…" : "Synchronisation en cours…";
  document.getElementById("syncBtn").disabled = true;
  document.getElementById("syncFullBtn").disabled = true;
  try {
    const summary = await api("/api/sync", { method: "POST", body: JSON.stringify({ full }) });
    statusEl.textContent = `${summary.upserted} nouveau(x) / ${summary.total_rides} au total.`;
    await refresh();
  } catch (e) {
    statusEl.textContent = "Erreur : " + e.message;
  } finally {
    document.getElementById("syncBtn").disabled = false;
    document.getElementById("syncFullBtn").disabled = false;
  }
}

// --- tabs ---
document.getElementById("tabTrips").addEventListener("click", () => switchTab("trips"));
document.getElementById("tabUngrouped").addEventListener("click", () => switchTab("ungrouped"));
document.getElementById("tabTags").addEventListener("click", () => switchTab("tags"));

function switchTab(tab) {
  state.tab = tab;
  localStorage.setItem("activeTab", tab);
  document.getElementById("tabTrips").classList.toggle("active", tab === "trips");
  document.getElementById("tabUngrouped").classList.toggle("active", tab === "ungrouped");
  document.getElementById("tabTags").classList.toggle("active", tab === "tags");
  document.getElementById("listtoolbar").style.display = tab === "ungrouped" ? "flex" : "none";
  document.getElementById("selectionbar").style.display = tab === "ungrouped" ? "flex" : "none";
  document.getElementById("tagshint").style.display = tab === "tags" ? "block" : "none";
  state.selected.clear();
  renderList();
}

// --- show grouped/tagged toggle ---
const showAllToggle = document.getElementById("showAllToggle");
showAllToggle.checked = state.showAll;
showAllToggle.addEventListener("change", (e) => {
  state.showAll = e.target.checked;
  localStorage.setItem("showAll", state.showAll ? "1" : "0");
  renderList();
});

async function refresh() {
  state.roadtrips = await api("/api/roadtrips");
  state.ungrouped = await api("/api/rides");
  state.tags = await api("/api/tags");
  renderList();
  if (state.activeEntityKind === "trip" && state.activeTripId) showTripDetail(state.activeTripId);
  else if (state.activeEntityKind === "tag" && state.activeTagId) showTagDetail(state.activeTagId);
}

function renderList() {
  const wrap = document.getElementById("listwrap");
  wrap.innerHTML = "";
  if (state.tab === "trips") {
    if (!state.roadtrips.length) {
      wrap.innerHTML = '<div class="section">Aucun roadtrip pour l\'instant.</div>';
      updateSelectionBar();
      return;
    }
    const sortedTrips = [...state.roadtrips].sort((a, b) => (b.start_date || "").localeCompare(a.start_date || ""));
    renderYearMonthGroups(wrap, sortedTrips, (t) => t.start_date, renderTripRow, state.collapsedTripYears, toggleTripYear);
  } else if (state.tab === "ungrouped") {
    const visible = state.showAll
      ? state.ungrouped
      : state.ungrouped.filter((r) => !r.roadtrip_id && !(r.tags && r.tags.length));
    if (!visible.length) {
      wrap.innerHTML = state.showAll
        ? '<div class="section">Aucun trajet.</div>'
        : '<div class="section">Rien à regrouper — tout est déjà rangé ou taggué. Coche « Afficher les groupés/taggués » pour tout voir.</div>';
      updateSelectionBar();
      return;
    }
    renderYearMonthGroups(wrap, visible, (r) => r.start_time, renderUngroupedRow, state.collapsedYears, toggleYear);
  } else {
    if (!state.tags.length) {
      wrap.innerHTML = '<div class="section">Aucun tag pour l\'instant.</div>';
      updateSelectionBar();
      return;
    }
    const sortedTags = [...state.tags].sort((a, b) => a.name.localeCompare(b.name));
    for (const t of sortedTags) wrap.appendChild(renderTagRow(t));
  }
  updateSelectionBar();
}

function renderTripRow(t) {
  const row = document.createElement("div");
  row.className = "trip-row" + (state.activeEntityKind === "trip" && t.id === state.activeTripId ? " active" : "");
  row.innerHTML = `
    <div class="name">${escapeHtml(t.name)}</div>
    <div class="meta">${t.start_date || "?"} → ${t.end_date || "?"} · ${t.day_count} j · ${t.ride_count} étapes · ${fmtKm(t.total_distance)} · ${fmtDuration(t.total_duration)}</div>
  `;
  row.addEventListener("click", () => {
    state.activeTripId = t.id;
    state.activeEntityKind = "trip";
    renderList();
    showTripDetail(t.id);
  });
  return row;
}

function renderTagRow(t) {
  const row = document.createElement("div");
  row.className = "tag-row" + (state.activeEntityKind === "tag" && t.id === state.activeTagId ? " active" : "");
  row.innerHTML = `
    <div class="name">🏷️ ${escapeHtml(t.name)}</div>
    <div class="meta">${t.start_date || "?"} → ${t.end_date || "?"} · ${t.day_count} j parcourus · ${t.ride_count} trajets · ${fmtKm(t.total_distance)}</div>
  `;
  row.addEventListener("click", () => {
    state.activeTagId = t.id;
    state.activeEntityKind = "tag";
    renderList();
    showTagDetail(t.id);
  });
  return row;
}

// Groups `items` into sticky year headers + month sub-headers, using
// `getDate(item)` (an ISO date/datetime string) to bucket them.
function renderYearMonthGroups(wrap, items, getDate, renderRow, collapsedSet, onToggleYear) {
  const yearCounts = new Map();
  const monthCounts = new Map();
  for (const item of items) {
    const d = new Date(getDate(item));
    const year = d.getFullYear();
    const monthKey = `${year}-${d.getMonth()}`;
    yearCounts.set(year, (yearCounts.get(year) || 0) + 1);
    monthCounts.set(monthKey, (monthCounts.get(monthKey) || 0) + 1);
  }

  let lastYear = null, lastMonth = null, yearBody = null;
  for (const item of items) {
    const d = new Date(getDate(item));
    const year = d.getFullYear();
    const month = d.getMonth();
    if (year !== lastYear) {
      const collapsed = collapsedSet.has(year);
      const yh = document.createElement("div");
      yh.className = "year-header";
      yh.innerHTML = `<span class="chevron">${collapsed ? "▸" : "▾"}</span> ${year} `
        + `<span class="count">(${yearCounts.get(year)})</span>`;
      yh.addEventListener("click", () => onToggleYear(year));
      wrap.appendChild(yh);
      yearBody = document.createElement("div");
      yearBody.className = "year-body";
      yearBody.style.display = collapsed ? "none" : "";
      wrap.appendChild(yearBody);
      lastYear = year;
      lastMonth = null;
    }
    if (month !== lastMonth) {
      const mh = document.createElement("div");
      mh.className = "month-header";
      mh.innerHTML = `${MONTHS_FR[month]} <span class="count">(${monthCounts.get(`${year}-${month}`)})</span>`;
      yearBody.appendChild(mh);
      lastMonth = month;
    }
    yearBody.appendChild(renderRow(item));
  }
}

function toggleYear(year) {
  if (state.collapsedYears.has(year)) state.collapsedYears.delete(year);
  else state.collapsedYears.add(year);
  renderList();
}

function toggleTripYear(year) {
  if (state.collapsedTripYears.has(year)) state.collapsedTripYears.delete(year);
  else state.collapsedTripYears.add(year);
  renderList();
}

const MAX_ROW_TAGS = 3;

function renderUngroupedRow(r) {
  const row = document.createElement("div");
  const isHandled = Boolean(r.roadtrip_id) || Boolean(r.tags && r.tags.length);
  const isMerge = (r.merge_ride_ids || []).length > 1;
  row.className = "ride-row" + (isHandled ? " handled" : "");
  const badges = [];
  // List is sorted newest → oldest: the earlier ride ("précédent") sits BELOW
  // this row, the later ride ("suivant") sits ABOVE — arrows point there.
  if (r.suggested_link_prev) badges.push('<span class="badge">↓ suite du précédent</span>');
  if (r.suggested_link_next) badges.push('<span class="badge">↑ enchaîne sur le suivant</span>');
  if (isMerge) badges.push(`<span class="badge merge-badge">🔗 fusion de ${r.merge_ride_ids.length} trajets</span>`);
  const tags = r.tags || [];
  const tagChips = tags.slice(0, MAX_ROW_TAGS).map((t) => `<span class="tag-chip small">${escapeHtml(t.name)}</span>`).join("");
  const extraTags = tags.length > MAX_ROW_TAGS ? `<span class="tag-chip small">+${tags.length - MAX_ROW_TAGS}</span>` : "";
  if (r.roadtrip_id) {
    const parentTrip = state.roadtrips.find((t) => t.id === r.roadtrip_id);
    if (parentTrip) badges.push(`<span class="badge">${escapeHtml(parentTrip.name)}</span>`);
  }
  const mergeTargetId = r.merge_candidate_prev_id || r.merge_candidate_next_id;
  const mergeDirection = r.merge_candidate_prev_id ? "↓" : "↑";
  row.innerHTML = `
    <input type="checkbox" ${state.selected.has(r.id) ? "checked" : ""} />
    ${r.preview_picture_url ? `<img class="ride-thumb" loading="lazy" src="${r.preview_picture_url}" alt="" />` : ""}
    <div class="ride-body">
      <div class="name">${escapeHtml(r.name || fmtDate(r.start_time))}</div>
      <div class="meta">${fmtDate(r.start_time)} → ${fmtTime(new Date(new Date(r.start_time).getTime() + (r.duration || 0) * 1000))} · ${fmtKm(r.distance)} · ${fmtDuration(r.duration)}</div>
      ${tags.length ? `<div class="row-tags">${tagChips}${extraTags}</div>` : ""}
      <div class="badges">${badges.join("")}</div>
      ${mergeTargetId ? `<button class="merge-suggest-btn" data-target="${mergeTargetId}">${mergeDirection} Même trajet ? Fusionner</button>` : ""}
    </div>
    <div class="ride-actions">
      <select data-ride="${r.id}">
        <option value="">+ Ajouter à…</option>
        ${state.roadtrips.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("")}
      </select>
    </div>
  `;
  row.querySelector("input[type=checkbox]").addEventListener("change", (e) => {
    e.stopPropagation();
    if (e.target.checked) state.selected.add(r.id); else state.selected.delete(r.id);
    updateSelectionBar();
  });
  row.querySelector("input[type=checkbox]").addEventListener("click", (e) => e.stopPropagation());
  row.querySelector("select").addEventListener("click", (e) => e.stopPropagation());
  row.querySelector("select").addEventListener("change", async (e) => {
    const tripId = e.target.value;
    if (!tripId) return;
    await api(`/api/roadtrips/${tripId}/rides`, { method: "POST", body: JSON.stringify({ ride_ids: [r.id] }) });
    await refresh();
  });
  const mergeBtn = row.querySelector(".merge-suggest-btn");
  if (mergeBtn) {
    mergeBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api("/api/rides/merge", {
        method: "POST",
        body: JSON.stringify({ ride_ids: [r.id, mergeBtn.dataset.target] }),
      });
      await refresh();
    });
  }
  row.addEventListener("click", () => openRideModal(r.id));
  return row;
}

function updateSelectionBar() {
  const countEl = document.getElementById("selcount");
  const createBtn = document.getElementById("createTripBtn");
  const mergeBtn = document.getElementById("mergeSelectionBtn");
  const nameInput = document.getElementById("newTripName");
  if (state.selected.size > 0) {
    countEl.textContent = `${state.selected.size} sélectionné(s)`;
  } else {
    countEl.textContent = "Coche des trajets ci-dessus pour les regrouper.";
  }
  createBtn.disabled = state.selected.size === 0 || !nameInput.value.trim();
  mergeBtn.disabled = state.selected.size < 2;
}

document.getElementById("newTripName").addEventListener("input", updateSelectionBar);

document.getElementById("createTripBtn").addEventListener("click", async () => {
  const name = document.getElementById("newTripName").value.trim();
  if (!name || state.selected.size === 0) return;
  await api("/api/roadtrips", {
    method: "POST",
    body: JSON.stringify({ name, ride_ids: Array.from(state.selected) }),
  });
  document.getElementById("newTripName").value = "";
  state.selected.clear();
  switchTab("trips");
  await refresh();
});

document.getElementById("mergeSelectionBtn").addEventListener("click", async () => {
  if (state.selected.size < 2) return;
  try {
    await api("/api/rides/merge", {
      method: "POST",
      body: JSON.stringify({ ride_ids: Array.from(state.selected) }),
    });
    state.selected.clear();
    await refresh();
  } catch (e) {
    alert("Impossible de fusionner : " + e.message);
  }
});

// --- ride detail modal ---
const rideModalBackdrop = document.getElementById("rideModalBackdrop");
document.getElementById("rideModalClose").addEventListener("click", closeRideModal);
rideModalBackdrop.addEventListener("click", (e) => {
  if (e.target === rideModalBackdrop) closeRideModal();
});

function closeRideModal() {
  rideModalBackdrop.classList.remove("visible");
  if (state.rideModalMap) {
    state.rideModalMap.remove();
    state.rideModalMap = null;
  }
}

async function openRideModal(id) {
  const ride = await api(`/api/rides/${id}`);
  state.rideModalId = ride.id;
  document.getElementById("rideModalTitle").textContent = ride.name || fmtDate(ride.start_time);
  const endTime = new Date(new Date(ride.start_time).getTime() + (ride.duration || 0) * 1000);
  document.getElementById("rideModalSubtitle").textContent =
    `${fmtDay(ride.start_time)} · Départ ${fmtTime(ride.start_time)} → Arrivée ${fmtTime(endTime)}`;
  // Grouped by proximity to the chart each set of numbers explains: time
  // breakdown right above the trajet/pause timeline, distance/altitude
  // right above the elevation profile.
  document.getElementById("rideModalStats").innerHTML = `
    <div class="stat"><div class="v">${fmtDuration(ride.duration)}</div><div class="l">Durée totale</div></div>
    <div class="stat"><div class="v">${fmtDuration(ride.duration_without_pauses)}</div><div class="l">À moto</div></div>
    <div class="stat"><div class="v">${fmtDuration(ride.total_pauses_duration)}</div><div class="l">En pause</div></div>
    <div class="stat"><div class="v">${ride.pause_count ?? 0}</div><div class="l">Pauses</div></div>
  `;
  document.getElementById("rideModalStatsTrack").innerHTML = `
    <div class="stat"><div class="v">${fmtKm(ride.distance)}</div><div class="l">Distance</div></div>
    <div class="stat"><div class="v">${fmtAvgSpeed(ride.distance, ride.duration_without_pauses)}</div><div class="l">Vitesse moy. (roulant)</div></div>
    <div class="stat"><div class="v">${fmtAlt(ride.maximum_altitude)}</div><div class="l">Altitude max.</div></div>
    <div class="stat"><div class="v" id="rideModalElevGain">…</div><div class="l">Dénivelé (D+ / D-)</div></div>
  `;
  document.getElementById("rideModalGpx").href = `/api/rides/${ride.id}/export.gpx`;
  setupRideModalDetailsToggle();
  renderRideTimeline(ride);
  renderElevationChart(ride.id);
  renderRideModalTags(ride.tags || []);
  renderRideModalMerge(ride);
  const notesEl = document.getElementById("rideModalNotesText");
  notesEl.textContent = ride.notes || "";
  rideModalBackdrop.classList.add("visible");

  if (state.rideModalMap) {
    state.rideModalMap.remove();
    state.rideModalMap = null;
  }
  const mapEl = document.getElementById("rideModalMap");
  const map = L.map(mapEl);
  state.rideModalMap = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
    maxZoom: 19,
  }).addTo(map);

  const bounds = [];
  if (ride.polyline && ride.polyline.length) {
    L.polyline(ride.polyline, { color: "#e0552b", weight: 4, opacity: 0.9 }).addTo(map);
    ride.polyline.forEach((p) => bounds.push(p));
  }
  for (const p of ride.pauses || []) {
    if (p.lat == null || p.lon == null) continue;
    L.circleMarker([p.lat, p.lon], {
      radius: 6, color: p.automatic ? "#e0552b" : "#2b7de0", weight: 2, fillOpacity: 0.5,
    }).addTo(map);
  }
  setTimeout(() => {
    map.invalidateSize();
    if (bounds.length) map.fitBounds(bounds, { padding: [20, 20] });
    else map.setView([48.8, 2.3], 8);
  }, 0);
}

function renderRideModalTags(tags) {
  const chips = document.getElementById("rideModalTagChips");
  chips.innerHTML = tags.map((t) => `
    <span class="tag-chip" data-tag-id="${t.id}">${escapeHtml(t.name)}<button title="Retirer">×</button></span>
  `).join("");
  chips.querySelectorAll(".tag-chip button").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      const tagId = e.target.closest(".tag-chip").dataset.tagId;
      const result = await api(`/api/rides/${state.rideModalId}/tags/${tagId}`, { method: "DELETE" });
      renderRideModalTags(result.tags);
      await onTagsChanged(state.rideModalId, result.tags);
    });
  });
}

const rideModalNotesText = document.getElementById("rideModalNotesText");
rideModalNotesText.addEventListener("blur", async () => {
  const rideId = state.rideModalId;
  if (!rideId) return;
  const notes = rideModalNotesText.textContent.trim();
  try {
    await api(`/api/rides/${rideId}/notes`, { method: "PATCH", body: JSON.stringify({ notes }) });
  } catch (e) {
    // Non-fatal — leave the edited text as-is, don't lose what the user typed.
  }
});

const rideModalTagInput = document.getElementById("rideModalTagInput");
rideModalTagInput.addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  const name = rideModalTagInput.value.trim();
  if (!name) return;
  const result = await api(`/api/rides/${state.rideModalId}/tags`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  rideModalTagInput.value = "";
  renderRideModalTags(result.tags);
  await onTagsChanged(state.rideModalId, result.tags);
});

// Pushes a ride's updated tags live into the sidebar list + tags tab,
// without waiting for the user to reload the page.
async function onTagsChanged(rideId, tags) {
  const ride = state.ungrouped.find((r) => r.id === rideId);
  if (ride) ride.tags = tags;
  state.tags = await api("/api/tags");
  if (state.tab === "ungrouped" || state.tab === "tags") renderList();
}

function renderRideModalMerge(ride) {
  const el = document.getElementById("rideModalMerge");
  const memberIds = ride.merge_ride_ids || [];
  if (memberIds.length <= 1) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <span>🔗 Fusion de ${memberIds.length} trajets Liberty Rider — les traces d'origine sont conservées.</span>
    <button id="unmergeBtn">Dissocier</button>
  `;
  document.getElementById("unmergeBtn").addEventListener("click", async () => {
    if (!confirm("Dissocier ces trajets ? Ils redeviennent des trajets séparés (les données d'origine n'ont jamais été modifiées).")) return;
    await api(`/api/rides/${ride.id}/merge`, { method: "DELETE" });
    closeRideModal();
    await refresh();
  });
}

// --- roadtrip detail ---
async function showTripDetail(id) {
  const trip = await api(`/api/roadtrips/${id}`);
  const main = document.getElementById("main");
  main.innerHTML = `
    <div id="detailhead">
      <h2 contenteditable="true" id="tripName">${escapeHtml(trip.name)}</h2>
      <button id="deleteTripBtn">Supprimer</button>
      <a href="/api/roadtrips/${trip.id}/export.gpx"><button>Export GPX</button></a>
    </div>
    <div id="detailstats">
      <div class="stat"><div class="v">${trip.day_count}</div><div class="l">jours</div></div>
      <div class="stat"><div class="v">${trip.ride_count}</div><div class="l">étapes</div></div>
      <div class="stat"><div class="v">${fmtKm(trip.total_distance)}</div><div class="l">distance</div></div>
      <div class="stat"><div class="v">${fmtDuration(trip.total_duration)}</div><div class="l">durée totale</div></div>
      <div class="stat"><div class="v">${trip.total_pause_count}</div><div class="l">pauses</div></div>
      <div id="kmChart" class="mini-chart"></div>
      <label id="pausesToggleLabel"><input type="checkbox" id="pausesToggle" /> Afficher les pauses</label>
    </div>
    <div id="mapview"></div>
    <div id="daylistHeader"><span>Étapes</span><button id="daylistToggle">▾</button></div>
    <div id="daylist"></div>
  `;
  setupDaylistToggle();

  document.getElementById("pausesToggle").addEventListener("change", (e) => {
    togglePauseLayer(e.target.checked);
  });

  document.getElementById("tripName").addEventListener("blur", async (e) => {
    const name = e.target.textContent.trim();
    if (name && name !== trip.name) {
      await api(`/api/roadtrips/${trip.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      await refresh();
    }
  });
  document.getElementById("deleteTripBtn").addEventListener("click", async () => {
    if (!confirm(`Supprimer le roadtrip « ${trip.name} » ? Les trajets redeviennent non groupés.`)) return;
    await api(`/api/roadtrips/${trip.id}`, { method: "DELETE" });
    state.activeTripId = null;
    state.activeEntityKind = null;
    document.getElementById("main").innerHTML = '<div id="empty">Sélectionne un roadtrip.</div>';
    await refresh();
  });
  renderDayList(trip);
  renderKmChart(trip, true);
  renderMap(trip);
}

// --- tag detail (same map/day-list rendering as a roadtrip, no period notion) ---
async function showTagDetail(id) {
  const tag = await api(`/api/tags/${id}`);
  const main = document.getElementById("main");
  main.innerHTML = `
    <div id="detailhead">
      <h2 contenteditable="true" id="tagName">🏷️ ${escapeHtml(tag.name)}</h2>
      <button id="deleteTagBtn">Supprimer le tag</button>
      <a href="/api/tags/${tag.id}/export.gpx"><button>Export GPX</button></a>
    </div>
    <div id="detailstats">
      <div class="stat"><div class="v">${tag.day_count}</div><div class="l">jours parcourus</div></div>
      <div class="stat"><div class="v">${tag.ride_count}</div><div class="l">trajets</div></div>
      <div class="stat"><div class="v">${fmtKm(tag.total_distance)}</div><div class="l">distance</div></div>
      <div class="stat"><div class="v">${fmtDuration(tag.total_duration)}</div><div class="l">durée totale</div></div>
      <div class="stat"><div class="v">${tag.total_pause_count}</div><div class="l">pauses</div></div>
      <div id="kmChart" class="mini-chart"></div>
      <label id="pausesToggleLabel"><input type="checkbox" id="pausesToggle" /> Afficher les pauses</label>
    </div>
    <div id="mapview"></div>
    <div id="daylistHeader"><span>Étapes</span><button id="daylistToggle">▾</button></div>
    <div id="daylist"></div>
  `;
  setupDaylistToggle();

  document.getElementById("pausesToggle").addEventListener("change", (e) => {
    togglePauseLayer(e.target.checked);
  });

  document.getElementById("tagName").addEventListener("blur", async (e) => {
    const name = e.target.textContent.replace("🏷️", "").trim();
    if (name && name !== tag.name) {
      await api(`/api/tags/${tag.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      await refresh();
    }
  });
  document.getElementById("deleteTagBtn").addEventListener("click", async () => {
    if (!confirm(`Supprimer le tag « ${tag.name} » ? Les trajets ne seront plus tagués, mais restent inchangés sinon.`)) return;
    await api(`/api/tags/${tag.id}`, { method: "DELETE" });
    state.activeTagId = null;
    state.activeEntityKind = null;
    document.getElementById("main").innerHTML = '<div id="empty">Sélectionne un tag.</div>';
    await refresh();
  });

  renderDayList(tag);
  renderKmChart(tag, false);
  renderMap(tag);
}

// Collapsed by default? No — always starts open on a fresh roadtrip/tag
// view (matches the "pausesToggle" convention: per-view UI state, not
// persisted across navigation). Collapsing just hides #daylist; #mapview's
// flex:1 then expands into the freed space on its own.
function setupDaylistToggle() {
  const header = document.getElementById("daylistHeader");
  const btn = document.getElementById("daylistToggle");
  const daylist = document.getElementById("daylist");
  header.addEventListener("click", () => {
    const collapsed = daylist.classList.toggle("collapsed");
    btn.textContent = collapsed ? "▸" : "▾";
    header.title = collapsed ? "Agrandir" : "Réduire";
    setTimeout(() => state.map && state.map.invalidateSize(), 0);
  });
}

// Same collapse pattern as setupDaylistToggle: hides the stats/charts so
// #rideModalMap (flex:1) can grow into the freed space. Open by default on
// every ride opened, not persisted.
function setupRideModalDetailsToggle() {
  const header = document.getElementById("rideModalDetailsHeader");
  const btn = document.getElementById("rideModalDetailsToggle");
  const body = document.getElementById("rideModalDetailsBody");
  // Unlike #daylistHeader (rebuilt via innerHTML on every showTripDetail
  // call, so old listeners are discarded with the old DOM), this header is
  // a static element in index.html reused across every openRideModal call
  // — addEventListener here would stack a new listener each time a ride is
  // opened, so a single click ends up toggling multiple times at once.
  // .onclick assignment replaces any previous handler instead of adding on.
  header.onclick = () => {
    const collapsed = body.classList.toggle("collapsed");
    btn.textContent = collapsed ? "▸" : "▾";
    header.title = collapsed ? "Agrandir" : "Réduire";
    setTimeout(() => state.rideModalMap && state.rideModalMap.invalidateSize(), 0);
  };
}

function renderDayList(trip) {
  const wrap = document.getElementById("daylist");
  const ridesById = Object.fromEntries(trip.rides.map((r) => [r.id, r]));
  wrap.innerHTML = trip.days.map((d, i) => `
    <div class="day-group">
      <div class="day-row" data-day-index="${i}">
        <span class="swatch" style="background:${COLORS[i % COLORS.length]}"></span>
        <div class="date">${d.date}</div>
        <div class="m">${fmtKm(d.total_distance)}</div>
        <div class="m">${fmtDuration(d.total_duration)} (dont ${fmtDuration(d.total_duration_without_pauses)} à moto)</div>
        <div class="m">${d.total_pause_count} pause(s)</div>
        <div class="m">${d.ride_ids.length} étape(s)</div>
      </div>
      <div class="day-rides">
        ${d.ride_ids.map((rideId) => {
          const r = ridesById[rideId];
          if (!r) return "";
          const endTime = new Date(new Date(r.start_time).getTime() + (r.duration || 0) * 1000);
          return `
            <div class="day-ride-row">
              <span class="name" title="${escapeHtml(r.name || fmtDate(r.start_time))}">${escapeHtml(r.name || fmtDate(r.start_time))}</span>
              <span class="day-ride-meta">
                <span class="meta-item"><span class="icon">🕐</span>${fmtTime(r.start_time)}→${fmtTime(endTime)}</span>
                <span class="meta-item"><span class="icon">📏</span>${fmtKm(r.distance)}</span>
                <span class="meta-item"><span class="icon">⏱</span>${fmtDuration(r.duration)}</span>
              </span>
              <div class="day-ride-note" contenteditable="true" data-ride="${rideId}" data-placeholder="+ note…" title="${escapeHtml(r.notes || "")}">${escapeHtml(r.notes || "")}</div>
              <button class="trash-btn" data-ride="${rideId}" title="Retirer du roadtrip">🗑</button>
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `).join("");
  wrap.querySelectorAll(".day-row").forEach((row) => {
    row.addEventListener("click", () => focusDay(Number(row.dataset.dayIndex)));
  });
  wrap.querySelectorAll(".trash-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const rideId = btn.dataset.ride;
      if (!confirm("Retirer ce trajet du roadtrip ? Il redevient un trajet non groupé.")) return;
      await api(`/api/rides/${rideId}/roadtrip`, { method: "DELETE" });
      await refresh();
    });
  });
  wrap.querySelectorAll(".day-ride-note").forEach((el) => {
    el.addEventListener("click", (e) => e.stopPropagation());
    el.addEventListener("blur", async () => {
      const rideId = el.dataset.ride;
      const notes = el.textContent.trim();
      try {
        await api(`/api/rides/${rideId}/notes`, { method: "PATCH", body: JSON.stringify({ notes }) });
      } catch (e) {
        // Non-fatal — leave the edited text as-is.
      }
    });
  });
}

function renderMap(trip) {
  const el = document.getElementById("mapview");
  if (state.map) {
    state.map.remove();
    state.map = null;
  }
  const map = L.map(el).setView([48.8, 2.3], 8);
  state.map = map;
  state.dayLayers = [];
  state.dayBounds = [];
  state.dayPauseLayers = [];
  state.activeDayIndex = null;
  state.showPauses = false;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
    maxZoom: 19,
  }).addTo(map);

  const allBounds = [];
  trip.days.forEach((day, i) => {
    const color = COLORS[i % COLORS.length];
    const layers = [];
    const dayPoints = [];
    const pauseGroup = L.layerGroup();
    for (const rideId of day.ride_ids) {
      const points = trip.polylines[rideId] || [];
      if (points.length) {
        layers.push(L.polyline(points, { color, weight: 3, opacity: 0.85 }).addTo(map));
        points.forEach((p) => { dayPoints.push(p); allBounds.push(p); });
      }
      for (const p of (trip.pauses || {})[rideId] || []) {
        if (p.lat == null || p.lon == null) continue;
        L.circleMarker([p.lat, p.lon], {
          radius: 5, color: p.automatic ? "#e0552b" : "#2b7de0", weight: 2, fillOpacity: 0.5,
        }).addTo(pauseGroup);
      }
    }
    state.dayLayers.push(layers);
    state.dayBounds.push(dayPoints);
    state.dayPauseLayers.push(pauseGroup);
  });
  if (allBounds.length) map.fitBounds(allBounds, { padding: [20, 20] });
  // Pause layers hidden by default — toggled via the "Afficher les pauses"
  // checkbox, and filtered to the focused day only (see applyPauseVisibility).
}

function togglePauseLayer(show) {
  state.showPauses = show;
  applyPauseVisibility();
}

function applyPauseVisibility() {
  if (!state.map || !state.dayPauseLayers) return;
  state.dayPauseLayers.forEach((group, i) => {
    const visible = state.showPauses && (state.activeDayIndex === null || state.activeDayIndex === i);
    if (visible) group.addTo(state.map);
    else state.map.removeLayer(group);
  });
}

function focusDay(index) {
  if (!state.map || !state.dayLayers) return;
  const reset = state.activeDayIndex === index;
  state.activeDayIndex = reset ? null : index;

  state.dayLayers.forEach((layers, i) => {
    const isActive = !reset && i === index;
    const dim = !reset && !isActive;
    layers.forEach((l) => l.setStyle({ weight: isActive ? 5 : 3, opacity: dim ? 0.15 : 0.85 }));
    if (isActive) layers.forEach((l) => l.bringToFront());
  });
  applyPauseVisibility();

  document.querySelectorAll("#daylist .day-row").forEach((row) => {
    row.classList.toggle("active", !reset && Number(row.dataset.dayIndex) === index);
  });
  document.querySelectorAll("#kmChart .mini-bar").forEach((bar) => {
    const i = Number(bar.dataset.dayIndex);
    bar.classList.toggle("active", !reset && i === index);
    bar.classList.toggle("dimmed", !reset && i !== index);
  });

  if (!reset && state.dayBounds[index] && state.dayBounds[index].length) {
    state.map.fitBounds(state.dayBounds[index], { padding: [30, 30] });
  } else {
    const allBounds = state.dayBounds.flat();
    if (allBounds.length) state.map.fitBounds(allBounds, { padding: [20, 20] });
  }
}

// Adds a day, in ISO YYYY-MM-DD form, without any local-timezone drift
// (Date-only strings parse as UTC midnight; walking with setDate() would
// use local-time semantics and can land on the wrong day near a DST/UTC
// offset boundary).
function addDaysISO(iso, n) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

// Fills every calendar day between the first and last entry with a
// zero-distance placeholder, so a rest day in the middle of a roadtrip
// shows up as a visible gap instead of silently compressing the timeline.
function fillDayGaps(days) {
  if (!days.length) return days;
  const byDate = new Map(days.map((d) => [d.date, d]));
  const filled = [];
  for (let iso = days[0].date; iso <= days[days.length - 1].date; iso = addDaysISO(iso, 1)) {
    filled.push(byDate.get(iso) || { date: iso, total_distance: 0, ride_ids: [] });
  }
  return filled;
}

// Must match .mini-chart-grid's CSS row height. KM_BAR_MIN_PX must stay
// clearly above .km-bar-empty's fixed 3px so a short-but-real ride never
// blurs together with a genuine rest day next to it.
const KM_CHART_HEIGHT = 34;
const KM_BAR_MIN_PX = 8;

// Wires the shared hover tooltip for a mini-chart (km/day, pause fatigue).
// Bars carry their tooltip text in data-tooltip; this just positions the
// shared floating label above whichever bar is hovered.
function attachMiniChartTooltip(container, selector = ".mini-bar") {
  const tooltip = container.querySelector(".mini-chart-tooltip");
  container.querySelectorAll(selector).forEach((bar) => {
    bar.addEventListener("mouseenter", () => {
      const barRect = bar.getBoundingClientRect();
      const containerRect = container.getBoundingClientRect();
      tooltip.textContent = bar.dataset.tooltip;
      tooltip.style.left = `${barRect.left - containerRect.left + barRect.width / 2}px`;
      tooltip.style.top = `${barRect.top - containerRect.top}px`;
      tooltip.classList.add("visible");
    });
    bar.addEventListener("mouseleave", () => tooltip.classList.remove("visible"));
  });
}

// Single-hue magnitude chart (km/day) — days are a comparison of one
// measure, not distinct series, so all bars share the accent color; the
// currently map-focused day (if any) is emphasized, the rest dimmed, same
// as the day's polyline on the map above. `fillGaps` is true for a roadtrip
// (a real continuous date span — a rest day should show as an empty slot)
// and false for a tag (which has no span semantics — filling gaps between
// e.g. a 2022 and a 2026 ride would mean thousands of meaningless bars).
function renderKmChart(trip, fillGaps) {
  const el = document.getElementById("kmChart");
  if (!trip.days.length) {
    el.innerHTML = "";
    return;
  }
  const realIndexByDate = new Map(trip.days.map((d, i) => [d.date, i]));
  const days = fillGaps ? fillDayGaps(trip.days) : trip.days;
  const maxKm = Math.max(...days.map((d) => d.total_distance || 0), 1);
  const shortDate = (iso) => new Date(`${iso}T00:00:00Z`)
    .toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit", timeZone: "UTC" });
  const barsHtml = days.map((d, i) => {
    const realIndex = realIndexByDate.get(d.date);
    const hasRide = realIndex !== undefined && (d.total_distance || 0) > 0;
    const label = shortDate(d.date);
    if (hasRide) {
      // A day with real (if small) distance must always read as taller
      // than the flat "no ride" marker below — a big outlier day (a
      // 400km+ leg) otherwise compresses every short-but-real day to a
      // sliver indistinguishable from a rest day.
      const heightPx = Math.max((d.total_distance || 0) / maxKm * KM_CHART_HEIGHT, KM_BAR_MIN_PX);
      const tooltipText = `${label} — ${fmtKm(d.total_distance)} — ${fmtDuration(d.total_duration)}`;
      return `<div class="mini-bar" data-day-index="${realIndex}" style="grid-column:${i + 1};height:${heightPx}px"
                   data-tooltip="${tooltipText}"></div>`;
    }
    return `<div class="mini-bar km-bar-empty" style="grid-column:${i + 1}" data-tooltip="${label} — pas de trajet"></div>`;
  }).join("");
  el.innerHTML = `
    <div class="mini-chart-grid" style="grid-template-columns: repeat(${days.length}, minmax(3px, 16px));">
      ${barsHtml}
      <div class="mini-chart-label-cell" style="grid-column:1">${shortDate(days[0].date)}</div>
      <div class="mini-chart-label-cell end" style="grid-column:${days.length}">${shortDate(days[days.length - 1].date)}</div>
    </div>
    <div class="mini-chart-tooltip"></div>
  `;
  el.querySelectorAll(".mini-bar[data-day-index]").forEach((bar) => {
    bar.addEventListener("click", () => focusDay(Number(bar.dataset.dayIndex)));
  });
  attachMiniChartTooltip(el);
}

// Fatigue proxy: riding vs. paused, alternating, each segment's width
// proportional to its real duration (not indexed by pause number, so a long
// pause followed by a short ride leg is directly visible as shapes, not
// just two adjacent bar heights). Segments are precomputed server-side
// (see _merged_ride_timeline in app.py) — merged (tracking-split) rides
// need each member ride's own pause/resume events resolved independently
// before concatenating, which needs each member's own start/duration that
// only the backend has; ride.timeline is already the final [{type, start,
// end}, ...] list, this just renders it.
function renderRideTimeline(ride) {
  const el = document.getElementById("rideModalTimeline");
  const segments = (ride.timeline || []).map((s) => ({ ...s, start: new Date(s.start), end: new Date(s.end) }));
  if (!segments.length) {
    el.innerHTML = "";
    return;
  }
  const totalMs = segments.reduce((sum, s) => sum + (s.end - s.start), 0) || 1;
  const segHtml = segments.map((s) => {
    const pct = ((s.end - s.start) / totalMs) * 100;
    const label = s.type === "ride" ? "Trajet" : "Pause";
    const tooltipText = `${label} — ${fmtTime(s.start)} → ${fmtTime(s.end)} — ${fmtDuration((s.end - s.start) / 1000)}`;
    return `<div class="ride-timeline-seg ${s.type}" style="flex-basis:${pct}%" data-tooltip="${tooltipText}"></div>`;
  }).join("");
  el.innerHTML = `
    <div class="l">chronologie du trajet</div>
    <div class="ride-timeline-legend">
      <span><span class="swatch ride"></span>Trajet</span>
      <span><span class="swatch pause"></span>Pause</span>
    </div>
    <div class="ride-timeline-track">${segHtml}</div>
    <div class="ride-timeline-labels">
      <span>${fmtTime(segments[0].start)}</span>
      <span>${fmtTime(segments[segments.length - 1].end)}</span>
    </div>
    <div class="mini-chart-tooltip"></div>
  `;
  attachMiniChartTooltip(el, ".ride-timeline-seg");
}

// Elevation is estimated (see /api/rides/{id}/elevation — Liberty Rider has
// no per-point altitude data at all), fetched separately from the rest of
// the modal since open-elevation can be slow on an uncached track — this
// must never block or break the modal if it's slow or unavailable, it's a
// fun/optional detail (e.g. spotting a pause taken at the ride's high
// point, for a photo).
async function renderElevationChart(rideId) {
  const el = document.getElementById("rideModalElevation");
  el.innerHTML = "";
  const elevGainEl = document.getElementById("rideModalElevGain");
  let data;
  try {
    data = await api(`/api/rides/${rideId}/elevation`);
  } catch (e) {
    if (elevGainEl) elevGainEl.textContent = "–";
    return; // silent — optional feature, never surface an error for it
  }
  if (state.rideModalId !== rideId) return; // modal moved on to another ride meanwhile

  if (elevGainEl) {
    elevGainEl.textContent = data.elevation_gain != null
      ? `+${Math.round(data.elevation_gain)} / -${Math.round(data.elevation_loss)} m`
      : "–";
  }

  const profile = (data.profile || []).filter((p) => p.elevation != null);
  if (profile.length < 2) return;
  const pauses = (data.pauses || []).filter((p) => p.elevation != null);

  // A square 0-100 viewBox would distort circles into ellipses once
  // stretched to a wide, short chart — approximate the real rendered box
  // (matches #rideModal's width minus padding) so pause/hover dots stay
  // circular instead of measuring the DOM for a "fun" optional feature.
  const VIEW_W = 684;
  const VIEW_H = 70;
  const maxDistance = profile[profile.length - 1].distance_km || 1;
  const elevations = profile.map((p) => p.elevation);
  const minElev = Math.min(...elevations);
  const maxElev = Math.max(...elevations);
  const elevRange = maxElev - minElev || 1;
  const x = (km) => (km / maxDistance) * VIEW_W;
  const y = (elev) => VIEW_H - ((elev - minElev) / elevRange) * VIEW_H;

  const linePoints = profile.map((p) => `${x(p.distance_km)},${y(p.elevation)}`);
  const fillPoints = [`${x(profile[0].distance_km)},${VIEW_H}`, ...linePoints, `${x(profile[profile.length - 1].distance_km)},${VIEW_H}`];
  const hitCircles = profile.map((p) =>
    `<circle class="elevation-hit" cx="${x(p.distance_km)}" cy="${y(p.elevation)}" r="3"
       data-tooltip="${p.distance_km.toFixed(1)} km — ${Math.round(p.elevation)} m"></circle>`
  ).join("");
  const pauseDots = pauses.map((p) =>
    `<circle class="elevation-pause-dot" cx="${x(p.distance_km)}" cy="${y(p.elevation)}" r="2.5"
       data-tooltip="Pause — ${p.distance_km.toFixed(1)} km — ${Math.round(p.elevation)} m"></circle>`
  ).join("");

  el.innerHTML = `
    <div class="l">altitude (estimée)</div>
    <svg class="elevation-chart" viewBox="0 0 ${VIEW_W} ${VIEW_H}" preserveAspectRatio="none" overflow="visible">
      <polygon class="elevation-fill" points="${fillPoints.join(" ")}"></polygon>
      <polyline class="elevation-line" points="${linePoints.join(" ")}"></polyline>
      ${hitCircles}
      ${pauseDots}
    </svg>
    <div class="elevation-labels">
      <span>0 km</span>
      <span>${maxDistance.toFixed(1)} km</span>
    </div>
    <div class="mini-chart-tooltip"></div>
  `;
  attachMiniChartTooltip(el, ".elevation-hit, .elevation-pause-dot");

  // Cols are fetched separately (see /api/rides/{id}/cols): a network call
  // per candidate peak against OpenStreetMap's Overpass, which can be slow
  // — the chart above must never wait on this, so it's appended once (if)
  // this resolves, after the chart is already visible.
  try {
    const colsData = await api(`/api/rides/${rideId}/cols`);
    if (state.rideModalId !== rideId) return;
    const cols = (colsData.cols || []).filter((c) => c.elevation != null);
    if (!cols.length) return;
    const svg = el.querySelector(".elevation-chart");
    if (!svg) return;
    // A col is identified by shape (climbs then descends), not altitude —
    // see _detect_peaks in app.py — and named via OpenStreetMap; only
    // named ones are marked here, an unnamed peak would just be noise.
    const colMarkers = cols.map((c) => {
      const cx = x(c.distance_km);
      const cy = y(c.elevation);
      return `<polygon class="elevation-col-marker" points="${cx},${cy - 9} ${cx - 4},${cy - 2} ${cx + 4},${cy - 2}"
         data-tooltip="${c.name} — ${Math.round(c.elevation)} m — ${c.distance_km.toFixed(1)} km"></polygon>`;
    }).join("");
    svg.insertAdjacentHTML("beforeend", colMarkers);
    attachMiniChartTooltip(el, ".elevation-col-marker");
  } catch (e) {
    // silent — fun/optional detail
  }
}

(async () => {
  const status = await api("/api/auth/status");
  if (status.logged_in) {
    await enterApp(status.first_name);
  }
  // else: leave #authScreen showing, #app stays hidden — no data is ever
  // fetched before a session is confirmed.
})();
