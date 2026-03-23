# API Reference

The flight logger provides a REST API at `http://<your-pi>:8082`. All endpoints return JSON.

## Core Status & Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check (status, paused state, last poll time) |
| GET | `/api/stats` | Basic logging statistics |
| GET | `/api/settings` | Get current settings (interval, retention) |
| POST | `/api/settings` | Update settings `{"interval": 10, "retention_days": 14}` |
| POST | `/api/pause` | Pause logging |
| POST | `/api/resume` | Resume logging |
| POST | `/api/clear` | Clear all logged data |

## Data Export

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/export` | Download all logs as CSV |
| GET | `/api/export?start=DATE&end=DATE` | Download filtered CSV (ISO dates) |
| GET | `/api/export/json` | Download all logs as JSON |
| GET | `/api/export/json?start=DATE&end=DATE` | Download filtered JSON |

## Flight Queries

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/flights` | Query flights (`?icao=` `?callsign=` `?start=` `?end=` `?limit=100`) |
| GET | `/api/trace/<icao>` | Get flight path for aircraft |
| GET | `/api/recent` | Recent aircraft (last hour, limit 50) |
| GET | `/api/aircraft/<icao>` | Detailed stats for specific aircraft |
| GET | `/api/aircraft/<icao>/trace` | Position traces (`?range=24h\|7d\|14d\|30d\|all`) |
| GET | `/api/aircraft/<icao>/photo` | Aircraft photo (proxies planespotters.net) |
| GET | `/api/callsign/<callsign>` | Callsign history and stats |

## Leaderboard & Statistics

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/leaderboard` | Top aircraft (`?range=24h\|7d\|14d\|30d\|all` `?limit=20` `?category=all\|commercial\|cargo\|military\|ga`) |
| GET | `/api/stats/overview` | Comprehensive statistics overview |
| GET | `/api/stats/records` | Personal records (fastest, highest, etc.) |
| GET | `/api/stats/timeline` | Traffic timeline (`?range=day\|week\|month` `?date=YYYY-MM-DD`) |
| GET | `/api/stats/heatmap` | Position density for heatmap (`?days=7` `?resolution=0.005` `?type=all\|helicopter\|military`) |

## Achievements & Gallery

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/achievements` | Achievement progress (74+ achievements) |
| GET | `/api/gallery` | Aircraft type gallery with counts |

## Calendar & History

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/logger/calendar` | Monthly logging activity (`?month=YYYY-MM` `?range=month\|year`) |
| GET | `/api/logger/calendar/<date>` | Detailed stats for specific day |

## Replay

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/replay/dates` | Available dates with position/aircraft counts |
| GET | `/api/replay/frames` | Stream NDJSON frames (`?date=YYYY-MM-DD` `?bucket=30` `?hours=24` `?live=true`) |
| GET | `/api/replay/frames/since` | Incremental frames since timestamp (`?after=UNIX_TS` `?bucket=30`) |

## System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system` | Pi system stats (CPU, memory, disk, uptime) |
| GET | `/api/system/hardware` | Hardware detection + optimization recommendations |
| GET | `/api/userconfig` | Get user dashboard config |
| POST | `/api/userconfig` | Update user config |
