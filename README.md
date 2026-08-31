# Puget Sound Transit Map

A simple, static **transit map browser** for the Puget Sound region, hosted on **GitHub Pages**.
Browse and filter routes from every major agency in the consolidated GTFS feed by **agency**,
**mode**, **service days**, **time of day**, and **text search**, and optionally show the stops
served by the routes currently displayed.

👉 **Live site:** <https://tbries.github.io/puget-sound-transit/>

## Features

- Interactive [Leaflet](https://leafletjs.com/) map with OpenStreetMap tiles, centered on Puget Sound.
- Route lines colored using each route's GTFS `route_color` (with a per-agency fallback palette).
- Filter routes by:
  - **Agency** — Metro, Sound Transit, Community Transit, Pierce, Kitsap, Intercity, Everett,
    City of Seattle, Washington State Ferries, Seattle Monorail, Amtrak.
  - **Mode** — Light Rail, Rail, Bus, Ferry.
  - **Service days** — Weekday / Saturday / Sunday.
  - **Time of day** — Early morning, Daytime, Evening, Late night.
  - **Text search** — by route number or name.
- **Individual route toggles** — a per-route list (reflecting the active filters) lets you enable or
  disable specific routes, with All / None shortcuts.
- Click a route for details (agency, mode, days, service span, destinations, schedule link).
- Toggleable **stops** layer showing only the stops served by the routes currently visible.

## Data

The source of truth is the consolidated GTFS feed in
[`data/gtfs_puget_sound_consolidated`](data/gtfs_puget_sound_consolidated). It is published by
Sound Transit's Open Transit Data (OTD) program:

- OTD downloads: <https://www.soundtransit.org/help-contacts/business-information/open-transit-data-otd/otd-downloads>
- OTD overview: <https://www.soundtransit.org/help-contacts/business-information/open-transit-data-otd>

The raw GTFS cannot be served directly — `stop_times.txt` alone is ~251 MB, over GitHub's 100 MB
per-file limit, so it is tracked with **[Git LFS](https://git-lfs.com/)** (see below). A build step
preprocesses the feed into compact artifacts under [`docs/data/`](docs/data) that the static site
loads at runtime:

| Artifact              | Contents                                                            |
| --------------------- | ------------------------------------------------------------------- |
| `routes.json`         | One record per route: agency, mode, colors, derived service-day & time-of-day flags, service span, representative shape ids, headsigns. |
| `shapes.geojson`      | Simplified route line geometry (Douglas–Peucker), referenced by route id. |
| `stops.geojson`       | Trimmed stops, each tagged with the route ids it serves.            |
| `meta.json`           | Agency, mode, service-day, and time-bucket definitions for the UI.  |

The build includes only trips scheduled after August 29, 2026, removing routes that exist in the
feed solely for expired service periods.

These artifacts are **committed** to the repo so GitHub Pages can serve them with no server-side build.

## Rebuilding the data

Re-run the build whenever the GTFS feed in `/data` is refreshed:

```bash
python3 scripts/build_data.py
```

Requirements: Python 3 (standard library only — no third-party packages). The script streams the
large GTFS files, so it stays memory-bounded and finishes in a few seconds. It writes the four
artifacts above into `docs/data/` and logs their sizes. Commit the updated artifacts.

## Running locally

Serve the `docs/` directory with any static file server, e.g.:

```bash
cd docs
python3 -m http.server 8000
# open http://127.0.0.1:8000/
```

## Deploying to GitHub Pages

1. Commit and push (including the built artifacts in `docs/data/`).
2. In the repository: **Settings → Pages**.
3. Under **Build and deployment**, set **Source** = *Deploy from a branch*.
4. Choose branch `main` and folder **`/docs`**, then **Save**.
5. The site publishes at `https://<owner>.github.io/<repo>/`.

## Project layout

```
data/gtfs_puget_sound_consolidated/   # source-of-truth GTFS feed
scripts/build_data.py                 # GTFS -> compact artifacts (re-runnable)
docs/                                 # GitHub Pages site root
  index.html
  css/styles.css
  js/app.js
  data/                               # committed build artifacts
```

## Large files (Git LFS)

`data/gtfs_puget_sound_consolidated/stop_times.txt` (~251 MB) exceeds GitHub's 100 MB per-file
limit, so it is stored with [Git LFS](https://git-lfs.com/) (see `.gitattributes`). After cloning,
install LFS and pull the file:

```bash
brew install git-lfs   # or your platform's package manager
git lfs install
git lfs pull
```

The static site never needs this file at runtime — only the built artifacts in `docs/data/` are
served. It is required only to re-run `scripts/build_data.py` and can otherwise be re-downloaded
from the OTD feed.

## Notes & limitations

- Eight routes in the feed have no trips and therefore no geometry or service info; they are loaded
  but never matched by the day/time filters.
- Kitsap Transit's "Worker/Driver" commuter routes are intentionally excluded from the map (see
  `is_excluded_route` in `scripts/build_data.py`).
- Route geometry is simplified for size; it is intended for overview/browsing, not navigation.
- Out of scope for now: realtime vehicle positions, trip planning, and a calendar date-range filter.