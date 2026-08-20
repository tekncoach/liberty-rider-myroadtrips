// The public share page (/t/{token}) — the one screen in this app that runs
// for a visitor with no account. It knows a token and nothing else: no
// session, no user, no ride id. Everything it renders comes from
// GET /api/public/rides/{token}, which is an explicit allow-list of fields
// (see _public_ride_dict in app.py) — this file cannot leak what it is
// never sent.
//
// Formatting and polyline decoding come from static/shared.js, the same
// code the logged-in app uses.

// /t/<token> — the last path segment. A page served from anywhere else
// (e.g. /static/share.html opened directly) yields a token that matches
// nothing, and lands on the same "link is gone" state as a revoked one.
const token = location.pathname.split("/").filter(Boolean).pop() || "";

function showStatus(title, text) {
  document.getElementById("statusTitle").textContent = title;
  document.getElementById("statusText").textContent = text;
  document.getElementById("status").hidden = false;
  document.getElementById("page").hidden = true;
  // Shown even on a dead link: whoever followed it should still have a way
  // to find out what this thing is — minus the "trace partagée depuis",
  // since on this state there is no trace.
  document.getElementById("footerLead").textContent = "";
  document.getElementById("footer").hidden = false;
}

function renderStats(ride) {
  const endTime = new Date(new Date(ride.start_time).getTime() + (ride.duration || 0) * 1000);
  document.getElementById("rideName").textContent = ride.name || fmtDay(ride.start_time);
  document.getElementById("rideSubtitle").textContent =
    `${fmtDay(ride.start_time)} · ${fmtTime(ride.start_time)} → ${fmtTime(endTime)}`;

  // The same two groups, in the same order, as the ride modal: time
  // breakdown under the chronology, distance and altitude under the
  // profile. `elevGain` is filled in later, if and when the elevation
  // request comes back.
  fillStats("statsTime", [
    [fmtDuration(ride.duration), "Durée totale"],
    [fmtDuration(ride.duration_without_pauses), "À moto"],
    [fmtDuration(ride.total_pauses_duration), "En pause"],
    [String(ride.pause_count ?? 0), "Pauses"],
  ]);
  fillStats("statsTrack", [
    [fmtKm(ride.distance), "Distance"],
    [fmtAvgSpeed(ride.distance, ride.duration_without_pauses), "Vitesse moy. (roulant)"],
    [fmtAlt(ride.maximum_altitude), "Altitude max."],
    ["…", "Dénivelé (D+ / D-)", "elevGain"],
  ]);

  renderRideTimeline(ride, document.getElementById("timeline"));

  // Said plainly rather than hidden: the trace really does start and end a
  // couple of hundred metres from where the ride did.
  document.getElementById("notice").textContent = ride.track_truncated
    ? "Départ et arrivée approximatifs — les premiers et derniers mètres de la trace ne sont pas partagés."
    : "";
}

function fillStats(id, stats) {
  document.getElementById(id).innerHTML = stats
    .map(([v, l, valueId]) =>
      `<div class="stat"><div class="v"${valueId ? ` id="${valueId}"` : ""}>${escapeHtml(v)}</div><div class="l">${escapeHtml(l)}</div></div>`)
    .join("");
}

// Fetched after the page has already drawn, exactly like the modal does it:
// open-elevation can be slow on a track nobody has looked at yet, and the
// map must never wait on it.
async function loadElevation(map) {
  const el = document.getElementById("elevation");
  let data;
  try {
    data = await getPublic("elevation");
  } catch (e) {
    setElevationGain(document.getElementById("elevGain"), {});
    return; // silent — optional detail, never break the page over it
  }
  setElevationGain(document.getElementById("elevGain"), data);
  const chart = renderElevationProfile(el, data, { getMap: () => map });
  if (!chart) return;

  // The same named cols the owner sees on their own chart. Their endpoint
  // only reads what is already stored — it never runs the Overpass lookup
  // that found them, which is the owner's own modal's job (and the modal is
  // where a share link is created, so a shared ride has been through it).
  try {
    const { cols } = await getPublic("cols");
    appendColMarkers(el, chart, cols, () => map);
  } catch (e) {
    // silent — the chart stands on its own without them
  }
}

async function getPublic(what) {
  const resp = await fetch(`/api/public/rides/${encodeURIComponent(token)}/${what}`);
  if (!resp.ok) throw new Error(String(resp.status));
  return resp.json();
}

function renderMap(ride) {
  const map = L.map("map");
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
    maxZoom: 19,
  }).addTo(map);

  const points = decodePolylines(ride.polyline);
  if (points.length) {
    L.polyline(points, { color: "#e0552b", weight: 4, opacity: 0.9 }).addTo(map);
  }
  for (const p of ride.pauses || []) {
    if (p.lat == null || p.lon == null) continue;
    L.circleMarker([p.lat, p.lon], {
      radius: 6, color: p.automatic ? "#e0552b" : "#2b7de0", weight: 2, fillOpacity: 0.5,
    }).addTo(map);
  }
  // The map element is sized by flexbox and was hidden until a moment ago;
  // Leaflet needs to re-measure before fitBounds means anything.
  setTimeout(() => {
    map.invalidateSize();
    if (points.length) map.fitBounds(points, { padding: [24, 24] });
    else map.setView([48.8, 2.3], 8);
  }, 0);
  return map;
}

async function load() {
  let ride;
  try {
    const resp = await fetch(`/api/public/rides/${encodeURIComponent(token)}`);
    if (resp.status === 404) {
      // Revoked, expired and never-existed are one and the same here — the
      // API answers all three identically on purpose.
      showStatus("Lien introuvable", "Ce lien de partage n'est plus actif, ou n'a jamais existé.");
      return;
    }
    if (!resp.ok) throw new Error(String(resp.status));
    ride = await resp.json();
  } catch (e) {
    showStatus("Trace indisponible", "Impossible de charger cette trace pour le moment. Réessaie dans un instant.");
    return;
  }
  document.getElementById("status").hidden = true;
  document.getElementById("page").hidden = false;
  document.getElementById("footer").hidden = false;
  renderStats(ride);
  loadElevation(renderMap(ride));
}

load();
