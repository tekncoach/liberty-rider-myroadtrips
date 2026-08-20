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

// Shared by pause dots and col markers on the elevation chart: clicking a
// marker with data-lat/data-lon flies the ride's map to that spot. `getMap`
// is a function rather than the map itself: on the logged-in side the chart
// is wired while the modal's map is still being built.
function wireChartMarkerZoom(markers, getMap) {
  markers.forEach((marker) => {
    marker.addEventListener("click", () => {
      const map = getMap();
      if (!map || !marker.dataset.lat) return;
      map.flyTo([Number(marker.dataset.lat), Number(marker.dataset.lon)], 14, { duration: 0.6 });
    });
  });
}

// Single-hue magnitude chart (km/day) — days are a comparison of one
// measure, not distinct series, so all bars share the accent color; the
// currently map-focused day (if any) is emphasized, the rest dimmed, same
// as the day's polyline on the map above. `fillGaps` is true for a roadtrip
// (a real continuous date span — a rest day should show as an empty slot)
// and false for a tag (which has no span semantics — filling gaps between
// e.g. a 2022 and a 2026 ride would mean thousands of meaningless bars).


// Fatigue proxy: riding vs. paused, alternating, each segment's width
// proportional to its real duration (not indexed by pause number, so a long
// pause followed by a short ride leg is directly visible as shapes, not
// just two adjacent bar heights). Segments are precomputed server-side
// (see _merged_ride_timeline in app.py) — merged (tracking-split) rides
// need each member ride's own pause/resume events resolved independently
// before concatenating, which needs each member's own start/duration that
// only the backend has; ride.timeline is already the final [{type, start,
// end}, ...] list, this just renders it.
function renderRideTimeline(ride, el) {
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


// Draws the elevation profile into `el` and returns { x, y, svg } — the
// scale functions included, so a caller with extra markers to place (the
// app's col triangles) can put them on the same axes. Returns null when
// there is nothing worth drawing. Fetching is the caller's job: the app
// reads the private endpoint, the share page the public one.
function renderElevationProfile(el, data, { getMap, label } = {}) {
  const profile = (data.profile || []).filter((p) => p.elevation != null);
  if (profile.length < 2) return null;
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
       data-tooltip="Pause — ${p.distance_km.toFixed(1)} km — ${Math.round(p.elevation)} m"
       data-lat="${p.lat}" data-lon="${p.lon}"></circle>`
  ).join("");

  el.innerHTML = `
    <div class="l">${label || "altitude (estimée)"}</div>
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
  wireChartMarkerZoom(el.querySelectorAll(".elevation-pause-dot"), getMap || (() => null));
  return { x, y, svg: el.querySelector(".elevation-chart") };
}


// Places named cols on an already-drawn profile, using the scale functions
// renderElevationProfile handed back. Shared: the owner's modal and the
// public page mark the same passes the same way — only where the list comes
// from differs (the app computes them, the share page reads what's stored).
function appendColMarkers(el, chart, cols, getMap) {
  const drawable = (cols || []).filter((c) => c.elevation != null);
  if (!drawable.length || !chart || !chart.svg) return;
  // A col is identified by shape (climbs then descends), not altitude — see
  // _detect_peaks in app.py — and named via OpenStreetMap; only named ones
  // are marked, an unnamed peak would just be noise.
  const markers = drawable.map((c) => {
    const cx = chart.x(c.distance_km);
    const cy = chart.y(c.elevation);
    // c.name is an OpenStreetMap `name` tag — anyone can edit it, and it is
    // persisted in ride_cols and replayed on every open, so it is hostile
    // data going into an HTML attribute.
    return `<polygon class="elevation-col-marker" points="${cx},${cy - 9} ${cx - 4},${cy - 2} ${cx + 4},${cy - 2}"
       data-tooltip="${escapeHtml(c.name)} — ${Math.round(c.elevation)} m — ${c.distance_km.toFixed(1)} km"
       data-lat="${c.lat}" data-lon="${c.lon}"></polygon>`;
  }).join("");
  chart.svg.insertAdjacentHTML("beforeend", markers);
  attachMiniChartTooltip(el, ".elevation-col-marker");
  wireChartMarkerZoom(el.querySelectorAll(".elevation-col-marker"), getMap || (() => null));
}


// The "+556 / -544 m" tile — both sides show the same one.
function setElevationGain(el, data) {
  if (!el) return;
  el.textContent = data.elevation_gain != null
    ? `+${Math.round(data.elevation_gain)} / -${Math.round(data.elevation_loss)} m`
    : "–";
}
