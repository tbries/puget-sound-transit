/* Puget Sound Transit Map Browser
 *
 * Loads compact build artifacts (see scripts/build_data.py) and renders an
 * interactive Leaflet map with route/agency/mode/service-day/time-of-day and
 * text-search filtering, plus a stops layer for currently visible routes.
 */
"use strict";

const DATA = {
  routes: "data/routes.json",
  shapes: "data/shapes.geojson",
  stops: "data/stops.geojson",
  meta: "data/meta.json",
};

// Fallback colors per agency id (used when a route has no route_color).
const AGENCY_FALLBACK = {
  "1": "0072ce",   // Metro
  "3": "00857c",   // Pierce
  "19": "e87722",  // Intercity
  "20": "005596",  // Kitsap
  "23": "5b6770",  // City of Seattle
  "29": "00833e",  // Community Transit
  "40": "0098a5",  // Sound Transit
  "51": "004080",  // Amtrak
  "95": "007a33",  // WSF
  "96": "d22630",  // Monorail
  "97": "6f263d",  // Everett
};
const DEFAULT_COLOR = "888888";

const state = {
  meta: null,
  routesById: new Map(),
  shapeLayersByRoute: new Map(), // route_id -> [L.Polyline]
  stopFeatures: [],
  filters: {
    agencies: new Set(),
    types: new Set(),
    days: new Set([0, 1, 2]),       // weekday, sat, sun indices
    buckets: new Set([0, 1, 2, 3]), // time-of-day indices
    search: "",
  },
  showStops: false,
};

let map, routeLayerGroup, stopLayerGroup;

function hexColor(route) {
  const c = route.color || AGENCY_FALLBACK[route.agency_id] || DEFAULT_COLOR;
  return "#" + c.replace(/^#/, "");
}

function routeLabel(route) {
  const name = route.short_name || route.long_name || route.id;
  return route.long_name && route.short_name
    ? `${name} — ${route.long_name}`
    : name;
}

function minToClock(min) {
  if (min === null || min === undefined) return "—";
  const h24 = Math.floor(min / 60);
  const m = min % 60;
  const ampm = h24 % 24 < 12 ? "a" : "p";
  let h = h24 % 12;
  if (h === 0) h = 12;
  const over = h24 >= 24 ? " (next day)" : "";
  return `${h}:${String(m).padStart(2, "0")}${ampm}${over}`;
}

// --- Loading ---------------------------------------------------------------
async function loadAll() {
  const [routes, shapes, stops, meta] = await Promise.all(
    [DATA.routes, DATA.shapes, DATA.stops, DATA.meta].map((u) =>
      fetch(u).then((r) => {
        if (!r.ok) throw new Error(`Failed to load ${u}: ${r.status}`);
        return r.json();
      })
    )
  );
  state.meta = meta;
  routes.forEach((r) => state.routesById.set(r.id, r));
  state.stopFeatures = stops.features;
  return { routes, shapes };
}

// --- Map setup -------------------------------------------------------------
function initMap() {
  map = L.map("map", { preferCanvas: true }).setView([47.6, -122.33], 10);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  routeLayerGroup = L.layerGroup().addTo(map);
  stopLayerGroup = L.layerGroup().addTo(map);
}

function buildShapeLayers(shapesGeojson) {
  shapesGeojson.features.forEach((feature) => {
    const coords = feature.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
    feature.properties.routes.forEach((rid) => {
      const route = state.routesById.get(rid);
      if (!route) return;
      const line = L.polyline(coords, {
        color: hexColor(route),
        weight: 3,
        opacity: 0.85,
      });
      line.on("click", () => openRoutePopup(route, line));
      const arr = state.shapeLayersByRoute.get(rid) || [];
      arr.push(line);
      state.shapeLayersByRoute.set(rid, arr);
    });
  });
}

function openRoutePopup(route, layer) {
  const dayNames = ["Weekday", "Sat", "Sun"];
  const days = route.service_days
    .map((on, i) => (on ? dayNames[i] : null))
    .filter(Boolean)
    .join(", ") || "—";
  const typeName =
    (state.meta.route_types.find((t) => t.id === route.type) || {}).name ||
    "Route";
  const color = hexColor(route);
  const span =
    route.start_min !== null
      ? `${minToClock(route.start_min)} – ${minToClock(route.end_min)}`
      : "—";
  const heads = route.headsigns && route.headsigns.length
    ? route.headsigns.join(" · ")
    : "—";
  const url = route.url
    ? `<a href="${route.url}" target="_blank" rel="noopener">Schedule &amp; map ↗</a>`
    : "";
  const html = `
    <div class="route-popup">
      <h3><span class="badge" style="background:${color};color:#fff">${
        route.short_name || route.id
      }</span>${route.long_name || ""}</h3>
      <dl>
        <dt>Agency</dt><dd>${route.agency}</dd>
        <dt>Mode</dt><dd>${typeName}</dd>
        <dt>Days</dt><dd>${days}</dd>
        <dt>Span</dt><dd>${span}</dd>
        <dt>To</dt><dd>${heads}</dd>
      </dl>
      ${url ? `<p style="margin:8px 0 0;font-size:12px">${url}</p>` : ""}
    </div>`;
  layer.bindPopup(html).openPopup();
}

// --- Filtering -------------------------------------------------------------
function routeMatches(route) {
  const f = state.filters;
  // Empty selection means "none" (consistent across all filter groups).
  if (!f.agencies.has(route.agency_id)) return false;
  if (!f.types.has(route.type)) return false;

  // Service days: route must serve at least one selected day.
  let dayOk = false;
  for (const d of f.days) {
    if (route.service_days[d]) { dayOk = true; break; }
  }
  if (!dayOk) return false;

  // Time buckets: route must be active in at least one selected bucket.
  let bucketOk = false;
  for (const b of f.buckets) {
    if (route.buckets[b]) { bucketOk = true; break; }
  }
  if (!bucketOk) return false;

  // Text search against number + name.
  if (f.search) {
    const hay = (
      route.short_name + " " + route.long_name + " " + route.agency
    ).toLowerCase();
    if (!hay.includes(f.search)) return false;
  }
  return true;
}

function applyFilters() {
  routeLayerGroup.clearLayers();
  const visibleRouteIds = new Set();
  let visibleCount = 0;

  state.routesById.forEach((route, rid) => {
    if (!routeMatches(route)) return;
    visibleCount += 1;
    visibleRouteIds.add(rid);
    const layers = state.shapeLayersByRoute.get(rid);
    if (layers) layers.forEach((l) => routeLayerGroup.addLayer(l));
  });

  updateSummary(visibleCount);
  renderStops(visibleRouteIds);
}

function renderStops(visibleRouteIds) {
  stopLayerGroup.clearLayers();
  if (!state.showStops) return;
  // Avoid drawing tens of thousands of markers when zoomed out / too many.
  const zoom = map.getZoom();
  const bounds = map.getBounds();
  let drawn = 0;
  const MAX = 4000;
  for (const feature of state.stopFeatures) {
    if (drawn >= MAX) break;
    const serves = feature.properties.routes.some((r) => visibleRouteIds.has(r));
    if (!serves) continue;
    const [lon, lat] = feature.geometry.coordinates;
    if (zoom < 13 || !bounds.contains([lat, lon])) continue;
    const marker = L.circleMarker([lat, lon], {
      radius: 3,
      color: "#fff",
      weight: 1,
      fillColor: "#222",
      fillOpacity: 0.9,
    });
    marker.bindPopup(
      `<strong>${feature.properties.name || feature.properties.id}</strong>`
    );
    stopLayerGroup.addLayer(marker);
    drawn += 1;
  }
}

function updateSummary(visible) {
  const total = state.routesById.size;
  const el = document.getElementById("summary");
  el.textContent = `Showing ${visible} of ${total} routes`;
}

// --- UI construction -------------------------------------------------------
function countBy(keyFn) {
  const counts = new Map();
  state.routesById.forEach((r) => {
    const k = keyFn(r);
    counts.set(k, (counts.get(k) || 0) + 1);
  });
  return counts;
}

function buildAgencyOptions() {
  const counts = countBy((r) => r.agency_id);
  const container = document.getElementById("agency-options");
  state.meta.agencies.forEach((a) => {
    state.filters.agencies.add(a.id);
    const swatch = "#" + (AGENCY_FALLBACK[a.id] || DEFAULT_COLOR);
    container.appendChild(
      makeOption("agency", a.id, a.name, counts.get(a.id) || 0, swatch)
    );
  });
}

function buildTypeOptions() {
  const counts = countBy((r) => r.type);
  const container = document.getElementById("type-options");
  state.meta.route_types.forEach((t) => {
    state.filters.types.add(t.id);
    container.appendChild(
      makeOption("type", t.id, t.name, counts.get(t.id) || 0, null)
    );
  });
}

function buildDayOptions() {
  const container = document.getElementById("day-options");
  state.meta.service_days.forEach((d, i) => {
    container.appendChild(makeOption("day", String(i), d.name, null, null, true));
  });
}

function buildBucketOptions() {
  const container = document.getElementById("bucket-options");
  state.meta.buckets.forEach((b, i) => {
    container.appendChild(makeOption("bucket", String(i), b.name, null, null, true));
  });
}

function makeOption(group, value, label, count, swatchColor, checked) {
  const lbl = document.createElement("label");
  lbl.className = "opt";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = checked === undefined ? true : checked;
  cb.dataset.group = group;
  cb.value = value;
  cb.addEventListener("change", onFilterChange);
  lbl.appendChild(cb);
  if (swatchColor) {
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = swatchColor;
    lbl.appendChild(sw);
  }
  const span = document.createElement("span");
  span.textContent = label;
  lbl.appendChild(span);
  if (count !== null && count !== undefined) {
    const c = document.createElement("span");
    c.className = "count";
    c.textContent = count;
    lbl.appendChild(c);
  }
  return lbl;
}

function filterSetFor(group) {
  switch (group) {
    case "agency": return state.filters.agencies;
    case "type": return state.filters.types;
    case "day": return state.filters.days;
    case "bucket": return state.filters.buckets;
  }
  return null;
}

function onFilterChange(e) {
  const cb = e.target;
  const set = filterSetFor(cb.dataset.group);
  if (!set) return;
  const val =
    cb.dataset.group === "day" || cb.dataset.group === "bucket"
      ? Number(cb.value)
      : cb.value;
  if (cb.checked) set.add(val);
  else set.delete(val);
  applyFilters();
}

function wireGroupButtons() {
  document.querySelectorAll("[data-all]").forEach((btn) => {
    btn.addEventListener("click", () => toggleGroup(btn.dataset.all, true));
  });
  document.querySelectorAll("[data-none]").forEach((btn) => {
    btn.addEventListener("click", () => toggleGroup(btn.dataset.none, false));
  });
}

function toggleGroup(group, on) {
  const set = filterSetFor(group);
  set.clear();
  document
    .querySelectorAll(`input[data-group="${group}"]`)
    .forEach((cb) => {
      cb.checked = on;
      if (on) {
        const val = group === "day" || group === "bucket" ? Number(cb.value) : cb.value;
        set.add(val);
      }
    });
  applyFilters();
}

function wireSearch() {
  const input = document.getElementById("search");
  let t;
  input.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => {
      state.filters.search = input.value.trim().toLowerCase();
      applyFilters();
    }, 150);
  });
}

function wireStopsToggle() {
  const cb = document.getElementById("toggle-stops");
  cb.addEventListener("change", () => {
    state.showStops = cb.checked;
    applyFilters();
  });
}

function wireSidebarToggle() {
  const btn = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  btn.addEventListener("click", () => {
    sidebar.classList.toggle("collapsed");
    setTimeout(() => map.invalidateSize(), 220);
  });
}

// Re-render stops when the user pans/zooms (stops depend on viewport).
function wireMapMove() {
  let t;
  map.on("moveend", () => {
    if (!state.showStops) return;
    clearTimeout(t);
    t = setTimeout(() => {
      const visible = new Set();
      state.routesById.forEach((r, rid) => {
        if (routeMatches(r)) visible.add(rid);
      });
      renderStops(visible);
    }, 120);
  });
}

// --- Boot ------------------------------------------------------------------
async function boot() {
  initMap();
  try {
    const { shapes } = await loadAll();
    buildShapeLayers(shapes);
    buildAgencyOptions();
    buildTypeOptions();
    buildDayOptions();
    buildBucketOptions();
    wireGroupButtons();
    wireSearch();
    wireStopsToggle();
    wireSidebarToggle();
    wireMapMove();
    applyFilters();
  } catch (err) {
    document.getElementById("summary").textContent = "Error: " + err.message;
    console.error(err);
  }
}

boot();
