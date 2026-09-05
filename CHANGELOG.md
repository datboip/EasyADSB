# Changelog

All notable changes to EasyADSB will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.3] - 2026-09-05

### Fixed

- **Live map tiles**: CARTO discontinued free anonymous access to their basemap CDN, which had started showing an "API KEY REQUIRED" watermark over the Dark/Light/Voyager map styles. Replaced with Esri's free Canvas basemaps (`World_Dark_Gray_Base`/`World_Light_Gray_Base` + a labels reference layer for Dark/Light, `World_Street_Map` for Voyager) — same no-key ArcGIS service already used for Satellite/Terrain
- **False "update available" notice**: the version-compare function split versions on `.` and ran them through `Number()`, so a pre-release suffix like `"1.4.3-beta"` silently parsed as `"1.4.0"` — making the dashboard think an older release (`1.4.2`) was newer. Comparison now separates the numeric core from any `-tag` suffix before comparing
- **Dashboard reachable by hostname**, not just IP (e.g. `http://raspberrypi.local:8081`) — nginx now uses `listen 80 default_server;` / `server_name _;` as a proper catch-all instead of `server_name localhost;`. *Suggested by [@vesatikkanen](https://github.com/vesatikkanen) in [#1](https://github.com/datboip/EasyADSB/pull/1)*
- **Settings dropdowns** (poll interval / retention) no longer get stuck showing a "Loading..." placeholder option after config loads
- **Heatmap query** no longer full-scans the positions table on large databases — cutoff timestamp is computed once in Python and passed as a literal instead of wrapping `timestamp` in a SQL function, restoring index usage

### Added

- **Trail gap detection**: live map, replay, and live-dwell trails now break and reset after a >5 minute gap between position updates (aircraft went out of range and came back)
- **Trail glitch filtering**: a new trail point is skipped if it implies >900kt groundspeed from the previous one, filtering GPS jump artifacts

## [1.4.2] - 2026-03-31

### Performance

- **Dashboard loads instantly** — stats now use pre-computed summary table instead of scanning millions of rows
- **Calendar cache** builds in ~75s instead of hanging indefinitely
- **System Health** database size displays correctly (was showing N/A)

### Fixed

- **Live dwell** now shows real-time aircraft during the 15s hold — fetches directly from aircraft.json for smooth 2s updates
- Fixed timing bug where loop would skip all frames after dwell
- Poll timer cleanup and race condition guards
- **Scrubber bar** redesigned — thinner, tighter to controls, wider thumb for touch screens
- **Collapsible sections** properly respect saved state across refreshes
- **Feed Status** summary pills correctly hidden when section is expanded
- **Station IDs** section starts collapsed by default
- **Version strings** updated across all files (title, footer, API)

### Security

- Removed hardcoded receiver coordinates from map fallback defaults
- Generic US center used as fallback until receiver.json loads

## [1.4.1] - 2026-03-23

### Added

- **Replay Live Loop Mode**: weather-radar style view that plays the last few hours of aircraft history, dwells on live for 15 seconds, then loops back (`?replay=true&live=true&hours=3`)
- **Time Range Presets**: quick 1h / 3h / 6h buttons in the replay controls with auto-speed scaling
- **KML Export**: export any replay session as KML for Google Earth — client-side generation, no server load, includes 3D flight tracks with altitude
- **Privacy Toggle**: hide your receiver location dot from the map, persists across sessions via localStorage
- **New API Endpoint**: `/api/replay/frames/since?after=UNIX_TS&bucket=30` — incremental frame polling for live mode, 24h max lookback to protect Pi performance

### Changed

- Slimmed README — detailed docs moved to `docs/` folder (API reference, advanced setup, troubleshooting, configuration)

## [1.3.3] - 2026-01-24

### Added

- **Aircraft Trails**: Always-on trail tracking with altitude-colored segments
  - Trails persist in background regardless of toggle state
  - Altitude-based color gradient (green=low → red=high)
  - Dashed lines for missing/uncertain altitude data
  - Gap detection skips teleportation artifacts (>50km jumps)
  - Isolate button (🎯) to focus on single aircraft trail

- **Heatmap Visualization**: Position density overlay showing where aircraft are most frequently seen
  - Toggle button in map controls
  - Days selector (24h, 7d, 14d, 30d)
  - Color legend (blue=low → red=high density)

- **Regional & Bizjet Achievements**: Now properly tracked with pre-computed counts
  - Regional Spotter/Expert/Master (CRJ, ERJ, E-Jets, ATR, Dash 8, etc.)
  - Bizjet Spotter/Expert/High Roller (Gulfstream, Citation, Falcon, Learjet, etc.)
  - Automatic migration populates historical data on upgrade

### Fixed

- **Version display**: Footer and update checker now pull version dynamically from logger API instead of hardcoded values
- **Aircraft rotation**: Added `calc_track` fallback for heading when `track` is unavailable
- **Regional/Bizjet achievements**: Were hardcoded to 0 progress, now properly count aircraft types
- **Feed status stuck on "Checking..."**: Removed stray code causing all feed status badges to show offline

### Database Changes

- Added `is_regional` and `is_bizjet` columns to `aircraft_summary` table
- Added `regional_count` and `bizjet_count` to `stats_summary` table
- Automatic migration on upgrade from v1.3.1/v1.3.2

---

## [1.3.2] - 2026-01-13

### Documentation

- **Complete API Reference**: Expanded README with comprehensive documentation for all 27 logger API endpoints
  - Organized by category: Core, Export, Queries, Statistics, Achievements, Calendar, System
  - Includes all query parameters and request/response examples
  - Added base URL guidance (`http://<your-pi>:8082`)

### Fixed

- **VERSION file**: Corrected VERSION file which was incorrectly left at `1.3.0` during the v1.3.1 release

---

## [1.3.1] - 2026-01-07

### Added

- **Backup Service**: New backup container for easy migration and data protection
  - Config-only backup (just `.env` and `dashboard-config.js`)
  - Full backup (config + all flight log data)
  - Modal UI with progress indicator and elapsed time
  - Download link when backup completes
- **New Personal Records**: Four additional record types tracked
  - Lowest altitude flyover (filters ground traffic >500ft)
  - Slowest aircraft (filters ground traffic >50kt)
  - Earliest morning catch (00:00-06:59)
  - Latest night catch (22:00+)

### Fixed

- **Critical: Achievements hanging on large databases** - Pre-computed achievement category counts in summary tables. Achievements now load in <1ms instead of hanging indefinitely on 2M+ row databases
- **Critical: Records hanging on large databases** - Pre-computed record fields in summary tables for instant loading
- **Hardware Guide not loading** - Fixed API URL routing through nginx instead of directly to logger
- **Logger settings not saving** - Dashboard was calling wrong API endpoint (`/api/config` instead of `/api/settings`)
- **Duplicate poll interval setting** - Removed confusing duplicate "Logger Poll Interval" from Display Settings
- Backup tar "file changed" error - Now handles exit code 1 gracefully

### Improved

- **Logger Settings UX overhaul**
  - Added explicit "Save Settings" button with toast confirmation
  - Loading state shows "Loading..." while fetching saved values
  - Dropdowns disabled until settings loaded from API
  - "Apply Recommended Settings" button in Hardware Guide modal
  - Settings now properly persist across page refreshes

### Performance

- **30 new database columns** for pre-computed statistics (eliminates expensive COUNT queries)
- Achievement categories (widebody, Boeing, Airbus, turboprop, giant, EMS, coastguard) now tracked incrementally
- Records calculation reduced from minutes to milliseconds
- Overall dashboard cache loading: ~55 seconds (down from hanging indefinitely)

### Migration

- **Automatic schema migration** for users upgrading from 1.3.0 or earlier
  - ALTER TABLE adds new columns automatically
  - One-time population of achievement flags (~2 seconds)
  - One-time population of new record fields (~2 minutes on large databases)
  - No manual intervention required

---

## [1.3.0] - 2026-01-05

### Added

- **Custom Leaflet Map**: Native interactive map replacing tar1090 iframe
  - SVG aircraft icons with altitude-based coloring (green→yellow→orange→red→purple)
  - Real-time heading rotation showing aircraft direction
  - **Map Style Picker**: Dark, Light, Voyager, Satellite, Terrain, OpenStreetMap
  - **Distance Rings**: 50nm, 100nm, 150nm, 200nm, 250nm range circles
  - **Antenna Range Outline**: Shows actual reception coverage from receiver data
  - Toggle buttons: Labels, Trails, Rings, Range, Follow mode
  - Proper attribution for CARTO/OpenStreetMap
  - Click aircraft to open detail modal
- **Historical Flight Trails**: Toggle to show flight paths from logger database
  - Fetches trace data via `/api/aircraft/<icao>/trace` endpoint
- **CORS Proxy**: nginx proxy for tar1090 data endpoints (avoids browser blocking)
- **Collapsible Feed Status**: 6 feeder cards now in collapsible section
  - Compact status pills when collapsed (●Map ●ADSBx ●ADSB.lol ●FA ●FR24 ●RB)
  - Green glowing dots indicate online status
- **Activity Heatmap UX Overhaul**: Completely redesigned for clarity
  - **Enhanced tooltips**: Full day name, 12-hour time (AM/PM), aircraft count, percentage of daily traffic
  - **Color legend**: Visual gradient showing activity levels with "Less ←→ More" scale
  - **Daily totals panel**: Sorted by busiest day, weekly average, peak/quietest day percentages
  - **Info icon**: Explains what the heatmap shows on hover
  - **Hover effects**: Cells scale up with shadow, peak hour pulses with gold glow
  - **12-hour time format**: Header shows "12 AM, 6 AM, 12 PM, 6 PM" instead of 24h
- **Aircraft Database by Default**: New installations automatically enable aircraft type codes
  - `READSB_EXTRA_ARGS=--db-file=/opt/tar1090/aircraft.csv.gz` in setup.sh
  - Aircraft types (B738, A321, etc.) work out of the box
- **Cache Loading Spinners**: Clean spinner with message during cache building
  - Replaces complex skeleton loaders with simple, universally understood spinners
  - Auto-retries every 5 seconds until data is ready
- **Hardware Detection & Recommendations**: Smart hardware-aware optimization tips
  - Auto-detects Raspberry Pi models (Pi 3/4/5, Zero, Zero 2 W)
  - Detects CPU cores, RAM, and provides tailored recommendations
  - **Poll interval guide**: Visual indicator showing optimal intervals for detected hardware
  - Pi 3B+: 30s recommended, 15s works fine (tested: CPU 100%→32% with caching)
  - Pi 4/5: 10-15s recommended with full feature support
  - Version compatibility badges (✅ ⭐ 🚀) based on hardware tier
- **Achievements System**: 74+ achievements across 8 categories (altitude, speed, aircraft types, streaks, etc.)
  - Background caching system (updates every 60 seconds)
  - Progress tracking and unlock notifications
  - Rebalanced thresholds for more challenging progression
- **Leaderboard**: Time-range filtering (24h/7d/30d/all) and category filtering
- **Aircraft Type Gallery**: Categorized display of logged aircraft types
  - Helpful message when empty explaining aircraft database requirement
- **Aircraft Modal Improvements**:
  - 60-second per-ICAO caching for faster repeat views
  - Wider layout (900px) with 2-column design
  - 80vh max height for better scrolling
- **Enhanced Statistics**:
  - Personal records tracking (busiest day, max altitude, max speed, etc.)
  - System monitoring endpoint
  - Individual aircraft detailed stats and historical traces
- **Database Enhancements**:
  - 9 new columns: `alt_geom`, `mach`, `roll`, `nav_modes`, `nic`, `nac_p`, `db_flags`, `emergency`, `on_ground`
  - 4 strategic indexes: `altitude`, `speed`, `aircraft_type`, `squawk`

### Fixed

- **Critical TypeError**: Fixed `max_alt` comparison crash (string vs int)
- **Critical ValueError**: Fixed "ground" altitude parsing error
- Achievement endpoint timeout (2+ minute hangs)
- Security: Changed directory permissions from 777 to 755
- Code consistency: Standardized UUID config keys (`adsbLolUUID`)
- **Hardware Detection**: Now properly detects Pi 3B from Model field (not just BCM chip)
- **Spotter Stats**: Renamed labels ("Max Altitude", "Top Speed"), added tooltips, number formatting
- **Leaderboard TYPE column**: Shows em dash (—) with gray styling when type is null
- **Helicopter Detection**: Expanded to 40+ type patterns including Robinson, Bell, Airbus/Eurocopter, AgustaWestland, Sikorsky, MD, Kamov, Mil, plus tar1090 category A7 (rotorcraft)
- **Military Detection**: Now detects via ICAO prefixes (AE/AF/A+hex), 15+ callsign prefixes (RCH, EVAC, NAVY, ARMY, etc.), and 30+ military aircraft types (V22, C17, F35, etc.)
- **Heatmap Layout**: Fixed center gap issue - daily totals panel now properly aligned next to heatmap grid
- **Map Z-index**: Live map no longer overlaps sticky header when scrolling

### Performance

- **Achievement caching**: Response time reduced from 2 minutes to <1ms
- **Database indexing**: MAX() queries optimized with strategic indexes
- Improved query performance on 149MB+ databases

### Changed

- **Layout Reorder**: Quick Stats moved above Live Aircraft Map for better visibility
- **Compact External Links**: "Track on Other Sites" redesigned as small inline pills
- **Callsign Links**: Styled purple with ↗ arrow for external site links
- **Badges Compacted**: Frequent Flyer/Local Regular badges moved to row above stat boxes
- API responses now include caching metadata
- Enhanced error handling for database value conversions

---

## [1.2.1] - 2024-12-04

Baseline release with core flight logging functionality.

### Features

- Real-time aircraft tracking from Ultrafeeder
- SQLite database with WAL mode optimization
- REST API for dashboard integration
- CSV/JSON data export
- Live aircraft table with country flags
- Dark mode support
- Multi-feed station management (ADSBexchange, ADSB.lol, FlightAware, FlightRadar24, RadarBox)
