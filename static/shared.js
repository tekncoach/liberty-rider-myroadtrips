// Shared by the logged-in app (static/app.js) and the public share page
// (static/share.js): formatting and polyline decoding, pure functions with
// no DOM, no `state`, no session. Two plain <script> tags, no modules and
// no build step — same way Leaflet's `L` is picked up.

const MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];

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

// --- polyline decoding ---
// The API sends raw Google-encoded polyline strings (precision 5) instead of
// decoded [lat,lon] arrays — ~6x smaller over the wire, and the decode runs
// here on the client instead of on the CPU-throttled server. Standard
// algorithm, matching Python's `polyline` library.
function decodePolyline(str) {
  let index = 0, lat = 0, lng = 0;
  const coords = [];
  while (index < str.length) {
    let shift = 0, result = 0, byte;
    do { byte = str.charCodeAt(index++) - 63; result += (byte & 0x1f) << shift; shift += 5; } while (byte >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);
    shift = 0; result = 0;
    do { byte = str.charCodeAt(index++) - 63; result += (byte & 0x1f) << shift; shift += 5; } while (byte >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);
    coords.push([lat / 1e5, lng / 1e5]);
  }
  return coords;
}
// A ride's polyline arrives as a LIST of encoded strings (one per merge
// member) — decode each and concatenate into one flat [lat,lon] array.
function decodePolylines(encodedList) {
  const points = [];
  for (const enc of encodedList || []) {
    if (enc) for (const p of decodePolyline(enc)) points.push(p);
  }
  return points;
}
