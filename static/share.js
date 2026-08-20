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

  const stats = [
    [fmtKm(ride.distance), "Distance"],
    [fmtDuration(ride.duration), "Durée totale"],
    [fmtDuration(ride.duration_without_pauses), "À moto"],
    [fmtAvgSpeed(ride.distance, ride.duration_without_pauses), "Vitesse moy."],
    [fmtAlt(ride.maximum_altitude), "Altitude max."],
  ];
  document.getElementById("stats").innerHTML = stats
    .map(([v, l]) => `<div class="stat"><div class="v">${escapeHtml(v)}</div><div class="l">${escapeHtml(l)}</div></div>`)
    .join("");

  // Said plainly rather than hidden: the trace really does start and end a
  // couple of hundred metres from where the ride did.
  document.getElementById("notice").textContent = ride.track_truncated
    ? "Départ et arrivée approximatifs — les premiers et derniers mètres de la trace ne sont pas partagés."
    : "";
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
  renderMap(ride);
}

load();
