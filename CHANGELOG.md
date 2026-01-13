# Changelog

All notable changes to EasyADSB will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
