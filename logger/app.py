#!/usr/bin/env python3
"""
EasyADSB Flight Logger
Version: 1.3.0

Polls ultrafeeder for aircraft data and stores in SQLite database.
Provides REST API for dashboard integration.

Endpoints:
    GET  /health                - Health check
    GET  /api/stats             - Get logging statistics
    GET  /api/settings          - Get current settings
    POST /api/settings          - Update settings
    GET  /api/userconfig        - Get user dashboard config (e.g., ADSBx Short ID)
    POST /api/userconfig        - Update user dashboard config
    GET  /api/export            - Download logs as CSV
    GET  /api/export/json       - Download logs as JSON
    GET  /api/flights           - Query flights with filters
    GET  /api/trace/<icao>      - Get flight path for aircraft
    GET  /api/leaderboard       - Top aircraft by sightings (with time range)
    GET  /api/aircraft/<icao>   - Detailed stats for specific aircraft
    GET  /api/aircraft/<icao>/trace - All traces for trail overlay
    POST /api/pause             - Pause logging
    POST /api/resume            - Resume logging
    POST /api/clear             - Clear all logs
"""

import os
import sys
import json
import csv
import io
import time
import sqlite3
import threading
import logging
import subprocess
from datetime import datetime, timedelta
from functools import wraps
from collections import deque

import requests
from flask import Flask, jsonify, request, Response, send_file

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# Load history for sparkline (last 30 readings, ~5 minutes at 10s intervals)
load_history = deque(maxlen=30)

# Environment variables with defaults
ULTRAFEEDER_HOST = os.getenv('ULTRAFEEDER_HOST', 'ultrafeeder')
ULTRAFEEDER_PORT = os.getenv('ULTRAFEEDER_PORT', '8080')
LOG_INTERVAL = int(os.getenv('LOG_INTERVAL', '10'))
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '14'))
DB_PATH = os.getenv('DB_PATH', '/data/flights.db')
CONFIG_PATH = os.getenv('CONFIG_PATH', '/data/config.json')
USER_CONFIG_PATH = os.getenv('USER_CONFIG_PATH', '/data/user-config.json')

# Runtime state
logger_paused = False
logger_running = True
last_poll_time = None
last_poll_count = 0
total_logged = 0

# Dashboard caches (updated by background thread every 60 seconds)
achievements_cache = {'data': None, 'last_updated': None}
leaderboard_cache = {'data': {}, 'last_updated': None}  # Keyed by range+category
overview_cache = {'data': None, 'last_updated': None}
records_cache = {'data': None, 'last_updated': None}
gallery_cache = {'data': None, 'last_updated': None}
calendar_cache = {'data': None, 'last_updated': None}  # Collection stats (current month)
# NOTE: heatmap_cache removed in v1.3.0 - Activity Heatmap removed from frontend
aircraft_detail_cache = {}  # Per-ICAO cache with 15 minute TTL (longer than 10min refresh cycle)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('easyadsb-logger')

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db(timeout=30):
    """Get database connection with row factory and timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables."""
    log.info(f"Initializing database at {DB_PATH}")
    conn = get_db()
    cursor = conn.cursor()
    
    # Enable WAL mode for better SD card performance (reduces writes by 2-4x)
    cursor.execute('PRAGMA journal_mode = WAL')
    cursor.execute('PRAGMA synchronous = NORMAL')
    cursor.execute('PRAGMA cache_size = -8000')  # 8MB cache
    
    # Main flights table - stores position snapshots
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            icao TEXT NOT NULL,
            callsign TEXT,
            lat REAL,
            lon REAL,
            altitude INTEGER,
            speed INTEGER,
            track INTEGER,
            vert_rate INTEGER,
            squawk TEXT,
            category TEXT,
            aircraft_type TEXT,
            rssi REAL,
            alt_geom INTEGER,
            mach REAL,
            roll REAL,
            nav_modes TEXT,
            nic INTEGER,
            nac_p INTEGER,
            db_flags INTEGER,
            emergency TEXT,
            on_ground INTEGER
        )
    ''')
    
    # Migration: Add new columns to existing databases
    new_columns = [
        ('alt_geom', 'INTEGER'),
        ('mach', 'REAL'),
        ('roll', 'REAL'),
        ('nav_modes', 'TEXT'),
        ('nic', 'INTEGER'),
        ('nac_p', 'INTEGER'),
        ('db_flags', 'INTEGER'),
        ('emergency', 'TEXT'),
        ('on_ground', 'INTEGER'),
    ]
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f'ALTER TABLE positions ADD COLUMN {col_name} {col_type}')
            log.info(f"Added column {col_name} to positions table")
        except sqlite3.OperationalError:
            pass  # Column already exists
    
    # Index for common queries
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON positions(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_icao ON positions(icao)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_callsign ON positions(callsign)')

    # Indexes for achievement queries (MAX, GROUP BY, filtering)
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_altitude ON positions(altitude)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_speed ON positions(speed)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_type ON positions(aircraft_type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_squawk ON positions(squawk)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_rssi ON positions(rssi)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_category ON positions(category)')
    
    # Stats table for daily summaries
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            total_positions INTEGER DEFAULT 0,
            unique_aircraft INTEGER DEFAULT 0,
            unique_flights INTEGER DEFAULT 0
        )
    ''')

    # ══════════════════════════════════════════════════════════════════════════
    # SUMMARY TABLES - Pre-aggregated data for fast dashboard queries
    # These are updated incrementally during polling instead of scanning all rows
    # ══════════════════════════════════════════════════════════════════════════

    # Global stats summary (single row - all-time totals)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stats_summary (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_positions INTEGER DEFAULT 0,
            unique_aircraft INTEGER DEFAULT 0,
            unique_types INTEGER DEFAULT 0,
            unique_callsigns INTEGER DEFAULT 0,
            days_active INTEGER DEFAULT 0,
            first_log TEXT,
            last_log TEXT,
            -- Category counts (distinct ICAOs)
            military_count INTEGER DEFAULT 0,
            helicopter_count INTEGER DEFAULT 0,
            cargo_count INTEGER DEFAULT 0,
            -- Time of day counts
            night_count INTEGER DEFAULT 0,
            early_count INTEGER DEFAULT 0,
            -- Airline counts
            delta_count INTEGER DEFAULT 0,
            united_count INTEGER DEFAULT 0,
            american_count INTEGER DEFAULT 0,
            southwest_count INTEGER DEFAULT 0,
            jetblue_count INTEGER DEFAULT 0,
            -- Records
            max_altitude INTEGER,
            max_altitude_icao TEXT,
            max_altitude_callsign TEXT,
            max_altitude_type TEXT,
            max_altitude_timestamp TEXT,
            max_speed INTEGER,
            max_speed_icao TEXT,
            max_speed_callsign TEXT,
            max_speed_type TEXT,
            max_speed_timestamp TEXT,
            best_rssi REAL,
            best_rssi_icao TEXT,
            best_rssi_callsign TEXT,
            best_rssi_type TEXT,
            best_rssi_timestamp TEXT,
            lowest_altitude INTEGER,
            lowest_altitude_icao TEXT,
            lowest_altitude_callsign TEXT,
            lowest_altitude_type TEXT,
            lowest_altitude_timestamp TEXT,
            slowest_speed INTEGER,
            slowest_speed_icao TEXT,
            slowest_speed_callsign TEXT,
            slowest_speed_type TEXT,
            slowest_speed_timestamp TEXT,
            earliest_catch_time TEXT,
            earliest_catch_icao TEXT,
            earliest_catch_callsign TEXT,
            earliest_catch_timestamp TEXT,
            earliest_catch_type TEXT,
            latest_catch_time TEXT,
            latest_catch_icao TEXT,
            latest_catch_callsign TEXT,
            latest_catch_timestamp TEXT,
            latest_catch_type TEXT,
            -- Misc
            emergency_count INTEGER DEFAULT 0,
            hours_covered INTEGER DEFAULT 0,
            busiest_day TEXT,
            busiest_day_count INTEGER DEFAULT 0,
            max_streak INTEGER DEFAULT 0,
            intl_carriers_seen TEXT,
            last_updated TEXT,
            -- Achievement category counts (from aircraft_summary flags)
            widebody_count INTEGER DEFAULT 0,
            boeing_count INTEGER DEFAULT 0,
            airbus_count INTEGER DEFAULT 0,
            turboprop_count INTEGER DEFAULT 0,
            giant_count INTEGER DEFAULT 0,
            ems_heli_count INTEGER DEFAULT 0,
            coastguard_count INTEGER DEFAULT 0
        )
    ''')

    # Per-aircraft summary (for leaderboard - replaces GROUP BY icao queries)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS aircraft_summary (
            icao TEXT PRIMARY KEY,
            position_count INTEGER DEFAULT 0,
            session_count INTEGER DEFAULT 0,
            days_seen INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            last_session_key TEXT,
            aircraft_type TEXT,
            callsigns TEXT,
            max_altitude INTEGER,
            max_speed INTEGER,
            is_military INTEGER DEFAULT 0,
            is_helicopter INTEGER DEFAULT 0,
            is_cargo INTEGER DEFAULT 0,
            is_commercial INTEGER DEFAULT 0,
            airline TEXT,
            -- Achievement category flags (for fast counting)
            is_widebody INTEGER DEFAULT 0,
            is_boeing INTEGER DEFAULT 0,
            is_airbus INTEGER DEFAULT 0,
            is_turboprop INTEGER DEFAULT 0,
            is_giant INTEGER DEFAULT 0,
            is_ems_heli INTEGER DEFAULT 0,
            is_coastguard INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_summary_sessions ON aircraft_summary(session_count DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_summary_type ON aircraft_summary(aircraft_type)')

    # Per-type summary (for gallery - replaces GROUP BY aircraft_type queries)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS type_summary (
            aircraft_type TEXT PRIMARY KEY,
            unique_aircraft INTEGER DEFAULT 0,
            total_positions INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            sample_icao TEXT
        )
    ''')

    # Tracking tables for COUNT(DISTINCT) simulation
    # These track which values have been seen (for incremental unique counting)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS seen_callsigns (
            callsign TEXT PRIMARY KEY,
            first_seen TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS seen_days (
            date TEXT PRIMARY KEY
        )
    ''')

    # Migrate existing databases: add new columns if they don't exist
    # This handles upgrading from older schema versions
    stats_new_columns = [
        ('widebody_count', 'INTEGER DEFAULT 0'),
        ('boeing_count', 'INTEGER DEFAULT 0'),
        ('airbus_count', 'INTEGER DEFAULT 0'),
        ('turboprop_count', 'INTEGER DEFAULT 0'),
        ('giant_count', 'INTEGER DEFAULT 0'),
        ('ems_heli_count', 'INTEGER DEFAULT 0'),
        ('coastguard_count', 'INTEGER DEFAULT 0'),
        # Additional records fields (v1.3.1)
        ('lowest_altitude_callsign', 'TEXT'),
        ('lowest_altitude_type', 'TEXT'),
        ('lowest_altitude_timestamp', 'TEXT'),
        ('slowest_speed_callsign', 'TEXT'),
        ('slowest_speed_type', 'TEXT'),
        ('slowest_speed_timestamp', 'TEXT'),
        ('earliest_catch_time', 'TEXT'),
        ('earliest_catch_icao', 'TEXT'),
        ('earliest_catch_callsign', 'TEXT'),
        ('earliest_catch_timestamp', 'TEXT'),
        ('earliest_catch_type', 'TEXT'),
        ('latest_catch_time', 'TEXT'),
        ('latest_catch_icao', 'TEXT'),
        ('latest_catch_callsign', 'TEXT'),
        ('latest_catch_timestamp', 'TEXT'),
        ('latest_catch_type', 'TEXT'),
    ]
    for col_name, col_type in stats_new_columns:
        try:
            cursor.execute(f'ALTER TABLE stats_summary ADD COLUMN {col_name} {col_type}')
            log.info(f"Added column {col_name} to stats_summary")
        except sqlite3.OperationalError:
            pass  # Column already exists

    aircraft_new_columns = [
        ('is_widebody', 'INTEGER DEFAULT 0'),
        ('is_boeing', 'INTEGER DEFAULT 0'),
        ('is_airbus', 'INTEGER DEFAULT 0'),
        ('is_turboprop', 'INTEGER DEFAULT 0'),
        ('is_giant', 'INTEGER DEFAULT 0'),
        ('is_ems_heli', 'INTEGER DEFAULT 0'),
        ('is_coastguard', 'INTEGER DEFAULT 0'),
    ]
    for col_name, col_type in aircraft_new_columns:
        try:
            cursor.execute(f'ALTER TABLE aircraft_summary ADD COLUMN {col_name} {col_type}')
            log.info(f"Added column {col_name} to aircraft_summary")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Initialize stats_summary with a single row if not exists
    cursor.execute('INSERT OR IGNORE INTO stats_summary (id) VALUES (1)')

    # Check if we need to populate the new achievement columns (migration)
    # If widebody_count is 0 but we have aircraft data, run the migration
    cursor.execute('SELECT widebody_count FROM stats_summary WHERE id = 1')
    row = cursor.fetchone()
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary')
    aircraft_count = cursor.fetchone()[0]

    if row and row[0] == 0 and aircraft_count > 0:
        log.info("Migrating achievement flags for existing aircraft...")
        # We need to populate the flags - this will be done by reading aircraft_type
        # and callsigns from aircraft_summary and computing the flags
        cursor.execute('SELECT icao, aircraft_type, callsigns FROM aircraft_summary')
        rows = cursor.fetchall()

        widebody_total = 0
        boeing_total = 0
        airbus_total = 0
        turboprop_total = 0
        giant_total = 0
        ems_total = 0
        coastguard_total = 0

        for icao, atype, callsigns in rows:
            # Import helper functions here - they're defined later in the file
            # so we use inline logic for the migration
            atype_upper = (atype or '').upper()

            # Widebody check
            widebodies = ['B744', 'B748', 'B772', 'B773', 'B77L', 'B77W', 'B788', 'B789', 'B78X',
                          'A332', 'A333', 'A338', 'A339', 'A342', 'A343', 'A345', 'A346', 'A359', 'A35K', 'A380', 'A388']
            is_wb = 1 if atype_upper in widebodies else 0

            # Boeing check
            is_boe = 1 if (atype_upper.startswith('B7') or atype_upper.startswith('B73') or
                          atype_upper.startswith('B74') or atype_upper.startswith('B75') or
                          atype_upper.startswith('B76') or atype_upper.startswith('B78')) else 0

            # Airbus check
            is_air = 1 if (atype_upper.startswith('A3') or atype_upper.startswith('A2')) else 0

            # Turboprop check
            is_turbo = 1 if (atype_upper.startswith('DH8') or atype_upper.startswith('AT') or
                            atype_upper == 'SF34' or atype_upper.startswith('BE') or
                            atype_upper == 'C208') else 0

            # Giant check
            giants = ['A380', 'A388', 'B748', 'AN124', 'AN225', 'C5', 'C5M', 'C17', 'B52']
            is_gi = 1 if atype_upper in giants else 0

            # EMS helicopter check
            ems_types = ['EC35', 'EC45', 'AS50', 'AS55', 'B407', 'B429', 'B412', 'B206',
                         'A109', 'A139', 'A169', 'S76', 'MD52', 'MD50', 'BK17', 'H145', 'H135']
            is_ems = 1 if atype_upper in ems_types else 0

            # Coastguard check (from callsigns)
            is_cg = 0
            if callsigns:
                for cs in callsigns.split(','):
                    cs_upper = cs.upper().strip()
                    if cs_upper.startswith('COAST') or cs_upper.startswith('USCG') or cs_upper.startswith('CG'):
                        is_cg = 1
                        break

            cursor.execute('''
                UPDATE aircraft_summary
                SET is_widebody = ?, is_boeing = ?, is_airbus = ?, is_turboprop = ?,
                    is_giant = ?, is_ems_heli = ?, is_coastguard = ?
                WHERE icao = ?
            ''', (is_wb, is_boe, is_air, is_turbo, is_gi, is_ems, is_cg, icao))

            widebody_total += is_wb
            boeing_total += is_boe
            airbus_total += is_air
            turboprop_total += is_turbo
            giant_total += is_gi
            ems_total += is_ems
            coastguard_total += is_cg

        # Update stats_summary with the totals
        cursor.execute('''
            UPDATE stats_summary SET
                widebody_count = ?,
                boeing_count = ?,
                airbus_count = ?,
                turboprop_count = ?,
                giant_count = ?,
                ems_heli_count = ?,
                coastguard_count = ?
            WHERE id = 1
        ''', (widebody_total, boeing_total, airbus_total, turboprop_total,
              giant_total, ems_total, coastguard_total))

        log.info(f"Migration complete: {widebody_total} widebody, {boeing_total} Boeing, "
                 f"{airbus_total} Airbus, {turboprop_total} turboprop, {giant_total} giant, "
                 f"{ems_total} EMS, {coastguard_total} coastguard")

    # Check if new record fields need to be populated (v1.3.1 migration)
    # These are NULL if upgrading from 1.3.0
    cursor.execute('SELECT lowest_altitude, total_positions FROM stats_summary WHERE id = 1')
    row = cursor.fetchone()
    if row and row['total_positions'] and row['total_positions'] > 0 and row['lowest_altitude'] is None:
        log.info("Migrating v1.3.1 record fields...")

        # Lowest altitude (> 500ft to filter ground)
        cursor.execute('''
            SELECT icao, callsign, altitude, timestamp, aircraft_type
            FROM positions WHERE altitude IS NOT NULL AND altitude > 500
            ORDER BY altitude ASC LIMIT 1
        ''')
        lowest = cursor.fetchone()

        # Slowest (> 50kt to filter ground)
        cursor.execute('''
            SELECT icao, callsign, speed, timestamp, aircraft_type
            FROM positions WHERE speed IS NOT NULL AND speed > 50
            ORDER BY speed ASC LIMIT 1
        ''')
        slowest = cursor.fetchone()

        # Earliest morning catch (00:00-06:59)
        cursor.execute('''
            SELECT icao, callsign, timestamp, aircraft_type, strftime('%H:%M', timestamp) as time_of_day
            FROM positions WHERE strftime('%H', timestamp) BETWEEN '00' AND '06'
            ORDER BY strftime('%H:%M', timestamp) ASC LIMIT 1
        ''')
        earliest = cursor.fetchone()

        # Latest night catch (22:00+)
        cursor.execute('''
            SELECT icao, callsign, timestamp, aircraft_type, strftime('%H:%M', timestamp) as time_of_day
            FROM positions WHERE strftime('%H', timestamp) >= '22'
            ORDER BY strftime('%H:%M', timestamp) DESC LIMIT 1
        ''')
        latest = cursor.fetchone()

        cursor.execute('''
            UPDATE stats_summary SET
                lowest_altitude = ?,
                lowest_altitude_icao = ?,
                lowest_altitude_callsign = ?,
                lowest_altitude_type = ?,
                lowest_altitude_timestamp = ?,
                slowest_speed = ?,
                slowest_speed_icao = ?,
                slowest_speed_callsign = ?,
                slowest_speed_type = ?,
                slowest_speed_timestamp = ?,
                earliest_catch_time = ?,
                earliest_catch_icao = ?,
                earliest_catch_callsign = ?,
                earliest_catch_timestamp = ?,
                earliest_catch_type = ?,
                latest_catch_time = ?,
                latest_catch_icao = ?,
                latest_catch_callsign = ?,
                latest_catch_timestamp = ?,
                latest_catch_type = ?
            WHERE id = 1
        ''', (
            lowest['altitude'] if lowest else None,
            lowest['icao'] if lowest else None,
            lowest['callsign'] if lowest else None,
            lowest['aircraft_type'] if lowest else None,
            lowest['timestamp'] if lowest else None,
            slowest['speed'] if slowest else None,
            slowest['icao'] if slowest else None,
            slowest['callsign'] if slowest else None,
            slowest['aircraft_type'] if slowest else None,
            slowest['timestamp'] if slowest else None,
            earliest['time_of_day'] if earliest else None,
            earliest['icao'] if earliest else None,
            earliest['callsign'] if earliest else None,
            earliest['timestamp'] if earliest else None,
            earliest['aircraft_type'] if earliest else None,
            latest['time_of_day'] if latest else None,
            latest['icao'] if latest else None,
            latest['callsign'] if latest else None,
            latest['timestamp'] if latest else None,
            latest['aircraft_type'] if latest else None
        ))
        log.info("v1.3.1 record fields migration complete")

    conn.commit()
    conn.close()
    log.info("Database initialized")


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_military_aircraft(icao, callsign, aircraft_type):
    """Check if aircraft is military based on ICAO, callsign, or type."""
    if not icao:
        return False
    icao = icao.upper()
    callsign = (callsign or '').upper().strip()
    aircraft_type = (aircraft_type or '').upper()

    # US military ICAO prefixes (AE/AF ranges are military)
    if icao.startswith('AE') or icao.startswith('AF'):
        return True

    # Military callsign prefixes
    mil_callsigns = ['RCH', 'EVAC', 'REACH', 'DUKE', 'KING', 'NOBLE', 'VALOR', 'PAT', 'CNV',
                     'TOPCT', 'JUDGE', 'NAVY', 'ARMY', 'COAST', 'GUARD']
    if any(callsign.startswith(p) for p in mil_callsigns):
        return True

    # Military aircraft types
    mil_types = ['V22', 'C17', 'C5M', 'C130', 'C5', 'KC10', 'KC135', 'KC46', 'E3', 'E6', 'E8',
                 'P8', 'P3', 'EP3', 'RC135', 'U2', 'B1', 'B2', 'B52', 'F15', 'F16', 'F18',
                 'F22', 'F35', 'A10', 'MH60', 'UH60', 'CH47', 'AH64', 'MV22', 'CV22', 'H60', 'C40']
    if aircraft_type in mil_types:
        return True

    return False


def is_helicopter_aircraft(aircraft_type, category):
    """Check if aircraft is a helicopter based on type or category."""
    if category == 'A7':
        return True
    if not aircraft_type:
        return False
    t = aircraft_type.upper()

    heli_prefixes = ['R22', 'R44', 'R66', 'B06', 'B206', 'B212', 'B407', 'B412', 'B429', 'B430',
                     'EC', 'H125', 'H130', 'H135', 'H145', 'H155', 'H160', 'H175', 'H215', 'H225',
                     'AS', 'SA', 'A109', 'A119', 'A139', 'AW', 'S76', 'S92', 'S70', 'S64',
                     'MD5', 'MD6', 'MD9', 'MDHI', 'UH', 'AH', 'CH', 'MH', 'HH',
                     'K1', 'KA', 'MI', 'MIL', 'NH90', 'F28', 'F280', 'EN28', 'BK17', 'BK117', 'EH10']
    return any(t.startswith(p) for p in heli_prefixes)


def is_cargo_aircraft(callsign, aircraft_type):
    """Check if aircraft is cargo based on callsign or type."""
    callsign = (callsign or '').upper().strip()
    aircraft_type = (aircraft_type or '').upper()

    cargo_callsigns = ['FDX', 'UPS', 'GTI', 'ABX', 'ATN', 'CKS', 'WGN', 'PAC', 'SOO',
                       'DHL', 'CLX', 'GEC', 'MPH', 'ASH']
    if any(callsign.startswith(p) for p in cargo_callsigns):
        return True

    # Freighter types (ending in F or specific freighter models)
    if aircraft_type.endswith('F'):
        return True
    cargo_types = ['B763F', 'B744F', 'B748F', 'B77L', 'B77F', 'MD11F', 'A306F', 'A310F', 'A332F', 'A333F']
    if aircraft_type in cargo_types:
        return True

    return False


def is_widebody_aircraft(aircraft_type):
    """Check if aircraft is a widebody (747, 777, 787, A330, A350, A380)."""
    if not aircraft_type:
        return False
    t = aircraft_type.upper()
    widebodies = ['B744', 'B748', 'B772', 'B773', 'B77L', 'B77W', 'B788', 'B789', 'B78X',
                  'A332', 'A333', 'A338', 'A339', 'A342', 'A343', 'A345', 'A346', 'A359', 'A35K', 'A380', 'A388']
    return t in widebodies


def is_boeing_aircraft(aircraft_type):
    """Check if aircraft is a Boeing."""
    if not aircraft_type:
        return False
    t = aircraft_type.upper()
    return (t.startswith('B7') or t.startswith('B73') or t.startswith('B74') or
            t.startswith('B75') or t.startswith('B76') or t.startswith('B78'))


def is_airbus_aircraft(aircraft_type):
    """Check if aircraft is an Airbus."""
    if not aircraft_type:
        return False
    t = aircraft_type.upper()
    return t.startswith('A3') or t.startswith('A2')


def is_turboprop_aircraft(aircraft_type):
    """Check if aircraft is a turboprop (Dash 8, ATR, Beech, Cessna Caravan)."""
    if not aircraft_type:
        return False
    t = aircraft_type.upper()
    return (t.startswith('DH8') or t.startswith('AT') or t == 'SF34' or
            t.startswith('BE') or t == 'C208')


def is_giant_aircraft(aircraft_type):
    """Check if aircraft is a giant (A380, 747-8, C-5, C-17, An-124, B-52)."""
    if not aircraft_type:
        return False
    t = aircraft_type.upper()
    giants = ['A380', 'A388', 'B748', 'AN124', 'AN225', 'C5', 'C5M', 'C17', 'B52']
    return t in giants


def is_ems_helicopter(aircraft_type):
    """Check if aircraft is an EMS-type helicopter."""
    if not aircraft_type:
        return False
    t = aircraft_type.upper()
    ems_types = ['EC35', 'EC45', 'AS50', 'AS55', 'B407', 'B429', 'B412', 'B206',
                 'A109', 'A139', 'A169', 'S76', 'MD52', 'MD50', 'BK17', 'H145', 'H135']
    return t in ems_types


def is_coastguard_aircraft(callsign):
    """Check if aircraft is Coast Guard based on callsign."""
    if not callsign:
        return False
    cs = callsign.upper().strip()
    return cs.startswith('COAST') or cs.startswith('USCG') or cs.startswith('CG')


def is_commercial_aircraft(callsign):
    """Check if aircraft is commercial based on callsign prefix."""
    if not callsign:
        return False
    cs = callsign.upper().strip()
    if len(cs) < 3:
        return False
    prefix = cs[:3]
    commercial_prefixes = {'AAL', 'AAY', 'ACA', 'AFR', 'ASA', 'ASH', 'BAW', 'DAL', 'DLH', 'EDV',
                          'ENY', 'FFT', 'GJS', 'HAL', 'JBU', 'JIA', 'KAL', 'NKS', 'PDT', 'QXE',
                          'RPA', 'SKW', 'SWA', 'TCF', 'UAL', 'VIR', 'VRD', 'WJA', 'EJA', 'LXJ',
                          'ASQ', 'AWI', 'CPZ', 'EGF', 'GXA', 'JZA', 'MXY', 'OPT', 'SCX', 'TRS'}
    return prefix in commercial_prefixes


def get_airline_code(callsign):
    """Get airline code from callsign (DAL, UAL, AAL, SWA, JBU) or None."""
    if not callsign:
        return None
    cs = callsign.upper().strip()
    for code in ['DAL', 'UAL', 'AAL', 'SWA', 'JBU']:
        if cs.startswith(code):
            return code
    return None


def get_intl_carrier(callsign):
    """Get international carrier code from callsign or None."""
    if not callsign:
        return None
    cs = callsign.upper().strip()[:3]
    intl_map = {
        'BAW': 'BA', 'AFR': 'AF', 'DLH': 'LH', 'KLM': 'KL', 'UAE': 'EK',
        'QTR': 'QR', 'SIA': 'SQ', 'ANA': 'NH', 'JAL': 'JL', 'CPA': 'CX',
        'KAL': 'KE', 'TAM': 'JJ', 'ACA': 'AC', 'QFA': 'QF', 'ETH': 'ET',
        'THA': 'TG', 'SAS': 'SK', 'ICA': 'FI', 'AVA': 'AV', 'CCA': 'CA'
    }
    return intl_map.get(cs)


def backfill_summary_tables():
    """
    One-time backfill of summary tables from existing positions data.
    Call this when upgrading an existing database.
    """
    log.info("Starting summary tables backfill...")
    start_time = time.time()

    conn = get_db()
    cursor = conn.cursor()

    # Check if backfill is needed (if stats_summary has no data)
    cursor.execute('SELECT total_positions FROM stats_summary WHERE id = 1')
    row = cursor.fetchone()
    if row and row['total_positions'] > 0:
        log.info("Summary tables already populated, skipping backfill")
        conn.close()
        return

    # Get total count for progress
    cursor.execute('SELECT COUNT(*) as total FROM positions')
    total_positions = cursor.fetchone()['total']

    if total_positions == 0:
        log.info("No positions to backfill")
        conn.close()
        return

    log.info(f"Backfilling from {total_positions:,} positions...")

    # ═══════════════════════════════════════════════════════════════════════════
    # Step 1: Populate aircraft_summary (per-ICAO stats)
    # ═══════════════════════════════════════════════════════════════════════════
    log.info("  Building aircraft_summary...")
    cursor.execute('''
        INSERT OR REPLACE INTO aircraft_summary (
            icao, position_count, session_count, days_seen, first_seen, last_seen,
            aircraft_type, callsigns, max_altitude, max_speed
        )
        SELECT
            icao,
            COUNT(*) as position_count,
            COUNT(DISTINCT strftime('%Y-%m-%d %H:', timestamp) || (CAST(strftime('%M', timestamp) AS INTEGER) / 30)) as session_count,
            COUNT(DISTINCT date(timestamp)) as days_seen,
            MIN(timestamp) as first_seen,
            MAX(timestamp) as last_seen,
            MAX(aircraft_type) as aircraft_type,
            GROUP_CONCAT(DISTINCT callsign) as callsigns,
            MAX(altitude) as max_altitude,
            MAX(speed) as max_speed
        FROM positions
        WHERE icao IS NOT NULL AND icao != ''
        GROUP BY icao
    ''')
    log.info(f"    Populated {cursor.rowcount} aircraft records")

    # Update category flags for each aircraft
    cursor.execute('SELECT icao, callsigns, aircraft_type FROM aircraft_summary')
    for row in cursor.fetchall():
        icao = row['icao']
        callsigns = row['callsigns'] or ''
        callsign = callsigns.split(',')[0] if callsigns else ''
        aircraft_type = row['aircraft_type']

        is_mil = 1 if is_military_aircraft(icao, callsign, aircraft_type) else 0
        is_heli = 1 if is_helicopter_aircraft(aircraft_type, None) else 0
        is_cargo_flag = 1 if is_cargo_aircraft(callsign, aircraft_type) else 0
        is_comm = 1 if is_commercial_aircraft(callsign) else 0
        airline = get_airline_code(callsign)
        # Achievement category flags
        is_wb = 1 if is_widebody_aircraft(aircraft_type) else 0
        is_boeing = 1 if is_boeing_aircraft(aircraft_type) else 0
        is_airbus = 1 if is_airbus_aircraft(aircraft_type) else 0
        is_turbo = 1 if is_turboprop_aircraft(aircraft_type) else 0
        is_giant = 1 if is_giant_aircraft(aircraft_type) else 0
        is_ems = 1 if is_ems_helicopter(aircraft_type) else 0
        is_cg = 1 if is_coastguard_aircraft(callsign) else 0

        cursor.execute('''
            UPDATE aircraft_summary
            SET is_military = ?, is_helicopter = ?, is_cargo = ?, is_commercial = ?, airline = ?,
                is_widebody = ?, is_boeing = ?, is_airbus = ?, is_turboprop = ?, is_giant = ?, is_ems_heli = ?, is_coastguard = ?
            WHERE icao = ?
        ''', (is_mil, is_heli, is_cargo_flag, is_comm, airline, is_wb, is_boeing, is_airbus, is_turbo, is_giant, is_ems, is_cg, icao))

    conn.commit()

    # ═══════════════════════════════════════════════════════════════════════════
    # Step 2: Populate type_summary (per-type stats)
    # ═══════════════════════════════════════════════════════════════════════════
    log.info("  Building type_summary...")
    cursor.execute('''
        INSERT OR REPLACE INTO type_summary (
            aircraft_type, unique_aircraft, total_positions, first_seen, last_seen, sample_icao
        )
        SELECT
            aircraft_type,
            COUNT(DISTINCT icao) as unique_aircraft,
            COUNT(*) as total_positions,
            MIN(timestamp) as first_seen,
            MAX(timestamp) as last_seen,
            MAX(icao) as sample_icao
        FROM positions
        WHERE aircraft_type IS NOT NULL AND aircraft_type != ''
        GROUP BY aircraft_type
    ''')
    log.info(f"    Populated {cursor.rowcount} type records")
    conn.commit()

    # ═══════════════════════════════════════════════════════════════════════════
    # Step 3: Populate tracking tables
    # ═══════════════════════════════════════════════════════════════════════════
    log.info("  Building tracking tables...")

    # Seen callsigns
    cursor.execute('''
        INSERT OR IGNORE INTO seen_callsigns (callsign, first_seen)
        SELECT callsign, MIN(timestamp)
        FROM positions
        WHERE callsign IS NOT NULL AND callsign != ''
        GROUP BY callsign
    ''')

    # Seen days
    cursor.execute('''
        INSERT OR IGNORE INTO seen_days (date)
        SELECT DISTINCT date(timestamp)
        FROM positions
    ''')
    conn.commit()

    # ═══════════════════════════════════════════════════════════════════════════
    # Step 4: Populate stats_summary (global stats)
    # ═══════════════════════════════════════════════════════════════════════════
    log.info("  Building stats_summary...")

    # Basic counts
    cursor.execute('SELECT COUNT(DISTINCT icao) FROM aircraft_summary')
    unique_aircraft = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM type_summary')
    unique_types = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM seen_callsigns')
    unique_callsigns = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM seen_days')
    days_active = cursor.fetchone()[0]

    cursor.execute('SELECT MIN(first_seen), MAX(last_seen) FROM aircraft_summary')
    date_range = cursor.fetchone()
    first_log = date_range[0]
    last_log = date_range[1]

    # Category counts
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_military = 1')
    military_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_helicopter = 1')
    helicopter_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_cargo = 1')
    cargo_count = cursor.fetchone()[0]

    # Achievement category counts
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_widebody = 1')
    widebody_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_boeing = 1')
    boeing_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_airbus = 1')
    airbus_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_turboprop = 1')
    turboprop_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_giant = 1')
    giant_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_ems_heli = 1')
    ems_heli_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_coastguard = 1')
    coastguard_count = cursor.fetchone()[0]

    # Airline counts
    cursor.execute("SELECT COUNT(*) FROM aircraft_summary WHERE airline = 'DAL'")
    delta_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM aircraft_summary WHERE airline = 'UAL'")
    united_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM aircraft_summary WHERE airline = 'AAL'")
    american_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM aircraft_summary WHERE airline = 'SWA'")
    southwest_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM aircraft_summary WHERE airline = 'JBU'")
    jetblue_count = cursor.fetchone()[0]

    # Night/early counts (from positions - these are harder to track incrementally)
    cursor.execute('''
        SELECT COUNT(DISTINCT icao) FROM positions
        WHERE strftime('%H', timestamp) BETWEEN '00' AND '05'
    ''')
    night_count = cursor.fetchone()[0]

    cursor.execute('''
        SELECT COUNT(DISTINCT icao) FROM positions
        WHERE strftime('%H', timestamp) BETWEEN '05' AND '07'
    ''')
    early_count = cursor.fetchone()[0]

    # Records
    cursor.execute('''
        SELECT icao, callsign, altitude, timestamp, aircraft_type
        FROM positions WHERE altitude IS NOT NULL
        ORDER BY altitude DESC LIMIT 1
    ''')
    max_alt_row = cursor.fetchone()

    cursor.execute('''
        SELECT icao, callsign, speed, timestamp, aircraft_type
        FROM positions WHERE speed IS NOT NULL
        ORDER BY speed DESC LIMIT 1
    ''')
    max_speed_row = cursor.fetchone()

    cursor.execute('''
        SELECT icao, callsign, rssi, timestamp, aircraft_type
        FROM positions WHERE rssi IS NOT NULL
        ORDER BY rssi DESC LIMIT 1
    ''')
    best_rssi_row = cursor.fetchone()

    # Lowest altitude (> 500ft to filter ground)
    cursor.execute('''
        SELECT icao, callsign, altitude, timestamp, aircraft_type
        FROM positions WHERE altitude IS NOT NULL AND altitude > 500
        ORDER BY altitude ASC LIMIT 1
    ''')
    lowest_alt_row = cursor.fetchone()

    # Slowest (> 50kt to filter ground)
    cursor.execute('''
        SELECT icao, callsign, speed, timestamp, aircraft_type
        FROM positions WHERE speed IS NOT NULL AND speed > 50
        ORDER BY speed ASC LIMIT 1
    ''')
    slowest_row = cursor.fetchone()

    # Earliest morning catch (00:00-06:59)
    cursor.execute('''
        SELECT icao, callsign, timestamp, aircraft_type, strftime('%H:%M', timestamp) as time_of_day
        FROM positions WHERE strftime('%H', timestamp) BETWEEN '00' AND '06'
        ORDER BY strftime('%H:%M', timestamp) ASC LIMIT 1
    ''')
    earliest_row = cursor.fetchone()

    # Latest night catch (22:00+)
    cursor.execute('''
        SELECT icao, callsign, timestamp, aircraft_type, strftime('%H:%M', timestamp) as time_of_day
        FROM positions WHERE strftime('%H', timestamp) >= '22'
        ORDER BY strftime('%H:%M', timestamp) DESC LIMIT 1
    ''')
    latest_row = cursor.fetchone()

    # Busiest day
    cursor.execute('''
        SELECT date(timestamp) as day, COUNT(DISTINCT icao) as count
        FROM positions GROUP BY day ORDER BY count DESC LIMIT 1
    ''')
    busiest_row = cursor.fetchone()

    # Emergency count
    cursor.execute('''
        SELECT COUNT(DISTINCT icao) FROM positions
        WHERE squawk IN ('7500', '7600', '7700')
    ''')
    emergency_count = cursor.fetchone()[0]

    # Hours covered (bitmask)
    cursor.execute('SELECT DISTINCT CAST(strftime("%H", timestamp) AS INTEGER) as hour FROM positions')
    hours = set(r[0] for r in cursor.fetchall())
    hours_covered = sum(1 << h for h in hours)

    # Max streak
    cursor.execute('''
        WITH daily AS (
            SELECT DISTINCT date(timestamp) as day FROM positions ORDER BY day
        ),
        with_prev AS (
            SELECT day, LAG(day) OVER (ORDER BY day) as prev_day FROM daily
        ),
        streak_groups AS (
            SELECT day,
                SUM(CASE WHEN julianday(day) - julianday(prev_day) > 1 OR prev_day IS NULL THEN 1 ELSE 0 END)
                OVER (ORDER BY day) as streak_group
            FROM with_prev
        )
        SELECT MAX(streak_len) as max_streak FROM (
            SELECT streak_group, COUNT(*) as streak_len FROM streak_groups GROUP BY streak_group
        )
    ''')
    result = cursor.fetchone()
    max_streak = int(result['max_streak']) if result and result['max_streak'] else 0

    # International carriers
    cursor.execute('SELECT DISTINCT callsign FROM positions WHERE callsign IS NOT NULL')
    intl_carriers = set()
    for row in cursor.fetchall():
        carrier = get_intl_carrier(row['callsign'])
        if carrier:
            intl_carriers.add(carrier)
    intl_carriers_str = ','.join(sorted(intl_carriers))

    # Update stats_summary
    cursor.execute('''
        UPDATE stats_summary SET
            total_positions = ?,
            unique_aircraft = ?,
            unique_types = ?,
            unique_callsigns = ?,
            days_active = ?,
            first_log = ?,
            last_log = ?,
            military_count = ?,
            helicopter_count = ?,
            cargo_count = ?,
            night_count = ?,
            early_count = ?,
            delta_count = ?,
            united_count = ?,
            american_count = ?,
            southwest_count = ?,
            jetblue_count = ?,
            max_altitude = ?,
            max_altitude_icao = ?,
            max_altitude_callsign = ?,
            max_altitude_type = ?,
            max_altitude_timestamp = ?,
            max_speed = ?,
            max_speed_icao = ?,
            max_speed_callsign = ?,
            max_speed_type = ?,
            max_speed_timestamp = ?,
            best_rssi = ?,
            best_rssi_icao = ?,
            best_rssi_callsign = ?,
            best_rssi_type = ?,
            best_rssi_timestamp = ?,
            emergency_count = ?,
            hours_covered = ?,
            busiest_day = ?,
            busiest_day_count = ?,
            max_streak = ?,
            intl_carriers_seen = ?,
            last_updated = ?,
            widebody_count = ?,
            boeing_count = ?,
            airbus_count = ?,
            turboprop_count = ?,
            giant_count = ?,
            ems_heli_count = ?,
            coastguard_count = ?,
            lowest_altitude = ?,
            lowest_altitude_icao = ?,
            lowest_altitude_callsign = ?,
            lowest_altitude_type = ?,
            lowest_altitude_timestamp = ?,
            slowest_speed = ?,
            slowest_speed_icao = ?,
            slowest_speed_callsign = ?,
            slowest_speed_type = ?,
            slowest_speed_timestamp = ?,
            earliest_catch_time = ?,
            earliest_catch_icao = ?,
            earliest_catch_callsign = ?,
            earliest_catch_timestamp = ?,
            earliest_catch_type = ?,
            latest_catch_time = ?,
            latest_catch_icao = ?,
            latest_catch_callsign = ?,
            latest_catch_timestamp = ?,
            latest_catch_type = ?
        WHERE id = 1
    ''', (
        total_positions,
        unique_aircraft,
        unique_types,
        unique_callsigns,
        days_active,
        first_log,
        last_log,
        military_count,
        helicopter_count,
        cargo_count,
        night_count,
        early_count,
        delta_count,
        united_count,
        american_count,
        southwest_count,
        jetblue_count,
        max_alt_row['altitude'] if max_alt_row else None,
        max_alt_row['icao'] if max_alt_row else None,
        max_alt_row['callsign'] if max_alt_row else None,
        max_alt_row['aircraft_type'] if max_alt_row else None,
        max_alt_row['timestamp'] if max_alt_row else None,
        max_speed_row['speed'] if max_speed_row else None,
        max_speed_row['icao'] if max_speed_row else None,
        max_speed_row['callsign'] if max_speed_row else None,
        max_speed_row['aircraft_type'] if max_speed_row else None,
        max_speed_row['timestamp'] if max_speed_row else None,
        best_rssi_row['rssi'] if best_rssi_row else None,
        best_rssi_row['icao'] if best_rssi_row else None,
        best_rssi_row['callsign'] if best_rssi_row else None,
        best_rssi_row['aircraft_type'] if best_rssi_row else None,
        best_rssi_row['timestamp'] if best_rssi_row else None,
        emergency_count,
        hours_covered,
        busiest_row['day'] if busiest_row else None,
        busiest_row['count'] if busiest_row else 0,
        max_streak,
        intl_carriers_str,
        datetime.now().isoformat(),
        widebody_count,
        boeing_count,
        airbus_count,
        turboprop_count,
        giant_count,
        ems_heli_count,
        coastguard_count,
        lowest_alt_row['altitude'] if lowest_alt_row else None,
        lowest_alt_row['icao'] if lowest_alt_row else None,
        lowest_alt_row['callsign'] if lowest_alt_row else None,
        lowest_alt_row['aircraft_type'] if lowest_alt_row else None,
        lowest_alt_row['timestamp'] if lowest_alt_row else None,
        slowest_row['speed'] if slowest_row else None,
        slowest_row['icao'] if slowest_row else None,
        slowest_row['callsign'] if slowest_row else None,
        slowest_row['aircraft_type'] if slowest_row else None,
        slowest_row['timestamp'] if slowest_row else None,
        earliest_row['time_of_day'] if earliest_row else None,
        earliest_row['icao'] if earliest_row else None,
        earliest_row['callsign'] if earliest_row else None,
        earliest_row['timestamp'] if earliest_row else None,
        earliest_row['aircraft_type'] if earliest_row else None,
        latest_row['time_of_day'] if latest_row else None,
        latest_row['icao'] if latest_row else None,
        latest_row['callsign'] if latest_row else None,
        latest_row['timestamp'] if latest_row else None,
        latest_row['aircraft_type'] if latest_row else None
    ))

    conn.commit()
    conn.close()

    elapsed = time.time() - start_time
    log.info(f"Summary tables backfill complete in {elapsed:.1f}s")
    log.info(f"  - {unique_aircraft:,} unique aircraft")
    log.info(f"  - {unique_types:,} aircraft types")
    log.info(f"  - {days_active:,} days active")


def update_summary_tables_incremental(aircraft_data, timestamp):
    """
    Incrementally update summary tables with new aircraft data.
    Called after each poll to keep summaries current without full rescans.

    aircraft_data: list of dicts with keys: icao, callsign, aircraft_type, category,
                   altitude, speed, rssi, squawk, hour
    timestamp: current timestamp string
    """
    if not aircraft_data:
        return

    conn = get_db()
    cursor = conn.cursor()

    today = timestamp[:10]  # YYYY-MM-DD
    current_hour = int(timestamp[11:13])  # Hour as int
    session_key = timestamp[:16]  # YYYY-MM-DD HH:MM (30-min granularity handled separately)

    # Track what we need to update in stats_summary
    new_positions = len(aircraft_data)
    new_aircraft_count = 0
    new_types_count = 0
    new_callsigns_count = 0
    new_day = False

    # Check if this is a new day
    cursor.execute('INSERT OR IGNORE INTO seen_days (date) VALUES (?)', (today,))
    if cursor.rowcount > 0:
        new_day = True

    # Process each aircraft
    for ac in aircraft_data:
        icao = ac['icao']
        callsign = ac.get('callsign')
        aircraft_type = ac.get('aircraft_type')
        category = ac.get('category')
        altitude = ac.get('altitude')
        speed = ac.get('speed')
        rssi = ac.get('rssi')
        squawk = ac.get('squawk')

        # Calculate session key (30-min windows)
        minute = int(timestamp[14:16])
        ac_session_key = f"{timestamp[:14]}{30 if minute >= 30 else 0:02d}"

        # ─────────────────────────────────────────────────────────────────────
        # Update aircraft_summary
        # ─────────────────────────────────────────────────────────────────────
        cursor.execute('SELECT * FROM aircraft_summary WHERE icao = ?', (icao,))
        existing = cursor.fetchone()

        if existing:
            # Update existing aircraft
            new_session = 1 if existing['last_session_key'] != ac_session_key else 0
            new_day_for_ac = 1 if existing['last_seen'][:10] != today else 0

            # Update callsigns list (keep last 10 unique)
            existing_callsigns = (existing['callsigns'] or '').split(',')
            if callsign and callsign not in existing_callsigns:
                existing_callsigns.append(callsign)
                existing_callsigns = [c for c in existing_callsigns if c][-10:]
            callsigns_str = ','.join(existing_callsigns)

            # Check for new records (ensure numeric comparison)
            existing_alt = existing['max_altitude']
            existing_spd = existing['max_speed']
            # Handle potential string values in database
            if isinstance(existing_alt, str):
                existing_alt = None
            if isinstance(existing_spd, str):
                existing_spd = None
            new_max_alt = altitude if altitude and isinstance(altitude, (int, float)) and (existing_alt is None or altitude > existing_alt) else existing_alt
            new_max_speed = speed if speed and isinstance(speed, (int, float)) and (existing_spd is None or speed > existing_spd) else existing_spd

            # Update type if we have one and existing doesn't
            new_type = aircraft_type if aircraft_type else existing['aircraft_type']

            cursor.execute('''
                UPDATE aircraft_summary SET
                    position_count = position_count + 1,
                    session_count = session_count + ?,
                    days_seen = days_seen + ?,
                    last_seen = ?,
                    last_session_key = ?,
                    aircraft_type = ?,
                    callsigns = ?,
                    max_altitude = ?,
                    max_speed = ?
                WHERE icao = ?
            ''', (new_session, new_day_for_ac, timestamp, ac_session_key, new_type,
                  callsigns_str, new_max_alt, new_max_speed, icao))
        else:
            # New aircraft
            new_aircraft_count += 1
            is_mil = 1 if is_military_aircraft(icao, callsign, aircraft_type) else 0
            is_heli = 1 if is_helicopter_aircraft(aircraft_type, category) else 0
            is_cargo_flag = 1 if is_cargo_aircraft(callsign, aircraft_type) else 0
            is_comm = 1 if is_commercial_aircraft(callsign) else 0
            airline = get_airline_code(callsign)
            # Achievement category flags
            is_wb = 1 if is_widebody_aircraft(aircraft_type) else 0
            is_boeing = 1 if is_boeing_aircraft(aircraft_type) else 0
            is_airbus = 1 if is_airbus_aircraft(aircraft_type) else 0
            is_turbo = 1 if is_turboprop_aircraft(aircraft_type) else 0
            is_giant = 1 if is_giant_aircraft(aircraft_type) else 0
            is_ems = 1 if is_ems_helicopter(aircraft_type) else 0
            is_cg = 1 if is_coastguard_aircraft(callsign) else 0

            cursor.execute('''
                INSERT INTO aircraft_summary (
                    icao, position_count, session_count, days_seen, first_seen, last_seen,
                    last_session_key, aircraft_type, callsigns, max_altitude, max_speed,
                    is_military, is_helicopter, is_cargo, is_commercial, airline,
                    is_widebody, is_boeing, is_airbus, is_turboprop, is_giant, is_ems_heli, is_coastguard
                ) VALUES (?, 1, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (icao, timestamp, timestamp, ac_session_key, aircraft_type,
                  callsign or '', altitude, speed, is_mil, is_heli, is_cargo_flag, is_comm, airline,
                  is_wb, is_boeing, is_airbus, is_turbo, is_giant, is_ems, is_cg))

            # Update category counts in stats_summary for new aircraft
            if is_mil:
                cursor.execute('UPDATE stats_summary SET military_count = military_count + 1 WHERE id = 1')
            if is_heli:
                cursor.execute('UPDATE stats_summary SET helicopter_count = helicopter_count + 1 WHERE id = 1')
            if is_cargo_flag:
                cursor.execute('UPDATE stats_summary SET cargo_count = cargo_count + 1 WHERE id = 1')
            if is_wb:
                cursor.execute('UPDATE stats_summary SET widebody_count = widebody_count + 1 WHERE id = 1')
            if is_boeing:
                cursor.execute('UPDATE stats_summary SET boeing_count = boeing_count + 1 WHERE id = 1')
            if is_airbus:
                cursor.execute('UPDATE stats_summary SET airbus_count = airbus_count + 1 WHERE id = 1')
            if is_turbo:
                cursor.execute('UPDATE stats_summary SET turboprop_count = turboprop_count + 1 WHERE id = 1')
            if is_giant:
                cursor.execute('UPDATE stats_summary SET giant_count = giant_count + 1 WHERE id = 1')
            if is_ems:
                cursor.execute('UPDATE stats_summary SET ems_heli_count = ems_heli_count + 1 WHERE id = 1')
            if is_cg:
                cursor.execute('UPDATE stats_summary SET coastguard_count = coastguard_count + 1 WHERE id = 1')

        # ─────────────────────────────────────────────────────────────────────
        # Update type_summary
        # ─────────────────────────────────────────────────────────────────────
        if aircraft_type:
            cursor.execute('SELECT * FROM type_summary WHERE aircraft_type = ?', (aircraft_type,))
            existing_type = cursor.fetchone()

            if existing_type:
                # Check if this is a new aircraft for this type
                cursor.execute('''
                    SELECT COUNT(*) FROM aircraft_summary
                    WHERE aircraft_type = ? AND icao = ? AND position_count = 1
                ''', (aircraft_type, icao))
                is_new_for_type = cursor.fetchone()[0] > 0

                cursor.execute('''
                    UPDATE type_summary SET
                        unique_aircraft = unique_aircraft + ?,
                        total_positions = total_positions + 1,
                        last_seen = ?,
                        sample_icao = ?
                    WHERE aircraft_type = ?
                ''', (1 if is_new_for_type else 0, timestamp, icao, aircraft_type))
            else:
                # New type
                new_types_count += 1
                cursor.execute('''
                    INSERT INTO type_summary (aircraft_type, unique_aircraft, total_positions, first_seen, last_seen, sample_icao)
                    VALUES (?, 1, 1, ?, ?, ?)
                ''', (aircraft_type, timestamp, timestamp, icao))

        # ─────────────────────────────────────────────────────────────────────
        # Track new callsigns
        # ─────────────────────────────────────────────────────────────────────
        if callsign:
            cursor.execute('INSERT OR IGNORE INTO seen_callsigns (callsign, first_seen) VALUES (?, ?)',
                          (callsign, timestamp))
            if cursor.rowcount > 0:
                new_callsigns_count += 1

    # ─────────────────────────────────────────────────────────────────────────
    # Update stats_summary
    # ─────────────────────────────────────────────────────────────────────────
    cursor.execute('SELECT * FROM stats_summary WHERE id = 1')
    stats = cursor.fetchone()

    if stats:
        # Update hours covered bitmask
        hours_covered = stats['hours_covered'] or 0
        hours_covered |= (1 << current_hour)

        # Check for new records from this batch
        for ac in aircraft_data:
            altitude = ac.get('altitude')
            speed = ac.get('speed')
            rssi = ac.get('rssi')
            squawk = ac.get('squawk')

            # Max altitude record (ensure numeric comparison)
            stats_max_alt = stats['max_altitude'] if isinstance(stats['max_altitude'], (int, float)) else None
            if altitude and isinstance(altitude, (int, float)) and (stats_max_alt is None or altitude > stats_max_alt):
                cursor.execute('''
                    UPDATE stats_summary SET
                        max_altitude = ?, max_altitude_icao = ?, max_altitude_callsign = ?,
                        max_altitude_type = ?, max_altitude_timestamp = ?
                    WHERE id = 1
                ''', (altitude, ac['icao'], ac.get('callsign'), ac.get('aircraft_type'), timestamp))
                # Refresh stats for next comparison
                cursor.execute('SELECT max_altitude FROM stats_summary WHERE id = 1')
                stats = dict(stats)
                stats['max_altitude'] = cursor.fetchone()[0]

            # Max speed record (ensure numeric comparison)
            stats_max_speed = stats['max_speed'] if isinstance(stats['max_speed'], (int, float)) else None
            if speed and isinstance(speed, (int, float)) and (stats_max_speed is None or speed > stats_max_speed):
                cursor.execute('''
                    UPDATE stats_summary SET
                        max_speed = ?, max_speed_icao = ?, max_speed_callsign = ?,
                        max_speed_type = ?, max_speed_timestamp = ?
                    WHERE id = 1
                ''', (speed, ac['icao'], ac.get('callsign'), ac.get('aircraft_type'), timestamp))
                stats = dict(stats)
                stats['max_speed'] = speed

            # Best RSSI record (ensure numeric comparison)
            stats_best_rssi = stats['best_rssi'] if isinstance(stats['best_rssi'], (int, float)) else None
            if rssi and isinstance(rssi, (int, float)) and (stats_best_rssi is None or rssi > stats_best_rssi):
                cursor.execute('''
                    UPDATE stats_summary SET
                        best_rssi = ?, best_rssi_icao = ?, best_rssi_callsign = ?,
                        best_rssi_type = ?, best_rssi_timestamp = ?
                    WHERE id = 1
                ''', (rssi, ac['icao'], ac.get('callsign'), ac.get('aircraft_type'), timestamp))
                stats = dict(stats)
                stats['best_rssi'] = rssi

            # Emergency squawk
            if squawk in ('7500', '7600', '7700'):
                cursor.execute('UPDATE stats_summary SET emergency_count = emergency_count + 1 WHERE id = 1')

        # Update counters
        cursor.execute('''
            UPDATE stats_summary SET
                total_positions = total_positions + ?,
                unique_aircraft = unique_aircraft + ?,
                unique_types = unique_types + ?,
                unique_callsigns = unique_callsigns + ?,
                days_active = days_active + ?,
                last_log = ?,
                hours_covered = ?,
                last_updated = ?
            WHERE id = 1
        ''', (new_positions, new_aircraft_count, new_types_count, new_callsigns_count,
              1 if new_day else 0, timestamp, hours_covered, timestamp))

        # Update first_log if this is the first data
        if stats['first_log'] is None:
            cursor.execute('UPDATE stats_summary SET first_log = ? WHERE id = 1', (timestamp,))

    conn.commit()
    conn.close()


def save_aircraft(aircraft_list):
    """Save aircraft positions to database."""
    global total_logged

    if not aircraft_list:
        return 0

    conn = get_db()
    cursor = conn.cursor()

    count = 0
    timestamp = datetime.now().isoformat()
    aircraft_data = []  # Collect data for summary update

    for ac in aircraft_list:
        # Skip aircraft without position
        if 'lat' not in ac or 'lon' not in ac:
            continue

        icao = ac.get('hex', '').upper()
        callsign = ac.get('flight', '').strip() if ac.get('flight') else None
        altitude = ac.get('alt_baro') or ac.get('alt_geom')
        # Handle "ground" altitude
        if altitude == 'ground':
            altitude = 0

        # Convert nav_modes array to JSON string if present
        nav_modes = None
        if ac.get('nav_modes'):
            nav_modes = json.dumps(ac.get('nav_modes'))

        # Emergency status
        emergency = ac.get('emergency')
        if emergency == 'none':
            emergency = None

        cursor.execute('''
            INSERT INTO positions (
                icao, callsign, lat, lon, altitude, speed, track, vert_rate,
                squawk, category, aircraft_type, rssi,
                alt_geom, mach, roll, nav_modes, nic, nac_p, db_flags, emergency, on_ground
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            icao,
            callsign,
            ac.get('lat'),
            ac.get('lon'),
            altitude,
            ac.get('gs'),
            ac.get('track'),
            ac.get('baro_rate') or ac.get('geom_rate'),
            ac.get('squawk'),
            ac.get('category'),
            ac.get('t'),
            ac.get('rssi'),
            # New fields
            ac.get('alt_geom'),
            ac.get('mach'),
            ac.get('roll'),
            nav_modes,
            ac.get('nic'),
            ac.get('nac_p'),
            ac.get('dbFlags'),
            emergency,
            1 if ac.get('alt_baro') == 'ground' else 0
        ))
        count += 1

        # Collect data for summary update
        aircraft_data.append({
            'icao': icao,
            'callsign': callsign,
            'aircraft_type': ac.get('t'),
            'category': ac.get('category'),
            'altitude': altitude if isinstance(altitude, (int, float)) else None,
            'speed': ac.get('gs'),
            'rssi': ac.get('rssi'),
            'squawk': ac.get('squawk')
        })

    conn.commit()
    conn.close()

    # Update summary tables incrementally
    if aircraft_data:
        try:
            update_summary_tables_incremental(aircraft_data, timestamp)
        except Exception as e:
            log.warning(f"Summary update error (non-fatal): {e}")

    total_logged += count
    return count

def cleanup_old_records():
    """Delete records older than retention period."""
    if LOG_RETENTION_DAYS <= 0:
        return 0
    
    conn = get_db()
    cursor = conn.cursor()
    
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    cursor.execute('SELECT COUNT(*) FROM positions WHERE timestamp < ?', (cutoff,))
    count = cursor.fetchone()[0]
    
    if count > 0:
        cursor.execute('DELETE FROM positions WHERE timestamp < ?', (cutoff,))
        conn.commit()
        log.info(f"Cleaned up {count} old records (older than {LOG_RETENTION_DAYS} days)")
        # Note: Avoiding VACUUM as it wears SD cards. SQLite reuses deleted space automatically.
    
    conn.close()
    return count

def cleanup_for_storage(min_free_mb=500):
    """Delete oldest records if disk space is low."""
    try:
        stat = os.statvfs('/data')
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        
        if free_mb < min_free_mb:
            conn = get_db()
            cursor = conn.cursor()
            
            # Delete oldest 10% of records
            cursor.execute('SELECT COUNT(*) FROM positions')
            total = cursor.fetchone()[0]
            
            if total > 1000:  # Only cleanup if we have significant data
                delete_count = max(1000, int(total * 0.1))
                cursor.execute('''
                    DELETE FROM positions WHERE id IN (
                        SELECT id FROM positions ORDER BY timestamp ASC LIMIT ?
                    )
                ''', (delete_count,))
                conn.commit()
                log.warning(f"Low disk space ({free_mb:.0f}MB free). Deleted {delete_count} oldest records.")
            
            conn.close()
            return True
    except Exception as e:
        log.error(f"Storage cleanup error: {e}")
    return False

def get_stats():
    """Get logging statistics."""
    conn = get_db()
    cursor = conn.cursor()
    
    # Total positions
    cursor.execute('SELECT COUNT(*) FROM positions')
    total_positions = cursor.fetchone()[0]
    
    # Unique aircraft (ICAO)
    cursor.execute('SELECT COUNT(DISTINCT icao) FROM positions')
    unique_aircraft = cursor.fetchone()[0]
    
    # Unique flights (callsigns)
    cursor.execute('SELECT COUNT(DISTINCT callsign) FROM positions WHERE callsign IS NOT NULL')
    unique_flights = cursor.fetchone()[0]
    
    # Date range
    cursor.execute('SELECT MIN(timestamp), MAX(timestamp) FROM positions')
    row = cursor.fetchone()
    oldest = row[0] if row[0] else None
    newest = row[1] if row[1] else None
    
    # Database size
    conn.close()
    
    db_size = 0
    if os.path.exists(DB_PATH):
        db_size = os.path.getsize(DB_PATH)
    
    # Disk space
    try:
        stat = os.statvfs('/data')
        disk_free = stat.f_bavail * stat.f_frsize
        disk_total = stat.f_blocks * stat.f_frsize
    except:
        disk_free = 0
        disk_total = 0
    
    return {
        'total_positions': total_positions,
        'unique_aircraft': unique_aircraft,
        'unique_flights': unique_flights,
        'oldest_record': oldest,
        'newest_record': newest,
        'storage_bytes': db_size,
        'storage_mb': round(db_size / (1024 * 1024), 2),
        'disk_free_bytes': disk_free,
        'disk_free_mb': round(disk_free / (1024 * 1024), 2),
        'disk_total_bytes': disk_total,
        'disk_total_mb': round(disk_total / (1024 * 1024), 2)
    }

# ══════════════════════════════════════════════════════════════════════════════
# LOGGER THREAD
# ══════════════════════════════════════════════════════════════════════════════

def poll_ultrafeeder():
    """Fetch current aircraft from ultrafeeder."""
    url = f"http://{ULTRAFEEDER_HOST}:{ULTRAFEEDER_PORT}/data/aircraft.json"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        return data.get('aircraft', [])
    except requests.exceptions.RequestException as e:
        log.warning(f"Failed to poll ultrafeeder: {e}")
        return []
    except json.JSONDecodeError as e:
        log.warning(f"Invalid JSON from ultrafeeder: {e}")
        return []

def logger_loop():
    """Main logging loop - runs in background thread."""
    global logger_running, logger_paused, last_poll_time, last_poll_count
    
    log.info(f"Logger started - polling every {LOG_INTERVAL} seconds")
    
    cleanup_counter = 0
    storage_check_counter = 0
    
    while logger_running:
        if not logger_paused:
            aircraft = poll_ultrafeeder()
            count = save_aircraft(aircraft)
            
            last_poll_time = datetime.now().isoformat()
            last_poll_count = count
            
            if count > 0:
                log.debug(f"Logged {count} aircraft positions")
            
            # Cleanup old records every hour (360 polls at 10s interval)
            cleanup_counter += 1
            if cleanup_counter >= 360:
                cleanup_old_records()
                cleanup_counter = 0
            
            # Check storage every 10 minutes (60 polls at 10s interval)
            storage_check_counter += 1
            if storage_check_counter >= 60:
                cleanup_for_storage(min_free_mb=500)  # Keep at least 500MB free
                storage_check_counter = 0
        
        time.sleep(LOG_INTERVAL)
    
    log.info("Logger stopped")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def load_config():
    """Load runtime config from file."""
    global LOG_INTERVAL, LOG_RETENTION_DAYS, logger_paused
    
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                config = json.load(f)
                LOG_INTERVAL = config.get('interval', LOG_INTERVAL)
                LOG_RETENTION_DAYS = config.get('retention_days', LOG_RETENTION_DAYS)
                logger_paused = config.get('paused', False)
                log.info(f"Loaded config: interval={LOG_INTERVAL}s, retention={LOG_RETENTION_DAYS}d, paused={logger_paused}")
        except Exception as e:
            log.warning(f"Could not load config: {e}")

def save_config():
    """Save runtime config to file."""
    config = {
        'interval': LOG_INTERVAL,
        'retention_days': LOG_RETENTION_DAYS,
        'paused': logger_paused
    }
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f)
    except Exception as e:
        log.warning(f"Could not save config: {e}")

def load_user_config():
    """Load user dashboard config from file."""
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, 'r') as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load user config: {e}")
    return {}

def save_user_config(config):
    """Save user dashboard config to file."""
    try:
        with open(USER_CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        log.warning(f"Could not save user config: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

def add_cors_headers(response):
    """Add CORS headers for dashboard access."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.after_request
def after_request(response):
    return add_cors_headers(response)

@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'ok',
        'paused': logger_paused,
        'last_poll': last_poll_time,
        'last_count': last_poll_count,
        'total_logged': total_logged
    })

@app.route('/api/stats')
def api_stats():
    """Get logging statistics."""
    stats = get_stats()
    stats['paused'] = logger_paused
    stats['interval'] = LOG_INTERVAL
    stats['retention_days'] = LOG_RETENTION_DAYS
    stats['last_poll'] = last_poll_time
    stats['last_count'] = last_poll_count
    return jsonify(stats)

@app.route('/api/settings', methods=['GET', 'POST', 'OPTIONS'])
def api_settings():
    """Get or update settings."""
    global LOG_INTERVAL, LOG_RETENTION_DAYS
    
    if request.method == 'OPTIONS':
        return '', 204
    
    if request.method == 'GET':
        return jsonify({
            'interval': LOG_INTERVAL,
            'retention_days': LOG_RETENTION_DAYS,
            'paused': logger_paused
        })
    
    # POST - update settings
    data = request.get_json()
    
    if 'interval' in data:
        new_interval = int(data['interval'])
        if 5 <= new_interval <= 60:
            LOG_INTERVAL = new_interval
            log.info(f"Interval updated to {LOG_INTERVAL}s")
    
    if 'retention_days' in data:
        new_retention = int(data['retention_days'])
        if new_retention >= 0:
            LOG_RETENTION_DAYS = new_retention
            log.info(f"Retention updated to {LOG_RETENTION_DAYS} days")
    
    save_config()
    
    return jsonify({
        'success': True,
        'interval': LOG_INTERVAL,
        'retention_days': LOG_RETENTION_DAYS
    })

@app.route('/api/userconfig', methods=['GET', 'POST', 'OPTIONS'])
def api_userconfig():
    """Get or update user dashboard config (e.g., ADSBx Short ID)."""
    if request.method == 'OPTIONS':
        return '', 204
    
    if request.method == 'GET':
        config = load_user_config()
        return jsonify(config)
    
    # POST - update user config
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    # Load existing config and merge with new data
    config = load_user_config()
    config.update(data)
    
    if save_user_config(config):
        log.info(f"User config updated: {list(data.keys())}")
        return jsonify({'success': True, 'config': config})
    else:
        return jsonify({'success': False, 'error': 'Could not save config'}), 500

@app.route('/api/pause', methods=['POST', 'OPTIONS'])
def api_pause():
    """Pause logging."""
    global logger_paused
    if request.method == 'OPTIONS':
        return '', 204
    logger_paused = True
    save_config()
    log.info("Logging paused")
    return jsonify({'success': True, 'paused': True})

@app.route('/api/resume', methods=['POST', 'OPTIONS'])
def api_resume():
    """Resume logging."""
    global logger_paused
    if request.method == 'OPTIONS':
        return '', 204
    logger_paused = False
    save_config()
    log.info("Logging resumed")
    return jsonify({'success': True, 'paused': False})

@app.route('/api/clear', methods=['POST', 'OPTIONS'])
def api_clear():
    """Clear all logs."""
    global total_logged
    if request.method == 'OPTIONS':
        return '', 204
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM positions')
    cursor.execute('VACUUM')
    conn.commit()
    conn.close()
    
    total_logged = 0
    log.info("All logs cleared")
    
    return jsonify({'success': True})

@app.route('/api/export')
def api_export_csv():
    """Export logs as CSV."""
    # Get optional date filters
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    
    conn = get_db()
    cursor = conn.cursor()
    
    query = 'SELECT timestamp, icao, callsign, lat, lon, altitude, speed, track, vert_rate, squawk, category, aircraft_type, rssi FROM positions'
    params = []
    
    if start_date or end_date:
        conditions = []
        if start_date:
            conditions.append('timestamp >= ?')
            params.append(start_date)
        if end_date:
            conditions.append('timestamp <= ?')
            params.append(end_date)
        query += ' WHERE ' + ' AND '.join(conditions)
    
    query += ' ORDER BY timestamp'
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    # Generate CSV
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(['timestamp', 'icao', 'callsign', 'lat', 'lon', 'altitude', 'speed', 'track', 'vert_rate', 'squawk', 'category', 'aircraft_type', 'rssi'])
    
    for row in rows:
        # Convert row to list and format timestamp with T separator
        row_list = list(row)
        if row_list[0]:
            row_list[0] = row_list[0].replace(' ', 'T')
        writer.writerow(row_list)
    
    output.seek(0)
    
    filename = f"flights_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/api/export/json')
def api_export_json():
    """Export logs as JSON."""
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    
    conn = get_db()
    cursor = conn.cursor()
    
    query = 'SELECT timestamp, icao, callsign, lat, lon, altitude, speed, track, vert_rate, squawk, category, aircraft_type, rssi FROM positions'
    params = []
    
    if start_date or end_date:
        conditions = []
        if start_date:
            conditions.append('timestamp >= ?')
            params.append(start_date)
        if end_date:
            conditions.append('timestamp <= ?')
            params.append(end_date)
        query += ' WHERE ' + ' AND '.join(conditions)
    
    query += ' ORDER BY timestamp'
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    flights = []
    for row in rows:
        flights.append({
            'timestamp': row['timestamp'],
            'icao': row['icao'],
            'callsign': row['callsign'],
            'lat': row['lat'],
            'lon': row['lon'],
            'altitude': row['altitude'],
            'speed': row['speed'],
            'track': row['track'],
            'vert_rate': row['vert_rate'],
            'squawk': row['squawk'],
            'category': row['category'],
            'aircraft_type': row['aircraft_type'],
            'rssi': row['rssi']
        })
    
    filename = f"flights_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    return Response(
        json.dumps(flights, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/api/flights')
def api_flights():
    """Query flights with filters."""
    icao = request.args.get('icao')
    callsign = request.args.get('callsign')
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    limit = int(request.args.get('limit', 100))
    
    conn = get_db()
    cursor = conn.cursor()
    
    query = 'SELECT DISTINCT icao, callsign, MIN(timestamp) as first_seen, MAX(timestamp) as last_seen, COUNT(*) as positions FROM positions'
    conditions = []
    params = []
    
    if icao:
        conditions.append('icao LIKE ?')
        params.append(f'%{icao.upper()}%')
    if callsign:
        conditions.append('callsign LIKE ?')
        params.append(f'%{callsign.upper()}%')
    if start_date:
        conditions.append('timestamp >= ?')
        params.append(start_date)
    if end_date:
        conditions.append('timestamp <= ?')
        params.append(end_date)
    
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    
    query += ' GROUP BY icao, callsign ORDER BY last_seen DESC LIMIT ?'
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    flights = []
    for row in rows:
        flights.append({
            'icao': row['icao'],
            'callsign': row['callsign'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
            'positions': row['positions']
        })
    
    return jsonify(flights)

@app.route('/api/trace/<icao>')
def api_trace(icao):
    """Get flight path for specific aircraft."""
    # Optional time filter
    start_time = request.args.get('start')
    end_time = request.args.get('end')
    
    conn = get_db()
    cursor = conn.cursor()
    
    query = 'SELECT timestamp, lat, lon, altitude, speed, track FROM positions WHERE UPPER(icao) = ?'
    params = [icao.upper()]
    
    if start_time:
        query += ' AND timestamp >= ?'
        params.append(start_time)
    if end_time:
        query += ' AND timestamp <= ?'
        params.append(end_time)
    
    query += ' ORDER BY timestamp'
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    trace = []
    for row in rows:
        trace.append({
            'timestamp': row['timestamp'],
            'lat': row['lat'],
            'lon': row['lon'],
            'altitude': row['altitude'],
            'speed': row['speed'],
            'track': row['track']
        })
    
    return jsonify({
        'icao': icao.upper(),
        'positions': len(trace),
        'trace': trace
    })

@app.route('/api/recent')
def api_recent():
    """Get recent unique aircraft (last hour)."""
    conn = get_db()
    cursor = conn.cursor()
    
    one_hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
    
    cursor.execute('''
        SELECT icao, callsign, MAX(lat) as lat, MAX(lon) as lon, MAX(altitude) as altitude, 
               MAX(timestamp) as last_seen, COUNT(*) as positions
        FROM positions 
        WHERE timestamp >= ?
        GROUP BY icao
        ORDER BY last_seen DESC
        LIMIT 50
    ''', (one_hour_ago,))
    
    rows = cursor.fetchall()
    conn.close()
    
    aircraft = []
    for row in rows:
        aircraft.append({
            'icao': row['icao'],
            'callsign': row['callsign'],
            'lat': row['lat'],
            'lon': row['lon'],
            'altitude': row['altitude'],
            'last_seen': row['last_seen'],
            'positions': row['positions']
        })
    
    return jsonify(aircraft)

# ══════════════════════════════════════════════════════════════════════════════
# LEADERBOARD API
# ══════════════════════════════════════════════════════════════════════════════

# Leaderboard category definitions (module-level for reuse)
COMMERCIAL_PREFIXES = {'AAL', 'AAY', 'ACA', 'AFR', 'ASA', 'ASH', 'BAW', 'DAL', 'DLH', 'EDV',
                      'ENY', 'FFT', 'GJS', 'HAL', 'JBU', 'JIA', 'KAL', 'NKS', 'PDT', 'QXE',
                      'RPA', 'SKW', 'SWA', 'TCF', 'UAL', 'VIR', 'VRD', 'WJA', 'EJA', 'LXJ',
                      'ASQ', 'AWI', 'CPZ', 'EGF', 'GXA', 'JZA', 'MXY', 'OPT', 'SCX', 'TRS'}
CARGO_PREFIXES = {'FDX', 'UPS', 'GTI', 'ABX', 'ATN', 'CLX', 'GEC', 'MPH', 'PAC', 'POL', 'SWQ', 'KFS', 'TWY'}
MILITARY_PREFIXES = {'RCH', 'DUKE', 'EVAC', 'REACH', 'KING', 'NOBLE', 'JUDGE', 'VALOR', 'TOPCT', 'CNV', 'PAT',
                     'NAVY', 'ARMY', 'COAST', 'GUARD'}
MILITARY_TYPES = {'V22', 'C17', 'C5M', 'C130', 'C5', 'KC10', 'KC135', 'KC46', 'E3', 'E6', 'E8',
                  'P8', 'P3', 'EP3', 'RC135', 'U2', 'B1', 'B2', 'B52', 'F15', 'F16', 'F18',
                  'F22', 'F35', 'A10', 'MH60', 'UH60', 'CH47', 'AH64', 'MV22', 'CV22', 'H60', 'C40'}
HELICOPTER_TYPES = {'R22', 'R44', 'R66', 'B06', 'B06T', 'B206', 'B407', 'B429', 'B430', 'EC20',
                   'EC25', 'EC30', 'EC35', 'EC45', 'EC55', 'EC75', 'A109', 'A119', 'A139', 'A169',
                   'AS50', 'AS55', 'AS65', 'H500', 'H60', 'UH60', 'S76', 'S92', 'BK17', 'MD52',
                   'MD50', 'MD60', 'MD90', 'B505', 'H125', 'H130', 'H135', 'H145', 'H155', 'H160',
                   'H175', 'H215', 'H225', 'EH10', 'NH90', 'AW09', 'AW10', 'AW13', 'AW16', 'AW18'}


def get_aircraft_category(callsigns, aircraft_type, icao):
    """Categorize aircraft based on callsigns, type, and ICAO."""
    # Check ICAO for military first (US military ranges AE/AF)
    if icao:
        icao_upper = icao.upper()
        if icao_upper.startswith(('AE', 'AF')):
            return 'military'

    # Check callsigns
    for cs in callsigns:
        if not cs:
            continue
        cs_upper = cs.upper()
        prefix = cs_upper[:3] if len(cs_upper) >= 3 else cs_upper
        if prefix in CARGO_PREFIXES:
            return 'cargo'
        if prefix in COMMERCIAL_PREFIXES:
            return 'commercial'
        if prefix in MILITARY_PREFIXES or cs_upper.startswith(('RCH', 'EVAC', 'REACH', 'NAVY', 'ARMY', 'COAST', 'GUARD')):
            return 'military'

    # Check aircraft type for military
    if aircraft_type:
        type_upper = aircraft_type.upper()
        if type_upper in MILITARY_TYPES:
            return 'military'
        # Also check helicopter types
        for heli_type in HELICOPTER_TYPES:
            if type_upper.startswith(heli_type) or heli_type in type_upper:
                return 'helicopter'

    return 'private'


def calculate_leaderboard_for_range(range_param):
    """Calculate leaderboard for a specific time range."""
    now = datetime.now()
    if range_param == '24h':
        cutoff = now - timedelta(hours=24)
    elif range_param == '7d':
        cutoff = now - timedelta(days=7)
    elif range_param == '14d':
        cutoff = now - timedelta(days=14)
    elif range_param == '30d':
        cutoff = now - timedelta(days=30)
    else:
        cutoff = None

    conn = get_db()
    cursor = conn.cursor()

    # Use aircraft_summary table for all ranges (FAST!)
    # Filter by last_seen for time-based ranges
    if cutoff:
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        # Use summary table filtered by last_seen - much faster than scanning positions
        cursor.execute('''
            SELECT icao, sessions, days_seen, first_seen, last_seen, aircraft_type, all_callsigns
            FROM (
                SELECT icao, session_count as sessions, days_seen, first_seen, last_seen,
                       aircraft_type, callsigns as all_callsigns
                FROM aircraft_summary
                WHERE last_seen >= ?
                ORDER BY session_count DESC LIMIT 200
            )
            UNION
            SELECT icao, sessions, days_seen, first_seen, last_seen, aircraft_type, all_callsigns
            FROM (
                SELECT icao, session_count as sessions, days_seen, first_seen, last_seen,
                       aircraft_type, callsigns as all_callsigns
                FROM aircraft_summary
                WHERE last_seen >= ? AND substr(icao, 1, 2) IN ('AE', 'AF')
                ORDER BY session_count DESC LIMIT 50
            )
        ''', (cutoff_str, cutoff_str))
        rows = cursor.fetchall()
    else:
        # Use aircraft_summary for all-time (instant query vs scanning 1.6M+ rows)
        # Single UNION query for top 200 + top 50 military (AE/AF prefixes)
        cursor.execute('''
            SELECT icao, sessions, days_seen, first_seen, last_seen, aircraft_type, all_callsigns
            FROM (
                SELECT icao, session_count as sessions, days_seen, first_seen, last_seen,
                       aircraft_type, callsigns as all_callsigns
                FROM aircraft_summary ORDER BY session_count DESC LIMIT 200
            )
            UNION
            SELECT icao, sessions, days_seen, first_seen, last_seen, aircraft_type, all_callsigns
            FROM (
                SELECT icao, session_count as sessions, days_seen, first_seen, last_seen,
                       aircraft_type, callsigns as all_callsigns
                FROM aircraft_summary
                WHERE substr(icao, 1, 2) IN ('AE', 'AF')
                ORDER BY session_count DESC LIMIT 50
            )
        ''')
        rows = cursor.fetchall()

    conn.close()

    # Process and categorize
    results = []
    for row in rows:
        callsigns = row['all_callsigns'].split(',') if row['all_callsigns'] else []
        callsigns = [c for c in callsigns if c and c != 'None']
        ac_category = get_aircraft_category(callsigns, row['aircraft_type'], row['icao'])

        results.append({
            'icao': row['icao'],
            'sessions': row['sessions'],
            'days_seen': row['days_seen'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
            'aircraft_type': row['aircraft_type'],
            'callsigns': callsigns[:5],
            'category': ac_category
        })

    return results


def calculate_leaderboard():
    """Calculate leaderboard for all time ranges - runs in background thread."""
    results = {}
    for range_param in ['24h', '7d', '14d', '30d', 'all']:
        start = time.time()
        results[range_param] = calculate_leaderboard_for_range(range_param)
        log.info(f"    leaderboard {range_param}: {time.time() - start:.1f}s")
    return results


@app.route('/api/leaderboard')
def api_leaderboard():
    """Return cached leaderboard with category filtering."""
    if not leaderboard_cache['data']:
        return jsonify({'error': 'Leaderboard not yet calculated, please wait...'}), 503

    range_param = request.args.get('range', '7d')
    limit = int(request.args.get('limit', 20))
    category = request.args.get('category', 'all')

    # Get cached data for this range
    cached_data = leaderboard_cache['data'].get(range_param, [])

    # Filter by category and apply limit
    leaderboard = []
    rank = 0
    for item in cached_data:
        if category != 'all' and item['category'] != category:
            continue
        rank += 1
        entry = item.copy()
        entry['rank'] = rank
        leaderboard.append(entry)
        if len(leaderboard) >= limit:
            break

    return jsonify({
        'range': range_param,
        'category': category,
        'count': len(leaderboard),
        'aircraft': leaderboard,
        'cached': True,
        'cache_age': int(time.time() - leaderboard_cache['last_updated']) if leaderboard_cache['last_updated'] else None
    })

@app.route('/api/aircraft/<icao>')
def api_aircraft_detail(icao):
    """Get detailed stats and info for a specific aircraft.
    OPTIMIZED: Uses aircraft_summary table for instant lookups (was 70s, now <0.1s)."""
    icao = icao.upper()

    # Check cache first (15 minute TTL - longer than refresh cycle)
    cache_key = icao
    if cache_key in aircraft_detail_cache:
        cached = aircraft_detail_cache[cache_key]
        if time.time() - cached['time'] < 900:  # 15 minute TTL
            response = cached['data'].copy()
            response['cached'] = True
            response['cache_age'] = int(time.time() - cached['time'])
            return jsonify(response)

    conn = get_db()
    cursor = conn.cursor()

    # FAST: Get basic stats from aircraft_summary (primary key lookup = instant)
    cursor.execute('''
        SELECT icao, position_count, session_count, days_seen, first_seen, last_seen,
               aircraft_type, callsigns, max_altitude, max_speed,
               is_military, is_helicopter, is_cargo, is_commercial, airline
        FROM aircraft_summary WHERE icao = ?
    ''', (icao,))
    summary = cursor.fetchone()

    if not summary:
        conn.close()
        return jsonify({'error': 'Aircraft not found'}), 404

    # Parse callsigns from stored JSON/comma-separated string
    callsigns = []
    if summary['callsigns']:
        try:
            cs_list = summary['callsigns'].split(',') if ',' in summary['callsigns'] else [summary['callsigns']]
            callsigns = [{'callsign': cs.strip(), 'count': 1} for cs in cs_list if cs.strip()]
        except:
            pass

    # Create stats dict from summary
    stats = {
        'total_positions': summary['position_count'],
        'sessions': summary['session_count'],
        'days_seen': summary['days_seen'],
        'first_seen': summary['first_seen'],
        'last_seen': summary['last_seen'],
        'aircraft_type': summary['aircraft_type'],
        'max_altitude': summary['max_altitude'],
        'max_speed': summary['max_speed'],
        'unique_callsigns': len(callsigns),
        'min_altitude': None,
        'avg_altitude': None,
        'min_speed': None,
        'avg_speed': None,
        'avg_rssi': None,
        'min_rssi': None,
        'max_rssi': None
    }

    # Get detailed stats with ONE optimized query (uses icao index)
    cursor.execute('''
        SELECT
            MIN(altitude) as min_alt, AVG(altitude) as avg_alt,
            MIN(speed) as min_spd, AVG(speed) as avg_spd,
            AVG(rssi) as avg_rssi, MIN(rssi) as min_rssi, MAX(rssi) as max_rssi,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) BETWEEN 5 AND 11 THEN 1 ELSE 0 END) as morning,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) BETWEEN 12 AND 17 THEN 1 ELSE 0 END) as afternoon,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) BETWEEN 18 AND 21 THEN 1 ELSE 0 END) as evening,
            SUM(CASE WHEN CAST(strftime('%H', timestamp) AS INTEGER) >= 22 OR CAST(strftime('%H', timestamp) AS INTEGER) < 5 THEN 1 ELSE 0 END) as night,
            SUM(CASE WHEN track BETWEEN 315 AND 360 OR track BETWEEN 0 AND 45 THEN 1 ELSE 0 END) as north,
            SUM(CASE WHEN track BETWEEN 45 AND 135 THEN 1 ELSE 0 END) as east,
            SUM(CASE WHEN track BETWEEN 135 AND 225 THEN 1 ELSE 0 END) as south,
            SUM(CASE WHEN track BETWEEN 225 AND 315 THEN 1 ELSE 0 END) as west
        FROM positions WHERE icao = ?
    ''', (icao,))
    detail = cursor.fetchone()

    if detail:
        stats['min_altitude'] = detail['min_alt']
        stats['avg_altitude'] = detail['avg_alt']
        stats['min_speed'] = detail['min_spd']
        stats['avg_speed'] = detail['avg_spd']
        stats['avg_rssi'] = detail['avg_rssi']
        stats['min_rssi'] = detail['min_rssi']
        stats['max_rssi'] = detail['max_rssi']

    # Build time pattern from query results
    time_pattern = {}
    if detail:
        if detail['morning']: time_pattern['morning'] = detail['morning']
        if detail['afternoon']: time_pattern['afternoon'] = detail['afternoon']
        if detail['evening']: time_pattern['evening'] = detail['evening']
        if detail['night']: time_pattern['night'] = detail['night']

    # Build direction pattern from query results
    direction_pattern = {}
    if detail:
        if detail['north']: direction_pattern['North'] = detail['north']
        if detail['east']: direction_pattern['East'] = detail['east']
        if detail['south']: direction_pattern['South'] = detail['south']
        if detail['west']: direction_pattern['West'] = detail['west']

    time_badges = {'early_bird': 0, 'night_owl': 0}
    milk_runs = []
    flights = []

    conn.close()

    # Calculate badges from summary data (no queries needed)
    badges = []

    # Frequency badges - based on number of times this aircraft passed through your coverage area
    if stats['sessions'] >= 50:
        badges.append({'id': 'frequent', 'name': 'Frequent Flyer', 'icon': '🏆', 'desc': f"Seen {stats['sessions']} times - a very frequent visitor to your airspace"})
    elif stats['sessions'] >= 20:
        badges.append({'id': 'regular', 'name': 'Regular', 'icon': '✈️', 'desc': f"Seen {stats['sessions']} times - regularly passes through your area"})
    elif stats['sessions'] >= 10:
        badges.append({'id': 'familiar', 'name': 'Familiar Face', 'icon': '👋', 'desc': f"Seen {stats['sessions']} times - becoming a familiar aircraft"})

    # Time-based badges - how many different days this aircraft was spotted
    if stats['days_seen'] >= 7:
        badges.append({'id': 'local', 'name': 'Local Regular', 'icon': '🏠', 'desc': f"Spotted on {stats['days_seen']} different days - likely operates routes through your area"})

    # Altitude badges
    if stats['max_altitude'] and stats['max_altitude'] >= 40000:
        badges.append({'id': 'highflyer', 'name': 'High Flyer', 'icon': '🔝', 'desc': f"Reached {stats['max_altitude']:,}ft - cruises at high altitude"})

    # Category badges from summary flags
    if summary['is_military']:
        badges.append({'id': 'military', 'name': 'Military', 'icon': '🎖️', 'desc': 'Military aircraft - identified by callsign pattern or type'})
    if summary['is_helicopter']:
        badges.append({'id': 'helicopter', 'name': 'Rotorcraft', 'icon': '🚁', 'desc': 'Helicopter or rotorcraft - identified by aircraft type'})
    if summary['is_cargo']:
        badges.append({'id': 'cargo', 'name': 'Cargo', 'icon': '📦', 'desc': 'Cargo/freight aircraft - FedEx, UPS, DHL, etc.'})

    # Signal badge
    if stats['avg_rssi'] and stats['avg_rssi'] > -15:
        badges.append({'id': 'signalking', 'name': 'Signal King', 'icon': '📡', 'desc': f"Avg signal {stats['avg_rssi']:.1f} dB - consistently strong ADS-B signal"})

    # Globe trotter - seen with many different callsigns (different flight numbers)
    if len(callsigns) >= 10:
        badges.append({'id': 'globetrotter', 'name': 'Globe Trotter', 'icon': '🌍', 'desc': f"Used {len(callsigns)} different flight numbers - flies many different routes"})
    
    # Determine primary direction
    primary_direction = max(direction_pattern, key=direction_pattern.get) if direction_pattern else None
    
    # Determine primary time
    primary_time = max(time_pattern, key=time_pattern.get) if time_pattern else None
    
    # Build response data
    response_data = {
        'icao': icao,
        'aircraft_type': stats['aircraft_type'],
        'stats': {
            'total_positions': stats['total_positions'],
            'sessions': stats['sessions'],  # fly-bys
            'days_seen': stats['days_seen'],
            'first_seen': stats['first_seen'],
            'last_seen': stats['last_seen'],
            'min_altitude': stats['min_altitude'],
            'max_altitude': stats['max_altitude'],
            'avg_altitude': round(stats['avg_altitude']) if stats['avg_altitude'] else None,
            'min_speed': stats['min_speed'],
            'max_speed': stats['max_speed'],
            'avg_speed': round(stats['avg_speed']) if stats['avg_speed'] else None,
            'avg_rssi': round(stats['avg_rssi'], 1) if stats['avg_rssi'] else None
        },
        'patterns': {
            'primary_time': primary_time,
            'time_breakdown': time_pattern,
            'primary_direction': primary_direction,
            'direction_breakdown': direction_pattern
        },
        'callsigns': callsigns,
        'milk_runs': milk_runs,
        'badges': badges,
        'flights': flights
    }

    # Cache the result (15 minute TTL)
    aircraft_detail_cache[icao] = {'data': response_data, 'time': time.time()}

    # Clean old cache entries (keep only last 500 - enough for all prefetched + user clicks)
    if len(aircraft_detail_cache) > 500:
        oldest_keys = sorted(aircraft_detail_cache.keys(),
                           key=lambda k: aircraft_detail_cache[k]['time'])[:200]
        for k in oldest_keys:
            del aircraft_detail_cache[k]

    return jsonify(response_data)

@app.route('/api/aircraft/<icao>/photo')
def api_aircraft_photo(icao):
    """Proxy to planespotters.net API for aircraft photos (avoids CORS)."""
    icao = icao.upper()
    try:
        response = requests.get(
            f'https://api.planespotters.net/pub/photos/hex/{icao}',
            timeout=5,
            headers={'User-Agent': 'EasyADSB-Logger/1.0'}
        )
        if response.ok:
            return jsonify(response.json())
        return jsonify({'photos': []})
    except Exception as e:
        log.debug(f"Photo fetch failed for {icao}: {e}")
        return jsonify({'photos': []})

@app.route('/api/callsign/<callsign>')
def api_callsign_detail(callsign):
    """Get details about a specific callsign - which aircraft used it, when seen, etc."""
    callsign = callsign.upper()

    conn = get_db()
    cursor = conn.cursor()

    # Get all aircraft that have used this callsign
    cursor.execute('''
        SELECT DISTINCT p.icao, p.aircraft_type,
            COUNT(*) as position_count,
            MIN(p.timestamp) as first_seen,
            MAX(p.timestamp) as last_seen
        FROM positions p
        WHERE UPPER(p.callsign) = ?
        GROUP BY p.icao
        ORDER BY last_seen DESC
        LIMIT 20
    ''', (callsign,))

    aircraft = []
    for row in cursor.fetchall():
        aircraft.append({
            'icao': row['icao'],
            'type': row['aircraft_type'],
            'positions': row['position_count'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen']
        })

    # Get recent sightings (last 30 days) - simplified query
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    cursor.execute('''
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM positions
        WHERE UPPER(callsign) = ? AND timestamp >= ?
        GROUP BY DATE(timestamp)
        ORDER BY date DESC
        LIMIT 14
    ''', (callsign, cutoff))

    sightings = []
    for row in cursor.fetchall():
        sightings.append({
            'date': row['date'],
            'count': row['count']
        })

    # Get total stats - simplified
    cursor.execute('''
        SELECT COUNT(DISTINCT DATE(timestamp)) as days_seen,
            MIN(timestamp) as first_ever,
            MAX(timestamp) as last_ever
        FROM positions
        WHERE UPPER(callsign) = ?
        LIMIT 1
    ''', (callsign,))

    stats_row = cursor.fetchone()
    stats = {
        'days_seen': stats_row['days_seen'] if stats_row else 0,
        'first_seen': stats_row['first_ever'] if stats_row else None,
        'last_seen': stats_row['last_ever'] if stats_row else None
    }

    conn.close()

    return jsonify({
        'callsign': callsign,
        'aircraft': aircraft,
        'sightings': sightings,
        'stats': stats
    })


@app.route('/api/aircraft/<icao>/trace')
def api_aircraft_trace_all(icao):
    """Get all position traces for an aircraft (for trail overlay view)."""
    icao = icao.upper()
    range_param = request.args.get('range', '7d')
    
    # Calculate cutoff time
    now = datetime.now()
    if range_param == '24h':
        cutoff = now - timedelta(hours=24)
    elif range_param == '7d':
        cutoff = now - timedelta(days=7)
    elif range_param == '14d':
        cutoff = now - timedelta(days=14)
    elif range_param == '30d':
        cutoff = now - timedelta(days=30)
    else:
        cutoff = None
    
    conn = get_db()
    cursor = conn.cursor()
    
    if cutoff:
        cursor.execute('''
            SELECT timestamp, lat, lon, altitude, speed, track, callsign
            FROM positions
            WHERE UPPER(icao) = ? AND timestamp >= ? AND lat IS NOT NULL
            ORDER BY timestamp
        ''', (icao, cutoff.isoformat()))
    else:
        cursor.execute('''
            SELECT timestamp, lat, lon, altitude, speed, track, callsign
            FROM positions
            WHERE UPPER(icao) = ? AND lat IS NOT NULL
            ORDER BY timestamp
        ''', (icao,))
    
    rows = cursor.fetchall()
    conn.close()
    
    # Group into separate flight sessions (gap > 30 min = new flight)
    flights = []
    current_flight = []
    last_time = None
    
    for row in rows:
        row_time = datetime.fromisoformat(row['timestamp'])
        
        if last_time and (row_time - last_time).total_seconds() > 1800:  # 30 min gap
            if current_flight:
                flights.append(current_flight)
            current_flight = []
        
        current_flight.append({
            'ts': row['timestamp'],
            'lat': row['lat'],
            'lon': row['lon'],
            'alt': row['altitude'],
            'spd': row['speed'],
            'trk': row['track'],
            'cs': row['callsign']
        })
        last_time = row_time
    
    if current_flight:
        flights.append(current_flight)
    
    return jsonify({
        'icao': icao,
        'range': range_param,
        'flight_count': len(flights),
        'total_positions': len(rows),
        'flights': flights
    })

# ══════════════════════════════════════════════════════════════════════════════
# STATS & ACHIEVEMENTS API
# ══════════════════════════════════════════════════════════════════════════════

def calculate_overview():
    """Calculate stats overview - runs in background thread.
    Uses pre-aggregated summary tables for instant queries."""
    conn = get_db()
    cursor = conn.cursor()

    # Get basic stats from stats_summary (INSTANT - single row lookup)
    cursor.execute('SELECT * FROM stats_summary WHERE id = 1')
    stats = cursor.fetchone()

    if stats and stats['total_positions'] > 0:
        total_positions = stats['total_positions']
        unique_aircraft = stats['unique_aircraft']
        unique_types = stats['unique_types']
        unique_flights = stats['unique_callsigns']
        days_active = stats['days_active']
        first_log = stats['first_log']
        last_log = stats['last_log']

        # Category counts from aircraft_summary (pre-computed flags)
        cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_helicopter = 1')
        heli_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_cargo = 1')
        cargo_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_military = 1')
        military_count = cursor.fetchone()[0]

        # Commercial = all commercial airlines (using is_commercial flag)
        cursor.execute('SELECT COUNT(*) FROM aircraft_summary WHERE is_commercial = 1')
        commercial_count = cursor.fetchone()[0]

        # Calculate private as mutually exclusive category
        # Private = not helicopter, not military, not cargo, not commercial
        cursor.execute('''
            SELECT COUNT(*) FROM aircraft_summary
            WHERE is_helicopter = 0 AND is_military = 0 AND is_cargo = 0 AND is_commercial = 0
        ''')
        private_count = cursor.fetchone()[0]

        categories = {
            'helicopter': heli_count,
            'cargo': cargo_count,
            'commercial': commercial_count,
            'military': military_count,
            'private': private_count
        }
    else:
        # Fallback to direct queries if summary not populated
        cursor.execute('SELECT COUNT(*) as total FROM positions')
        total_positions = cursor.fetchone()['total']

        cursor.execute('SELECT COUNT(DISTINCT icao) FROM positions WHERE icao IS NOT NULL')
        unique_aircraft = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(DISTINCT aircraft_type) FROM positions WHERE aircraft_type IS NOT NULL AND aircraft_type != ""')
        unique_types = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(DISTINCT callsign) FROM positions WHERE callsign IS NOT NULL AND callsign != ""')
        unique_flights = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(DISTINCT date(timestamp)) FROM positions')
        days_active = cursor.fetchone()[0]

        cursor.execute('SELECT MIN(timestamp), MAX(timestamp) FROM positions')
        date_range = cursor.fetchone()
        first_log = date_range[0]
        last_log = date_range[1]

        categories = {'helicopter': 0, 'cargo': 0, 'commercial': 0, 'military': 0, 'private': unique_aircraft}

    # Rare types from type_summary (INSTANT - small table query)
    cursor.execute('''
        SELECT aircraft_type, unique_aircraft as aircraft_count, sample_icao
        FROM type_summary
        ORDER BY unique_aircraft ASC, aircraft_type
        LIMIT 10
    ''')
    rare_type_rows = cursor.fetchall()

    rare_types = []
    for r in rare_type_rows:
        ac_type = r['aircraft_type']
        count = r['aircraft_count']
        sample_icao = r['sample_icao']

        if count == 1:
            rarity = 'ultra_rare'
        elif count <= 2:
            rarity = 'very_rare'
        elif count <= 5:
            rarity = 'rare'
        else:
            rarity = 'uncommon'

        rare_types.append({
            'type': ac_type,
            'count': count,
            'rarity': rarity,
            'sample_icaos': [sample_icao] if sample_icao else []
        })

    # Most common types from type_summary (INSTANT)
    cursor.execute('''
        SELECT aircraft_type, unique_aircraft as aircraft_count
        FROM type_summary
        ORDER BY unique_aircraft DESC
        LIMIT 10
    ''')
    common_types = [{'type': r['aircraft_type'], 'count': r['aircraft_count']} for r in cursor.fetchall()]

    conn.close()

    return {
        'total_positions': total_positions,
        'unique_aircraft': unique_aircraft,
        'unique_types': unique_types,
        'unique_flights': unique_flights,
        'days_active': days_active,
        'first_log': first_log,
        'last_log': last_log,
        'categories': categories,
        'rare_types': rare_types,
        'common_types': common_types
    }


@app.route('/api/stats/overview')
def api_stats_overview():
    """Return cached stats overview."""
    if overview_cache['data'] is None:
        return jsonify({'error': 'Stats not yet calculated, please wait...'}), 503

    response = overview_cache['data'].copy()
    response['cached'] = True
    response['cache_age'] = int(time.time() - overview_cache['last_updated']) if overview_cache['last_updated'] else None
    return jsonify(response)


def calculate_records():
    """Calculate personal records - runs in background thread.
    Uses stats_summary for main records (INSTANT)."""
    conn = get_db()
    cursor = conn.cursor()

    records = {}

    # Try to get main records from stats_summary (INSTANT - single row lookup)
    cursor.execute('SELECT * FROM stats_summary WHERE id = 1')
    stats = cursor.fetchone()

    if stats and stats['total_positions'] > 0:
        # Best signal from summary
        if stats['best_rssi']:
            records['best_signal'] = {
                'icao': stats['best_rssi_icao'],
                'callsign': stats['best_rssi_callsign'],
                'rssi': stats['best_rssi'],
                'timestamp': stats['best_rssi_timestamp'],
                'type': stats['best_rssi_type']
            }

        # Highest altitude from summary
        if stats['max_altitude']:
            records['highest_altitude'] = {
                'icao': stats['max_altitude_icao'],
                'callsign': stats['max_altitude_callsign'],
                'altitude': stats['max_altitude'],
                'timestamp': stats['max_altitude_timestamp'],
                'type': stats['max_altitude_type']
            }

        # Fastest from summary
        if stats['max_speed']:
            records['fastest'] = {
                'icao': stats['max_speed_icao'],
                'callsign': stats['max_speed_callsign'],
                'speed': stats['max_speed'],
                'timestamp': stats['max_speed_timestamp'],
                'type': stats['max_speed_type']
            }

        # Busiest day from summary
        if stats['busiest_day']:
            records['busiest_day'] = {
                'date': stats['busiest_day'],
                'count': stats['busiest_day_count']
            }
    else:
        # Fallback for records not in summary (initial state)
        cursor.execute('''
            SELECT icao, callsign, rssi, timestamp, aircraft_type
            FROM positions WHERE rssi IS NOT NULL
            ORDER BY rssi DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        if row:
            records['best_signal'] = {
                'icao': row['icao'], 'callsign': row['callsign'],
                'rssi': row['rssi'], 'timestamp': row['timestamp'], 'type': row['aircraft_type']
            }

        cursor.execute('''
            SELECT icao, callsign, altitude, timestamp, aircraft_type
            FROM positions WHERE altitude IS NOT NULL
            ORDER BY altitude DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        if row:
            records['highest_altitude'] = {
                'icao': row['icao'], 'callsign': row['callsign'],
                'altitude': row['altitude'], 'timestamp': row['timestamp'], 'type': row['aircraft_type']
            }

        cursor.execute('''
            SELECT icao, callsign, speed, timestamp, aircraft_type
            FROM positions WHERE speed IS NOT NULL
            ORDER BY speed DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        if row:
            records['fastest'] = {
                'icao': row['icao'], 'callsign': row['callsign'],
                'speed': row['speed'], 'timestamp': row['timestamp'], 'type': row['aircraft_type']
            }

        cursor.execute('''
            SELECT date(timestamp) as day, COUNT(DISTINCT icao) as count
            FROM positions GROUP BY day ORDER BY count DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        if row:
            records['busiest_day'] = {'date': row['day'], 'count': row['count']}

    # Use pre-computed values from stats_summary for remaining records (FAST)
    if stats and stats['lowest_altitude']:
        records['lowest_altitude'] = {
            'icao': stats['lowest_altitude_icao'],
            'callsign': stats['lowest_altitude_callsign'],
            'altitude': stats['lowest_altitude'],
            'timestamp': stats['lowest_altitude_timestamp'],
            'type': stats['lowest_altitude_type']
        }

    if stats and stats['slowest_speed']:
        records['slowest'] = {
            'icao': stats['slowest_speed_icao'],
            'callsign': stats['slowest_speed_callsign'],
            'speed': stats['slowest_speed'],
            'timestamp': stats['slowest_speed_timestamp'],
            'type': stats['slowest_speed_type']
        }

    if stats and stats['earliest_catch_time']:
        records['earliest_catch'] = {
            'icao': stats['earliest_catch_icao'],
            'callsign': stats['earliest_catch_callsign'],
            'timestamp': stats['earliest_catch_timestamp'],
            'time': stats['earliest_catch_time'],
            'type': stats['earliest_catch_type']
        }

    if stats and stats['latest_catch_time']:
        records['latest_catch'] = {
            'icao': stats['latest_catch_icao'],
            'callsign': stats['latest_catch_callsign'],
            'timestamp': stats['latest_catch_timestamp'],
            'time': stats['latest_catch_time'],
            'type': stats['latest_catch_type']
        }

    conn.close()
    return records


@app.route('/api/stats/records')
def api_stats_records():
    """Return cached personal records."""
    if records_cache['data'] is None:
        return jsonify({'error': 'Records not yet calculated, please wait...'}), 503

    response = records_cache['data'].copy()
    response['cached'] = True
    response['cache_age'] = int(time.time() - records_cache['last_updated']) if records_cache['last_updated'] else None
    return jsonify(response)


# NOTE: calculate_heatmap() removed in v1.3.0 - was taking ~130s per calculation
# Activity Heatmap feature removed from frontend to improve performance


@app.route('/api/stats/heatmap')
def api_stats_heatmap():
    """Heatmap removed in v1.3.0 - returns 410 Gone."""
    return jsonify({'error': 'Heatmap feature removed in v1.3.0'}), 410


@app.route('/api/stats/timeline')
def api_stats_timeline():
    """
    Return traffic timeline data for charts.
    Shows aircraft activity over time with multiple granularity options.
    Query params:
        - range: 'day', 'week', 'month' (default: week)
        - date: specific date for 'day' range (YYYY-MM-DD)
    """
    conn = get_db()
    cursor = conn.cursor()

    range_type = request.args.get('range', 'week')
    date_param = request.args.get('date')

    timeline_data = []
    comparison_data = []

    if range_type == 'day':
        # Hourly breakdown for a single day
        if date_param:
            target_date = date_param
        else:
            cursor.execute('SELECT date(MAX(timestamp)) as latest FROM positions')
            target_date = cursor.fetchone()['latest'] or datetime.now().strftime('%Y-%m-%d')

        # Get hourly data for the target day
        cursor.execute('''
            SELECT
                strftime('%H', timestamp) as hour,
                COUNT(DISTINCT icao) as aircraft_count,
                COUNT(*) as position_count
            FROM positions
            WHERE date(timestamp) = ?
            GROUP BY hour
            ORDER BY hour
        ''', (target_date,))

        for row in cursor.fetchall():
            timeline_data.append({
                'label': f"{int(row['hour']):02d}:00",
                'hour': int(row['hour']),
                'aircraft': row['aircraft_count'],
                'positions': row['position_count']
            })

        # Fill in missing hours with zeros
        existing_hours = {d['hour'] for d in timeline_data}
        for hour in range(24):
            if hour not in existing_hours:
                timeline_data.append({
                    'label': f"{hour:02d}:00",
                    'hour': hour,
                    'aircraft': 0,
                    'positions': 0
                })
        timeline_data.sort(key=lambda x: x['hour'])

        # Get previous day for comparison
        cursor.execute('''
            SELECT
                strftime('%H', timestamp) as hour,
                COUNT(DISTINCT icao) as aircraft_count
            FROM positions
            WHERE date(timestamp) = date(?, '-1 day')
            GROUP BY hour
            ORDER BY hour
        ''', (target_date,))
        prev_day_data = {int(r['hour']): r['aircraft_count'] for r in cursor.fetchall()}
        comparison_data = [prev_day_data.get(h, 0) for h in range(24)]

        meta = {'date': target_date, 'granularity': 'hourly'}

    elif range_type == 'week':
        # Last 7 days by day
        cursor.execute('''
            SELECT
                date(timestamp) as day,
                strftime('%w', timestamp) as dow,
                COUNT(DISTINCT icao) as aircraft_count,
                COUNT(*) as position_count
            FROM positions
            WHERE timestamp >= datetime('now', '-7 days')
            GROUP BY day
            ORDER BY day
        ''')

        day_names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
        for row in cursor.fetchall():
            dow = int(row['dow'])
            timeline_data.append({
                'label': f"{day_names[dow]} {row['day'][5:]}",
                'date': row['day'],
                'day_of_week': dow,
                'aircraft': row['aircraft_count'],
                'positions': row['position_count']
            })

        # Get previous week for comparison
        cursor.execute('''
            SELECT
                date(timestamp) as day,
                COUNT(DISTINCT icao) as aircraft_count
            FROM positions
            WHERE timestamp >= datetime('now', '-14 days')
              AND timestamp < datetime('now', '-7 days')
            GROUP BY day
            ORDER BY day
        ''')
        comparison_data = [r['aircraft_count'] for r in cursor.fetchall()]

        meta = {'granularity': 'daily', 'days': 7}

    else:  # month
        # Last 30 days by day
        cursor.execute('''
            SELECT
                date(timestamp) as day,
                COUNT(DISTINCT icao) as aircraft_count,
                COUNT(*) as position_count
            FROM positions
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
            ORDER BY day
        ''')

        for row in cursor.fetchall():
            timeline_data.append({
                'label': row['day'][5:],  # MM-DD
                'date': row['day'],
                'aircraft': row['aircraft_count'],
                'positions': row['position_count']
            })

        # Get previous month for comparison
        cursor.execute('''
            SELECT
                date(timestamp) as day,
                COUNT(DISTINCT icao) as aircraft_count
            FROM positions
            WHERE timestamp >= datetime('now', '-60 days')
              AND timestamp < datetime('now', '-30 days')
            GROUP BY day
            ORDER BY day
        ''')
        comparison_data = [r['aircraft_count'] for r in cursor.fetchall()]

        meta = {'granularity': 'daily', 'days': 30}

    # Calculate summary stats
    if timeline_data:
        aircraft_values = [d['aircraft'] for d in timeline_data]
        total_aircraft = sum(aircraft_values)
        avg_aircraft = round(sum(aircraft_values) / len(aircraft_values), 1)
        peak_aircraft = max(aircraft_values)
        peak_idx = aircraft_values.index(peak_aircraft)
        peak_label = timeline_data[peak_idx]['label']
    else:
        total_aircraft = avg_aircraft = peak_aircraft = 0
        peak_label = 'N/A'

    conn.close()

    return jsonify({
        'range': range_type,
        'meta': meta,
        'timeline': timeline_data,
        'comparison': comparison_data,
        'summary': {
            'total': total_aircraft,
            'average': avg_aircraft,
            'peak': peak_aircraft,
            'peak_label': peak_label
        }
    })


@app.route('/api/logger/calendar')
def api_logger_calendar():
    """
    Return logging calendar data showing activity per day.
    Includes positions count, active hours, and gap detection.
    Query params:
        - month: YYYY-MM format (default: current month)
        - range: 'month' or 'year' (default: month)
    """
    from datetime import datetime, timedelta

    # Use cache for default request (current month, no params)
    range_type = request.args.get('range', 'month')
    month_param = request.args.get('month')

    if not month_param and range_type == 'month':
        # Default request - use cached data
        if calendar_cache['data'] is None:
            return jsonify({'error': 'Building cache, please wait...'}), 503

        response = calendar_cache['data'].copy()
        response['cached'] = True
        response['cache_age'] = int(time.time() - calendar_cache['last_updated']) if calendar_cache['last_updated'] else None
        return jsonify(response)

    # Non-default request (specific month or year range) - query directly
    conn = get_db()
    cursor = conn.cursor()

    if month_param:
        try:
            year, month = map(int, month_param.split('-'))
        except:
            year, month = datetime.now().year, datetime.now().month
    else:
        year, month = datetime.now().year, datetime.now().month

    if range_type == 'year':
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31"
    else:
        # Get month range
        start_date = f"{year}-{month:02d}-01"
        if month == 12:
            end_date = f"{year + 1}-01-01"
        else:
            end_date = f"{year}-{month + 1:02d}-01"

    # Get daily stats: positions, unique aircraft, active hours
    cursor.execute('''
        SELECT
            date(timestamp) as date,
            COUNT(*) as positions,
            COUNT(DISTINCT icao) as unique_aircraft,
            COUNT(DISTINCT strftime('%H', timestamp)) as active_hours,
            MIN(timestamp) as first_log,
            MAX(timestamp) as last_log
        FROM positions
        WHERE date(timestamp) >= ? AND date(timestamp) < ?
        GROUP BY date(timestamp)
        ORDER BY date
    ''', (start_date, end_date))

    daily_rows = cursor.fetchall()

    # Get ALL hourly data in ONE query (fixes N+1 problem!)
    cursor.execute('''
        SELECT
            date(timestamp) as date,
            strftime('%H', timestamp) as hour,
            COUNT(*) as count
        FROM positions
        WHERE date(timestamp) >= ? AND date(timestamp) < ?
        GROUP BY date(timestamp), strftime('%H', timestamp)
    ''', (start_date, end_date))

    # Build hourly lookup: {date: {hour: count}}
    hourly_by_date = {}
    for r in cursor.fetchall():
        date_str = r['date']
        if date_str not in hourly_by_date:
            hourly_by_date[date_str] = {}
        hourly_by_date[date_str][int(r['hour'])] = r['count']

    # Build calendar data with gap detection
    calendar_days = []
    for row in daily_rows:
        date_str = row['date']
        positions = row['positions']
        unique_aircraft = row['unique_aircraft']
        active_hours = row['active_hours']

        # Determine status based on activity
        # Full day = 18+ hours active, Partial = 6-17, Low = <6
        if active_hours >= 18:
            status = 'full'  # Green
        elif active_hours >= 6:
            status = 'partial'  # Yellow
        elif active_hours > 0:
            status = 'low'  # Orange
        else:
            status = 'offline'  # Red

        # Use pre-fetched hourly data (no more N+1 queries!)
        hourly_data = hourly_by_date.get(date_str, {})

        # Find gaps during peak hours (6am-10pm)
        gaps = []
        gap_start = None
        for hour in range(6, 22):  # 6am to 10pm
            if hour not in hourly_data or hourly_data[hour] == 0:
                if gap_start is None:
                    gap_start = hour
            else:
                if gap_start is not None:
                    gap_end = hour
                    gap_duration = gap_end - gap_start
                    if gap_duration >= 2:  # Only report gaps >= 2 hours
                        gaps.append({
                            'start': f"{gap_start:02d}:00",
                            'end': f"{gap_end:02d}:00",
                            'duration_hours': gap_duration
                        })
                    gap_start = None
        # Check if gap extends to end of peak hours
        if gap_start is not None:
            gap_duration = 22 - gap_start
            if gap_duration >= 2:
                gaps.append({
                    'start': f"{gap_start:02d}:00",
                    'end': "22:00",
                    'duration_hours': gap_duration
                })

        calendar_days.append({
            'date': date_str,
            'positions': positions,
            'unique_aircraft': unique_aircraft,
            'active_hours': active_hours,
            'status': status,
            'gaps': gaps
        })

    # Calculate streaks
    all_dates = [d['date'] for d in calendar_days]
    current_streak = 0
    longest_streak = 0
    streak_count = 0

    if all_dates:
        # Check for current streak (consecutive days ending today or yesterday)
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        today_str = today.strftime('%Y-%m-%d')
        yesterday_str = yesterday.strftime('%Y-%m-%d')

        if today_str in all_dates or yesterday_str in all_dates:
            # Count back from most recent
            check_date = today if today_str in all_dates else yesterday
            while check_date.strftime('%Y-%m-%d') in all_dates:
                current_streak += 1
                check_date -= timedelta(days=1)

        # Calculate longest streak
        sorted_dates = sorted(all_dates)
        streak_count = 1
        for i in range(1, len(sorted_dates)):
            prev_date = datetime.strptime(sorted_dates[i-1], '%Y-%m-%d').date()
            curr_date = datetime.strptime(sorted_dates[i], '%Y-%m-%d').date()
            if (curr_date - prev_date).days == 1:
                streak_count += 1
            else:
                longest_streak = max(longest_streak, streak_count)
                streak_count = 1
        longest_streak = max(longest_streak, streak_count)

    # Summary stats
    total_days_in_range = len(calendar_days)
    total_positions = sum(d['positions'] for d in calendar_days)
    full_days = sum(1 for d in calendar_days if d['status'] == 'full')
    partial_days = sum(1 for d in calendar_days if d['status'] == 'partial')
    low_days = sum(1 for d in calendar_days if d['status'] == 'low')
    days_with_gaps = sum(1 for d in calendar_days if d['gaps'])

    conn.close()

    return jsonify({
        'year': year,
        'month': month if range_type == 'month' else None,
        'range_type': range_type,
        'days': calendar_days,
        'summary': {
            'total_days': total_days_in_range,
            'full_days': full_days,
            'partial_days': partial_days,
            'low_days': low_days,
            'days_with_gaps': days_with_gaps,
            'total_positions': total_positions,
            'current_streak': current_streak,
            'longest_streak': longest_streak
        }
    })


@app.route('/api/logger/calendar/<date>')
def api_logger_calendar_day(date):
    """Get detailed hourly breakdown for a specific day."""
    conn = get_db()
    cursor = conn.cursor()

    # Validate date format
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    # Get hourly breakdown
    cursor.execute('''
        SELECT
            strftime('%H', timestamp) as hour,
            COUNT(*) as positions,
            COUNT(DISTINCT icao) as unique_aircraft,
            COUNT(DISTINCT callsign) as unique_flights
        FROM positions
        WHERE date(timestamp) = ?
        GROUP BY hour
        ORDER BY hour
    ''', (date,))

    hourly_data = []
    for row in cursor.fetchall():
        hourly_data.append({
            'hour': int(row['hour']),
            'positions': row['positions'],
            'unique_aircraft': row['unique_aircraft'],
            'unique_flights': row['unique_flights']
        })

    # Get top aircraft types for this day
    cursor.execute('''
        SELECT aircraft_type, COUNT(DISTINCT icao) as count
        FROM positions
        WHERE date(timestamp) = ? AND aircraft_type IS NOT NULL AND aircraft_type != ''
        GROUP BY aircraft_type
        ORDER BY count DESC
        LIMIT 10
    ''', (date,))
    top_types = [{'type': r['aircraft_type'], 'count': r['count']} for r in cursor.fetchall()]

    # Get notable catches (rare types, military, etc.)
    cursor.execute('''
        SELECT DISTINCT icao, callsign, aircraft_type, category
        FROM positions
        WHERE date(timestamp) = ?
            AND (category = 'A7'
                OR icao LIKE 'AE%' OR icao LIKE 'AF%'
                OR callsign LIKE 'RCH%' OR callsign LIKE 'EVAC%')
        LIMIT 20
    ''', (date,))
    notable = [dict(r) for r in cursor.fetchall()]

    # Summary for the day
    cursor.execute('''
        SELECT
            COUNT(*) as positions,
            COUNT(DISTINCT icao) as unique_aircraft,
            COUNT(DISTINCT callsign) as unique_flights,
            MIN(timestamp) as first_log,
            MAX(timestamp) as last_log
        FROM positions
        WHERE date(timestamp) = ?
    ''', (date,))
    summary = cursor.fetchone()

    conn.close()

    return jsonify({
        'date': date,
        'summary': {
            'positions': summary['positions'] or 0,
            'unique_aircraft': summary['unique_aircraft'] or 0,
            'unique_flights': summary['unique_flights'] or 0,
            'first_log': summary['first_log'],
            'last_log': summary['last_log']
        },
        'hourly': hourly_data,
        'top_types': top_types,
        'notable_catches': notable
    })


@app.route('/api/system')
def api_system_stats():
    """Get Raspberry Pi system stats - temp, CPU, memory, disk, uptime."""
    stats = {}
    
    # CPU Temperature
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp_raw = int(f.read().strip())
            stats['cpu_temp'] = round(temp_raw / 1000, 1)  # Convert millidegrees to degrees
    except:
        stats['cpu_temp'] = None
    
    # CPU Usage - use load average as percentage (more reliable in Docker)
    # Load avg / num_cpus * 100 gives rough CPU usage
    try:
        # Get number of CPU cores
        num_cpus = 1
        with open('/proc/cpuinfo', 'r') as f:
            num_cpus = max(1, f.read().count('processor'))
        
        # Get load average (1 minute)
        with open('/proc/loadavg', 'r') as f:
            load_1 = float(f.read().split()[0])
            # Convert to percentage (capped at 100%)
            cpu_pct = min(100, round((load_1 / num_cpus) * 100, 1))
            stats['cpu_usage'] = cpu_pct
            stats['num_cpus'] = num_cpus
    except:
        stats['cpu_usage'] = None
        stats['num_cpus'] = 1
    
    # Memory Usage
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = {}
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = int(parts[1].strip().split()[0])  # Value in KB
                    meminfo[key] = val
            
            total = meminfo.get('MemTotal', 0)
            available = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            used = total - available
            
            stats['mem_total'] = round(total / 1024, 0)  # MB
            stats['mem_used'] = round(used / 1024, 0)  # MB
            stats['mem_percent'] = round((used / total) * 100, 1) if total > 0 else 0
    except:
        stats['mem_total'] = None
        stats['mem_used'] = None
        stats['mem_percent'] = None
    
    # Disk Usage
    try:
        result = subprocess.run(['df', '-B1', '/'], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split('\n')
        if len(lines) >= 2:
            parts = lines[1].split()
            total = int(parts[1])
            used = int(parts[2])
            stats['disk_total'] = round(total / (1024**3), 1)  # GB
            stats['disk_used'] = round(used / (1024**3), 1)  # GB
            stats['disk_percent'] = round((used / total) * 100, 1) if total > 0 else 0
    except:
        stats['disk_total'] = None
        stats['disk_used'] = None
        stats['disk_percent'] = None
    
    # System Uptime
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            stats['uptime_seconds'] = int(uptime_seconds)
            stats['uptime_formatted'] = f"{days}d {hours}h {minutes}m"
    except:
        stats['uptime_seconds'] = None
        stats['uptime_formatted'] = None
    
    # Load Average
    try:
        with open('/proc/loadavg', 'r') as f:
            loads = f.read().split()[:3]
            stats['load_1'] = float(loads[0])
            stats['load_5'] = float(loads[1])
            stats['load_15'] = float(loads[2])
            
            # Store in history for sparkline (with timestamp)
            load_history.append({
                'time': datetime.now().strftime('%H:%M:%S'),
                'load': stats['load_1'],
                'cpu': stats.get('cpu_usage', 0)
            })
    except:
        stats['load_1'] = None
        stats['load_5'] = None
        stats['load_15'] = None
    
    # Include load history for sparkline (last 30 readings)
    stats['load_history'] = list(load_history)
    
    # Database size
    try:
        if os.path.exists(DB_PATH):
            db_size = os.path.getsize(DB_PATH)
            stats['db_size'] = round(db_size / (1024**2), 2)  # MB
        else:
            stats['db_size'] = 0
    except:
        stats['db_size'] = None
    
    # Storage warning if disk is getting full
    if stats.get('disk_percent') and stats['disk_percent'] > 85:
        stats['storage_warning'] = True
        stats['storage_warning_msg'] = f"Disk {stats['disk_percent']}% full. Consider increasing retention cleanup."
    else:
        stats['storage_warning'] = False
    
    return jsonify(stats)


@app.route('/api/system/hardware')
def api_system_hardware():
    """Detect hardware (Raspberry Pi model, CPU, RAM) and provide recommendations."""
    hardware = {
        'model': None,
        'model_name': None,
        'cpu_model': None,
        'cpu_cores': 1,
        'ram_mb': 0,
        'is_raspberry_pi': False,
        'recommendations': []
    }

    # Detect Raspberry Pi model from /proc/cpuinfo
    try:
        with open('/proc/cpuinfo', 'r') as f:
            cpuinfo = f.read()

            # Count cores
            hardware['cpu_cores'] = max(1, cpuinfo.count('processor'))

            # Get CPU model name and detect Pi
            for line in cpuinfo.split('\n'):
                if line.startswith('model name'):
                    hardware['cpu_model'] = line.split(':')[1].strip()
                elif line.startswith('Model'):
                    # Raspberry Pi specific - check Model field
                    hardware['model'] = line.split(':')[1].strip()
                    # Detect Pi from Model field (e.g., "Raspberry Pi 3 Model B Rev 1.2")
                    if 'raspberry pi' in hardware['model'].lower():
                        hardware['is_raspberry_pi'] = True
                elif line.startswith('Hardware'):
                    hw = line.split(':')[1].strip()
                    # Also detect Pi from BCM hardware
                    if 'BCM' in hw:
                        hardware['is_raspberry_pi'] = True

            # Determine Pi model name from Model field
            if hardware['model']:
                model = hardware['model'].lower()
                if 'pi 5' in model:
                    hardware['model_name'] = 'Raspberry Pi 5'
                elif 'pi 4' in model:
                    hardware['model_name'] = 'Raspberry Pi 4'
                elif 'pi 3' in model:
                    hardware['model_name'] = 'Raspberry Pi 3'
                elif 'pi 2' in model:
                    hardware['model_name'] = 'Raspberry Pi 2'
                elif 'pi zero 2' in model:
                    hardware['model_name'] = 'Raspberry Pi Zero 2 W'
                elif 'pi zero' in model:
                    hardware['model_name'] = 'Raspberry Pi Zero'
                elif hardware['is_raspberry_pi']:
                    hardware['model_name'] = hardware['model']
    except:
        pass

    # Get RAM from /proc/meminfo
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemTotal'):
                    mem_kb = int(line.split(':')[1].strip().split()[0])
                    hardware['ram_mb'] = round(mem_kb / 1024)
                    break
    except:
        pass

    # Generate recommendations based on hardware
    ram_mb = hardware['ram_mb']
    cores = hardware['cpu_cores']
    is_pi = hardware['is_raspberry_pi']
    model_name = hardware['model_name'] or ''

    # Initialize poll interval recommendations (will be customized based on hardware)
    hardware['poll_intervals'] = {
        'recommended': 30,
        'min_safe': 15,
        'warning_threshold': 10,
        'intervals': {
            5: {'status': 'danger', 'label': '❌ Too aggressive', 'desc': 'May cause high CPU load'},
            10: {'status': 'warning', 'label': '⚠️ High load', 'desc': 'Monitor system resources'},
            15: {'status': 'success', 'label': '✅ Works well', 'desc': 'Good balance of freshness and performance'},
            30: {'status': 'recommended', 'label': '⭐ Recommended', 'desc': 'Optimal for most setups'},
            60: {'status': 'conservative', 'label': '💚 Conservative', 'desc': 'Minimal resource usage'}
        }
    }
    hardware['version_recommendation'] = 'v1.3.0 ✅'

    # Determine hardware tier and set recommendations
    if is_pi:
        if 'Zero' in model_name and 'Zero 2' not in model_name:
            # Pi Zero (original) - most conservative
            hardware['poll_intervals']['recommended'] = 60
            hardware['poll_intervals']['min_safe'] = 30
            hardware['poll_intervals']['warning_threshold'] = 15
            hardware['version_recommendation'] = 'v1.3.0 ⚠️'
            hardware['recommendations'].append({
                'type': 'warning',
                'title': 'Pi Zero Detected',
                'message': 'Limited single-core performance. Use 60s polling and 7-14 day retention.'
            })
        elif 'Pi 3' in model_name or 'Zero 2' in model_name:
            # Pi 3 / Zero 2 W - performs great with v1.3.0 caching!
            hardware['poll_intervals']['recommended'] = 30
            hardware['poll_intervals']['min_safe'] = 15
            hardware['poll_intervals']['warning_threshold'] = 10
            hardware['poll_intervals']['intervals'][15] = {'status': 'success', 'label': '✅ Works well', 'desc': 'Tested and stable on Pi 3B+'}
            hardware['poll_intervals']['intervals'][30] = {'status': 'recommended', 'label': '⭐ Recommended', 'desc': 'Best balance for Pi 3'}
            hardware['version_recommendation'] = 'v1.3.0 ✅'
            hardware['recommendations'].append({
                'type': 'success',
                'title': f'{model_name} + v1.3.0 Caching',
                'message': 'Great performance! Caching reduced CPU from 100% to ~32%. 30s recommended, 15s works fine.'
            })
        elif 'Pi 4' in model_name:
            # Pi 4 - check RAM for 8GB variant
            if ram_mb >= 7000:  # 8GB model
                hardware['poll_intervals']['recommended'] = 10
                hardware['poll_intervals']['min_safe'] = 5
                hardware['poll_intervals']['warning_threshold'] = 5
                hardware['poll_intervals']['intervals'][5] = {'status': 'success', 'label': '✅ Supported', 'desc': 'Pi 4 8GB handles 5s easily'}
                hardware['poll_intervals']['intervals'][10] = {'status': 'recommended', 'label': '⭐ Recommended', 'desc': 'Optimal for Pi 4 8GB'}
                hardware['version_recommendation'] = 'v1.3.0 🚀'
                hardware['recommendations'].append({
                    'type': 'success',
                    'title': 'Pi 4 8GB - Maximum Performance',
                    'message': 'Top-tier hardware! All features run smoothly. 10s polling recommended, 5s supported.'
                })
            else:  # 2GB or 4GB model
                hardware['poll_intervals']['recommended'] = 15
                hardware['poll_intervals']['min_safe'] = 10
                hardware['poll_intervals']['warning_threshold'] = 5
                hardware['poll_intervals']['intervals'][10] = {'status': 'success', 'label': '✅ Works great', 'desc': 'Pi 4 handles 10s well'}
                hardware['poll_intervals']['intervals'][15] = {'status': 'recommended', 'label': '⭐ Recommended', 'desc': 'Optimal for Pi 4'}
                hardware['version_recommendation'] = 'v1.3.0 ⭐'
                hardware['recommendations'].append({
                    'type': 'success',
                    'title': f'Pi 4 ({ram_mb}MB) - Optimal',
                    'message': 'Excellent performance for v1.3.0. 15s recommended, 10s works great.'
                })
        elif 'Pi 5' in model_name:
            # Pi 5 - maximum performance
            hardware['poll_intervals']['recommended'] = 10
            hardware['poll_intervals']['min_safe'] = 5
            hardware['poll_intervals']['warning_threshold'] = 5
            hardware['poll_intervals']['intervals'][5] = {'status': 'success', 'label': '✅ Supported', 'desc': 'Pi 5 handles 5s easily'}
            hardware['poll_intervals']['intervals'][10] = {'status': 'recommended', 'label': '⭐ Recommended', 'desc': 'Optimal for Pi 5'}
            hardware['version_recommendation'] = 'v1.3.0 🚀'
            hardware['recommendations'].append({
                'type': 'success',
                'title': 'Pi 5 - Maximum Performance',
                'message': 'Flagship hardware! All features run at full speed. 10s recommended, 5s supported.'
            })
        else:
            # Unknown Pi model - use moderate defaults
            hardware['recommendations'].append({
                'type': 'info',
                'title': 'Raspberry Pi Detected',
                'message': 'v1.3.0 caching provides excellent performance. Try 30s interval to start.'
            })
    else:
        # Non-Pi hardware - base on RAM/cores
        if ram_mb >= 4000 and cores >= 4:
            hardware['poll_intervals']['recommended'] = 10
            hardware['poll_intervals']['min_safe'] = 5
            hardware['version_recommendation'] = 'v1.3.0 🚀'
            hardware['recommendations'].append({
                'type': 'success',
                'title': 'Powerful Hardware',
                'message': f'{cores} cores, {ram_mb}MB RAM - all features run at full speed.'
            })
        elif ram_mb >= 2000:
            hardware['poll_intervals']['recommended'] = 15
            hardware['poll_intervals']['min_safe'] = 10
            hardware['version_recommendation'] = 'v1.3.0 ⭐'
            hardware['recommendations'].append({
                'type': 'success',
                'title': 'Good Hardware',
                'message': 'Solid performance expected. 15s polling recommended.'
            })

    # RAM-based retention recommendation
    if ram_mb < 1024:
        hardware['recommendations'].append({
            'type': 'info',
            'title': 'RAM Optimization',
            'message': 'With <1GB RAM, 14-day retention keeps database compact. Caching helps significantly!'
        })
    elif ram_mb < 2048:
        hardware['recommendations'].append({
            'type': 'success',
            'title': 'RAM: Good for Logging',
            'message': '1-2GB RAM handles 30-day retention well with v1.3.0 caching.'
        })
    else:
        hardware['recommendations'].append({
            'type': 'success',
            'title': 'RAM: Excellent',
            'message': f'{ram_mb}MB RAM supports 60+ day retention easily.'
        })

    # SD card info (informational, not alarming)
    if is_pi:
        hardware['recommendations'].append({
            'type': 'info',
            'title': 'Storage Info',
            'message': 'SQLite WAL mode reduces SD card writes. SSD via USB recommended for heavy use.'
        })

    # Add educational content for modal display
    hardware['settings_guide'] = {
        'poll_interval': {
            'name': 'Poll Interval',
            'description': 'How often the logger fetches new aircraft data from your receiver.',
            'impact': 'Lower = more frequent updates, higher CPU/database load. Higher = less resource usage but less real-time data.',
            'values': {
                5: 'Aggressive - Maximum data capture, highest CPU usage. Best for powerful hardware.',
                10: 'Fast - Great balance for Pi 4/5 and modern hardware.',
                15: 'Balanced - Good for Pi 3B+ and most systems.',
                30: 'Conservative - Default for most setups. Low resource usage.',
                60: 'Minimal - Very low CPU usage. Good for weak hardware or background monitoring.'
            }
        },
        'retention_days': {
            'name': 'Data Retention',
            'description': 'How many days of historical data to keep before automatic cleanup.',
            'impact': 'Longer retention = larger database, more storage. Shorter = smaller database, faster queries.',
            'recommendations': {
                'low_ram': '7-14 days for systems with <1GB RAM',
                'medium_ram': '14-30 days for 1-2GB RAM',
                'high_ram': '30-90 days for 4GB+ RAM'
            }
        },
        'database': {
            'name': 'Database Settings',
            'description': 'SQLite with WAL (Write-Ahead Logging) mode for better performance and reliability.',
            'impact': 'WAL mode reduces SD card wear and improves concurrent read/write performance.',
            'tips': [
                'Database is automatically optimized with indexes for fast queries',
                'Stats are cached and refreshed every 60 seconds to reduce CPU load',
                'Consider USB SSD for heavy use on Raspberry Pi'
            ]
        }
    }

    # Hardware tier explanation
    hardware['hardware_tiers'] = {
        'flagship': {
            'examples': ['Pi 5', 'Pi 4 8GB', 'Modern x86 servers'],
            'capabilities': 'All features at maximum speed, 5-10s polling, 90+ day retention',
            'recommended_interval': 10
        },
        'optimal': {
            'examples': ['Pi 4 2GB/4GB', 'Pi 3B+ with good cooling'],
            'capabilities': 'Full features with 15-30s polling, 30-60 day retention',
            'recommended_interval': 15
        },
        'moderate': {
            'examples': ['Pi 3', 'Pi Zero 2 W'],
            'capabilities': 'All features with background caching, 30s polling, 14-30 day retention',
            'recommended_interval': 30
        },
        'constrained': {
            'examples': ['Pi Zero (original)', 'Very low RAM systems'],
            'capabilities': 'Core logging works, 60s polling, 7-14 day retention',
            'recommended_interval': 60
        }
    }

    # Determine current tier
    if is_pi:
        if 'Pi 5' in model_name or (ram_mb >= 7000 and 'Pi 4' in model_name):
            hardware['current_tier'] = 'flagship'
        elif 'Pi 4' in model_name:
            hardware['current_tier'] = 'optimal'
        elif 'Pi 3' in model_name or 'Zero 2' in model_name:
            hardware['current_tier'] = 'moderate'
        else:
            hardware['current_tier'] = 'constrained'
    else:
        if ram_mb >= 4000 and cores >= 4:
            hardware['current_tier'] = 'flagship'
        elif ram_mb >= 2000:
            hardware['current_tier'] = 'optimal'
        else:
            hardware['current_tier'] = 'moderate'

    return jsonify(hardware)


def calculate_achievements():
    """Calculate all achievements - runs in background thread.
    Uses pre-aggregated summary tables for INSTANT queries."""
    conn = get_db()
    cursor = conn.cursor()

    achievements = []

    # Prestige ranks for achievements completed multiple times
    PRESTIGE_RANKS = [
        (1, ''),           # Base completion - no suffix
        (2, ' II'),        # 2x complete
        (3, ' III'),       # 3x
        (5, ' IV'),        # 5x
        (10, ' V'),        # 10x
        (25, ' ★'),        # 25x - star rank
        (50, ' ★★'),       # 50x
        (100, ' ★★★'),     # 100x - triple star
    ]

    def get_prestige_rank(times_complete):
        """Get prestige suffix based on times completed."""
        rank = ''
        for threshold, suffix in PRESTIGE_RANKS:
            if times_complete >= threshold:
                rank = suffix
        return rank

    # Helper to add achievement with prestige
    def add_achievement(id, name, icon, desc, unlocked, progress=None, target=None):
        times_complete = 0
        prestige = ''
        if target and target > 0 and progress:
            times_complete = progress // target
            prestige = get_prestige_rank(times_complete)

        achievements.append({
            'id': id,
            'name': name,
            'icon': icon,
            'desc': desc,
            'unlocked': unlocked,
            'progress': progress,
            'target': target,
            'times_complete': times_complete,
            'prestige': prestige
        })

    # ═══════════════════════════════════════════════════════════════════════════
    # Load all stats from summary tables (INSTANT - vs scanning 1.6M+ rows)
    # ═══════════════════════════════════════════════════════════════════════════
    cursor.execute('SELECT * FROM stats_summary WHERE id = 1')
    stats = cursor.fetchone()

    if stats and stats['total_positions'] > 0:
        # Use pre-computed values from summary tables
        aircraft_count = stats['unique_aircraft']
        position_count = stats['total_positions']
        days_active = stats['days_active']
        type_count = stats['unique_types']
        military_count = stats['military_count']
        heli_count = stats['helicopter_count']
        cargo_count = stats['cargo_count']
        night_count = stats['night_count']
        early_count = stats['early_count']
        delta_count = stats['delta_count']
        united_count = stats['united_count']
        american_count = stats['american_count']
        southwest_count = stats['southwest_count']
        jetblue_count = stats['jetblue_count']
        emergency_count = stats['emergency_count']
        hours_covered = bin(stats['hours_covered'] or 0).count('1')
        max_streak = stats['max_streak'] or 0
        intl_carriers_str = stats['intl_carriers_seen'] or ''
        intl_carriers = len([c for c in intl_carriers_str.split(',') if c])
        # Achievement category counts (pre-computed)
        widebody_count = stats['widebody_count'] or 0
        boeing_count = stats['boeing_count'] or 0
        airbus_count = stats['airbus_count'] or 0
        turboprop_count = stats['turboprop_count'] or 0
        giant_count = stats['giant_count'] or 0
        ems_heli_count = stats['ems_heli_count'] or 0
        coastguard_count = stats['coastguard_count'] or 0
    else:
        # Fallback to direct queries if summary not populated
        cursor.execute('SELECT COUNT(DISTINCT icao) as count FROM positions')
        aircraft_count = cursor.fetchone()['count']

        cursor.execute('SELECT COUNT(*) as count FROM positions')
        position_count = cursor.fetchone()['count']

        cursor.execute('SELECT COUNT(DISTINCT date(timestamp)) as days FROM positions')
        days_active = cursor.fetchone()['days']

        cursor.execute('SELECT COUNT(DISTINCT aircraft_type) as count FROM positions WHERE aircraft_type IS NOT NULL AND aircraft_type != ""')
        type_count = cursor.fetchone()['count']

        # Set defaults for category counts (will use old queries below)
        military_count = 0
        heli_count = 0
        cargo_count = 0
        night_count = 0
        early_count = 0
        delta_count = 0
        united_count = 0
        american_count = 0
        southwest_count = 0
        jetblue_count = 0
        emergency_count = 0
        hours_covered = 0
        max_streak = 0
        intl_carriers = 0
        # Achievement category defaults
        widebody_count = 0
        boeing_count = 0
        airbus_count = 0
        turboprop_count = 0
        giant_count = 0
        ems_heli_count = 0
        coastguard_count = 0

    # --- Aircraft Count Achievements (Progressive tiers - designed for 3-12 months progression) ---
    add_achievement('first_thousand', 'First Thousand', '✈️', 'Log 1,000 unique aircraft', aircraft_count >= 1000, aircraft_count, 1000)
    add_achievement('sky_watcher', 'Sky Watcher', '👀', 'Log 5,000 unique aircraft', aircraft_count >= 5000, aircraft_count, 5000)
    add_achievement('plane_spotter', 'Plane Spotter', '🔍', 'Log 15,000 unique aircraft', aircraft_count >= 15000, aircraft_count, 15000)
    add_achievement('ten_thousand', 'Thirty Thousand', '🎯', 'Log 30,000 unique aircraft', aircraft_count >= 30000, aircraft_count, 30000)
    add_achievement('twenty_five_k', 'Fifty Thousand', '📡', 'Log 50,000 unique aircraft', aircraft_count >= 50000, aircraft_count, 50000)
    add_achievement('fifty_k', 'Hundred Thousand', '🔭', 'Log 100,000 unique aircraft', aircraft_count >= 100000, aircraft_count, 100000)
    add_achievement('hundred_k', 'Quarter Million', '⭐', 'Log 250,000 unique aircraft', aircraft_count >= 250000, aircraft_count, 250000)
    add_achievement('two_fifty_k', 'Half Million', '🌟', 'Log 500,000 unique aircraft', aircraft_count >= 500000, aircraft_count, 500000)
    add_achievement('five_hundred_k', 'Millionaire', '👑', 'Log 1,000,000 unique aircraft', aircraft_count >= 1000000, aircraft_count, 1000000)
    add_achievement('million', 'Two Million', '💎', 'Log 2,000,000 unique aircraft', aircraft_count >= 2000000, aircraft_count, 2000000)
    add_achievement('two_million', 'Five Million', '💠', 'Log 5,000,000 unique aircraft', aircraft_count >= 5000000, aircraft_count, 5000000)
    add_achievement('five_million', 'Ten Million', '🏆', 'Log 10,000,000 unique aircraft', aircraft_count >= 10000000, aircraft_count, 10000000)

    # --- Position Count Achievements (Data volume - ~2.2M/month at busy hub) ---
    add_achievement('data_1m', 'Million Points', '🗃️', 'Log 5,000,000 positions', position_count >= 5000000, position_count, 5000000)
    add_achievement('data_5m', 'Data Enthusiast', '📁', 'Log 10,000,000 positions', position_count >= 10000000, position_count, 10000000)
    add_achievement('data_10m', 'Data Collector', '🗄️', 'Log 25,000,000 positions', position_count >= 25000000, position_count, 25000000)
    add_achievement('data_25m', 'Data Hoarder', '📦', 'Log 50,000,000 positions', position_count >= 50000000, position_count, 50000000)
    add_achievement('data_50m', 'Big Data', '🏔️', 'Log 100,000,000 positions', position_count >= 100000000, position_count, 100000000)
    add_achievement('data_100m', 'Massive Data', '🗻', 'Log 250,000,000 positions', position_count >= 250000000, position_count, 250000000)
    add_achievement('data_500m', 'Data Legend', '🌋', 'Log 500,000,000 positions', position_count >= 500000000, position_count, 500000000)
    add_achievement('data_1b', 'Billion Tracker', '🌍', 'Log 1,000,000,000 positions', position_count >= 1000000000, position_count, 1000000000)

    # --- Consistency Achievements (Time-based) ---
    add_achievement('first_day', 'First Day', '🌱', 'Log aircraft for 1 day', days_active >= 1, days_active, 1)
    add_achievement('three_days', 'Getting Started', '📆', 'Log aircraft for 3 days', days_active >= 3, days_active, 3)
    add_achievement('one_week', 'Week Tracker', '📅', 'Log aircraft for 7 days', days_active >= 7, days_active, 7)
    add_achievement('two_weeks', 'Two Week Tracker', '🗓️', 'Log aircraft for 14 days', days_active >= 14, days_active, 14)
    add_achievement('month_monitor', 'Monthly Monitor', '📆', 'Log aircraft for 30 days', days_active >= 30, days_active, 30)
    add_achievement('quarter_year', 'Quarter Year', '🌙', 'Log aircraft for 90 days', days_active >= 90, days_active, 90)
    add_achievement('half_year', 'Half Year Hero', '☀️', 'Log aircraft for 180 days', days_active >= 180, days_active, 180)
    add_achievement('full_year', 'Year of Spotting', '🏆', 'Log aircraft for 365 days', days_active >= 365, days_active, 365)

    # --- Type Achievements (Aircraft type diversity - ~250 types/month at hub) ---
    add_achievement('type_beginner', 'Type Beginner', '📝', 'Log 100 different aircraft types', type_count >= 100, type_count, 100)
    add_achievement('type_learner', 'Type Learner', '📖', 'Log 200 different aircraft types', type_count >= 200, type_count, 200)
    add_achievement('type_student', 'Type Student', '🎒', 'Log 300 different aircraft types', type_count >= 300, type_count, 300)
    add_achievement('type_spotter', 'Type Spotter', '📚', 'Log 400 different aircraft types', type_count >= 400, type_count, 400)
    add_achievement('type_enthusiast', 'Type Enthusiast', '🎓', 'Log 500 different aircraft types', type_count >= 500, type_count, 500)
    add_achievement('type_collector', 'Type Collector', '🏅', 'Log 750 different aircraft types', type_count >= 750, type_count, 750)
    add_achievement('type_expert', 'Type Expert', '👨‍🎓', 'Log 1,000 different aircraft types', type_count >= 1000, type_count, 1000)

    # --- Category Achievements (from pre-computed flags in aircraft_summary) ---
    # Military achievements (~190/month near base, varies heavily by location)
    add_achievement('military_first', 'First Military', '🎖️', 'Log 100 military aircraft', military_count >= 100, military_count, 100)
    add_achievement('military_fan', 'Military Fan', '🏵️', 'Log 500 military aircraft', military_count >= 500, military_count, 500)
    add_achievement('military_watcher', 'Military Watcher', '🔰', 'Log 1,000 military aircraft', military_count >= 1000, military_count, 1000)
    add_achievement('military_spotter', 'Military Spotter', '🪖', 'Log 2,500 military aircraft', military_count >= 2500, military_count, 2500)
    add_achievement('military_expert', 'Military Expert', '🎗️', 'Log 5,000 military aircraft', military_count >= 5000, military_count, 5000)
    add_achievement('military_master', 'Military Master', '🏛️', 'Log 10,000 military aircraft', military_count >= 10000, military_count, 10000)

    # Helicopter achievements (~30/month, varies by location)
    add_achievement('heli_first', 'First Heli', '🚁', 'Log 100 helicopters', heli_count >= 100, heli_count, 100)
    add_achievement('heli_fan', 'Heli Fan', '🌀', 'Log 250 helicopters', heli_count >= 250, heli_count, 250)
    add_achievement('heli_watcher', 'Heli Watcher', '🦅', 'Log 500 helicopters', heli_count >= 500, heli_count, 500)
    add_achievement('heli_hunter', 'Helicopter Hunter', '🔄', 'Log 1,000 helicopters', heli_count >= 1000, heli_count, 1000)
    add_achievement('heli_expert', 'Rotor Expert', '💫', 'Log 2,500 helicopters', heli_count >= 2500, heli_count, 2500)
    add_achievement('heli_master', 'Rotor Master', '🌟', 'Log 5,000 helicopters', heli_count >= 5000, heli_count, 5000)

    # Cargo achievements (~540/month near hub)
    add_achievement('cargo_first', 'First Cargo', '📦', 'Log 500 cargo aircraft', cargo_count >= 500, cargo_count, 500)
    add_achievement('cargo_watcher', 'Cargo Watcher', '📫', 'Log 1,000 cargo aircraft', cargo_count >= 1000, cargo_count, 1000)
    add_achievement('cargo_tracker', 'Package Tracker', '🚚', 'Log 2,500 cargo aircraft', cargo_count >= 2500, cargo_count, 2500)
    add_achievement('cargo_spotter', 'Cargo Spotter', '📬', 'Log 5,000 cargo aircraft', cargo_count >= 5000, cargo_count, 5000)
    add_achievement('cargo_expert', 'Logistics Expert', '📮', 'Log 10,000 cargo aircraft', cargo_count >= 10000, cargo_count, 10000)
    add_achievement('cargo_master', 'Logistics Master', '🏭', 'Log 25,000 cargo aircraft', cargo_count >= 25000, cargo_count, 25000)

    # --- Time of Day Achievements (~4000 night, ~700 early per month at hub) ---
    add_achievement('night_spotter', 'Night Spotter', '🌙', 'Log 5,000 aircraft between midnight and 5am', night_count >= 5000, night_count, 5000)
    add_achievement('night_watcher', 'Night Watcher', '🌃', 'Log 15,000 aircraft between midnight and 5am', night_count >= 15000, night_count, 15000)
    add_achievement('night_owl', 'Night Owl', '🦉', 'Log 50,000 aircraft between midnight and 5am', night_count >= 50000, night_count, 50000)
    add_achievement('night_master', 'Night Master', '🌌', 'Log 150,000 aircraft between midnight and 5am', night_count >= 150000, night_count, 150000)

    add_achievement('early_spotter', 'Early Spotter', '🌅', 'Log 2,500 aircraft before 7am', early_count >= 2500, early_count, 2500)
    add_achievement('early_riser', 'Early Riser', '☀️', 'Log 10,000 aircraft before 7am', early_count >= 10000, early_count, 10000)
    add_achievement('dawn_patrol', 'Dawn Patrol', '🌄', 'Log 50,000 aircraft before 7am', early_count >= 50000, early_count, 50000)
    add_achievement('early_master', 'Early Master', '🌞', 'Log 150,000 aircraft before 7am', early_count >= 150000, early_count, 150000)

    # --- Airline Achievements (from pre-computed counts) ---
    major_carriers_seen = sum([delta_count > 0, united_count > 0, american_count > 0, southwest_count > 0, jetblue_count > 0])
    has_full_house = major_carriers_seen >= 5
    add_achievement('full_house', 'Full House', '🃏', 'See all 5 major US carriers', has_full_house, major_carriers_seen, 5)

    # --- Widebody Achievements (from pre-computed count) ---
    add_achievement('widebody_spotter', 'Widebody Spotter', '🛫', 'Log 100 widebody aircraft (747, 777, 787, A330, A350, A380)', widebody_count >= 100, widebody_count, 100)
    add_achievement('widebody_fan', 'Widebody Fan', '🛬', 'Log 500 widebody aircraft', widebody_count >= 500, widebody_count, 500)
    add_achievement('widebody_expert', 'Widebody Expert', '✨', 'Log 2,000 widebody aircraft', widebody_count >= 2000, widebody_count, 2000)

    # --- Manufacturer Diversity (from pre-computed counts) ---
    add_achievement('boeing_buff', 'Boeing Buff', '🔵', 'Log 5,000 Boeing aircraft', boeing_count >= 5000, boeing_count, 5000)
    add_achievement('airbus_admirer', 'Airbus Admirer', '🔴', 'Log 5,000 Airbus aircraft', airbus_count >= 5000, airbus_count, 5000)

    # --- Special Operations (from pre-computed counts) ---
    add_achievement('lifesaver', 'EMS Spotter', '🚑', 'Log 25 EMS-type helicopters (EC135, EC145, Bell 407, etc.)', ems_heli_count >= 25, ems_heli_count, 25)
    add_achievement('coast_watcher', 'Coast Watcher', '⚓', 'Log 50 Coast Guard aircraft', coastguard_count >= 50, coastguard_count, 50)

    # --- Rare Sightings (from pre-computed count) ---
    add_achievement('giant_hunter', 'Giant Hunter', '🦣', 'Log 10 giant aircraft (A380, 747-8, C-5, C-17, An-124)', giant_count >= 10, giant_count, 10)
    add_achievement('giant_collector', 'Giant Collector', '🏛️', 'Log 100 giant aircraft', giant_count >= 100, giant_count, 100)

    # --- Turboprop Achievements (from pre-computed count) ---
    add_achievement('prop_head', 'Prop Head', '🌀', 'Log 500 turboprop aircraft', turboprop_count >= 500, turboprop_count, 500)
    add_achievement('prop_master', 'Prop Master', '💫', 'Log 2,000 turboprop aircraft', turboprop_count >= 2000, turboprop_count, 2000)

    # --- International Carrier Achievements (~13/month at hub) ---
    add_achievement('world_traveler', 'World Traveler', '🌍', 'See 10 international carriers', intl_carriers >= 10, intl_carriers, 10)
    add_achievement('globe_trotter', 'Globe Trotter', '🌏', 'See 20 international carriers', intl_carriers >= 20, intl_carriers, 20)
    add_achievement('international_expert', 'International Expert', '🗺️', 'See 35 international carriers', intl_carriers >= 35, intl_carriers, 35)
    add_achievement('international_master', 'International Master', '🌐', 'See 50 international carriers', intl_carriers >= 50, intl_carriers, 50)

    # --- Consecutive Day Streaks (from pre-computed max_streak) ---
    add_achievement('streak_7', 'Week Streak', '🔥', '7 consecutive days of logging', max_streak >= 7, max_streak, 7)
    add_achievement('streak_30', 'Month Streak', '💪', '30 consecutive days of logging', max_streak >= 30, max_streak, 30)
    add_achievement('streak_90', 'Quarter Streak', '🏃', '90 consecutive days of logging', max_streak >= 90, max_streak, 90)
    add_achievement('streak_180', 'Half Year Streak', '🏅', '180 consecutive days of logging', max_streak >= 180, max_streak, 180)
    add_achievement('streak_365', 'Year Streak', '🥇', '365 consecutive days of logging', max_streak >= 365, max_streak, 365)
    add_achievement('streak_730', 'Two Year Streak', '👑', '730 consecutive days of logging', max_streak >= 730, max_streak, 730)

    # --- Hour Diversity (from pre-computed hours bitmask) ---
    add_achievement('around_clock', 'Around the Clock', '🕐', 'Log aircraft in all 24 hours of day', hours_covered >= 24, hours_covered, 24)

    # --- Emergency Squawk Achievements (from pre-computed count) ---
    add_achievement('emergency_first', 'First Responder', '🚨', 'Log aircraft with emergency squawk', emergency_count >= 1, emergency_count, 1)
    add_achievement('emergency_5', 'Emergency Expert', '🆘', 'Log 5 emergency squawk aircraft', emergency_count >= 5, emergency_count, 5)
    add_achievement('emergency_10', 'Emergency Veteran', '🚑', 'Log 10 emergency squawk aircraft', emergency_count >= 10, emergency_count, 10)
    
    # --- Bizjet/Private Achievement (TODO: pre-compute in v1.3.2) ---
    # Bizjet and regional achievements temporarily disabled - queries too slow on large databases
    # These will be re-enabled with pre-computed counts in a future update
    bizjet_count = 0  # Placeholder - will pre-compute later
    add_achievement('bizjet_spotter', 'Bizjet Spotter', '💼', 'Log 5,000 business jets', bizjet_count >= 5000, bizjet_count, 5000)
    add_achievement('bizjet_expert', 'Bizjet Expert', '🤵', 'Log 15,000 business jets', bizjet_count >= 15000, bizjet_count, 15000)
    add_achievement('bizjet_master', 'High Roller', '💰', 'Log 50,000 business jets', bizjet_count >= 50000, bizjet_count, 50000)

    # --- Regional Jet Achievements (TODO: pre-compute in v1.3.2) ---
    regional_count = 0  # Placeholder - will pre-compute later
    add_achievement('regional_spotter', 'Regional Spotter', '🛫', 'Log 5,000 regional aircraft', regional_count >= 5000, regional_count, 5000)
    add_achievement('regional_expert', 'Regional Expert', '🛬', 'Log 25,000 regional aircraft', regional_count >= 25000, regional_count, 25000)
    add_achievement('regional_master', 'Regional Master', '🏛️', 'Log 100,000 regional aircraft', regional_count >= 100000, regional_count, 100000)

    # --- Altitude Achievements (use pre-computed max from stats_summary) ---
    max_alt = stats['max_altitude'] or 0 if stats else 0
    add_achievement('high_flyer', 'High Flyer', '✈️', 'Log aircraft above FL400', max_alt >= 40000, max_alt, 40000)
    add_achievement('jet_stream', 'Jet Stream', '💨', 'Log aircraft above FL450', max_alt >= 45000, max_alt, 45000)
    add_achievement('stratosphere', 'Stratosphere', '🚀', 'Log aircraft above FL500', max_alt >= 50000, max_alt, 50000)
    add_achievement('edge_of_space', 'Edge of Space', '🛸', 'Log aircraft above FL550 (military/special)', max_alt >= 55000, max_alt, 55000)

    # --- Speed Achievement (use pre-computed max from stats_summary) ---
    max_speed = stats['max_speed'] or 0 if stats else 0
    add_achievement('speed_demon', 'Speed Demon', '⚡', 'Log aircraft above 600 knots', max_speed >= 600, max_speed, 600)
    add_achievement('supersonic', 'Near Supersonic', '💨', 'Log aircraft above 700 knots', max_speed >= 700, max_speed, 700)
    add_achievement('mach_buster', 'Mach Buster', '🔊', 'Log aircraft above 800 knots', max_speed >= 800, max_speed, 800)
    
    # --- Special Daily Achievements (use pre-computed busiest_day_count from stats_summary) ---
    busiest_day_count = stats['busiest_day_count'] or 0 if stats else 0
    # Daily achievements - EXTREME mode (rebalanced v1.3.0 - 5x harder)
    add_achievement('busy_day', 'Busy Day', '🔥', 'Log 10,000+ aircraft in one day', busiest_day_count >= 10000, busiest_day_count, 10000)
    add_achievement('crazy_day', 'Crazy Day', '🌪️', 'Log 25,000+ aircraft in one day', busiest_day_count >= 25000, busiest_day_count, 25000)
    add_achievement('insane_day', 'Insane Day', '💥', 'Log 50,000+ aircraft in one day', busiest_day_count >= 50000, busiest_day_count, 50000)
    add_achievement('legendary_day', 'Legendary Day', '🌟', 'Log 100,000+ aircraft in one day', busiest_day_count >= 100000, busiest_day_count, 100000)
    
    conn.close()

    # Sort: unlocked first, then by progress percentage
    achievements.sort(key=lambda x: (not x['unlocked'], -(x['progress'] or 0) / (x['target'] or 1) if x['target'] else 0))

    # Count unlocked
    unlocked_count = sum(1 for a in achievements if a['unlocked'])

    return {
        'achievements': achievements,
        'unlocked': unlocked_count,
        'total': len(achievements)
    }


def calculate_calendar():
    """Calculate calendar data for current month - runs in background thread."""
    from datetime import datetime, timedelta

    conn = get_db()
    cursor = conn.cursor()

    # Current month
    year, month = datetime.now().year, datetime.now().month
    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{month + 1:02d}-01"

    # Get daily stats
    cursor.execute('''
        SELECT
            date(timestamp) as date,
            COUNT(*) as positions,
            COUNT(DISTINCT icao) as unique_aircraft,
            COUNT(DISTINCT strftime('%H', timestamp)) as active_hours
        FROM positions
        WHERE date(timestamp) >= ? AND date(timestamp) < ?
        GROUP BY date(timestamp)
        ORDER BY date
    ''', (start_date, end_date))
    daily_rows = cursor.fetchall()

    # Get ALL hourly data in ONE query
    cursor.execute('''
        SELECT date(timestamp) as date, strftime('%H', timestamp) as hour, COUNT(*) as count
        FROM positions
        WHERE date(timestamp) >= ? AND date(timestamp) < ?
        GROUP BY date(timestamp), strftime('%H', timestamp)
    ''', (start_date, end_date))

    hourly_by_date = {}
    for r in cursor.fetchall():
        date_str = r['date']
        if date_str not in hourly_by_date:
            hourly_by_date[date_str] = {}
        hourly_by_date[date_str][int(r['hour'])] = r['count']

    # Build calendar data
    calendar_days = []
    for row in daily_rows:
        date_str = row['date']
        active_hours = row['active_hours']

        if active_hours >= 18:
            status = 'full'
        elif active_hours >= 6:
            status = 'partial'
        elif active_hours > 0:
            status = 'low'
        else:
            status = 'offline'

        hourly_data = hourly_by_date.get(date_str, {})
        gaps = []
        gap_start = None
        for hour in range(6, 22):
            if hour not in hourly_data or hourly_data[hour] == 0:
                if gap_start is None:
                    gap_start = hour
            else:
                if gap_start is not None:
                    gap_duration = hour - gap_start
                    if gap_duration >= 2:
                        gaps.append({'start': f"{gap_start:02d}:00", 'end': f"{hour:02d}:00", 'duration_hours': gap_duration})
                    gap_start = None
        if gap_start is not None:
            gap_duration = 22 - gap_start
            if gap_duration >= 2:
                gaps.append({'start': f"{gap_start:02d}:00", 'end': "22:00", 'duration_hours': gap_duration})

        calendar_days.append({
            'date': date_str,
            'positions': row['positions'],
            'unique_aircraft': row['unique_aircraft'],
            'active_hours': active_hours,
            'status': status,
            'gaps': gaps
        })

    # Calculate streaks
    all_dates = [d['date'] for d in calendar_days]
    current_streak = 0
    longest_streak = 0

    if all_dates:
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        today_str = today.strftime('%Y-%m-%d')
        yesterday_str = yesterday.strftime('%Y-%m-%d')

        if today_str in all_dates or yesterday_str in all_dates:
            check_date = today if today_str in all_dates else yesterday
            while check_date.strftime('%Y-%m-%d') in all_dates:
                current_streak += 1
                check_date -= timedelta(days=1)

        sorted_dates = sorted(all_dates)
        streak_count = 1
        for i in range(1, len(sorted_dates)):
            prev_date = datetime.strptime(sorted_dates[i-1], '%Y-%m-%d').date()
            curr_date = datetime.strptime(sorted_dates[i], '%Y-%m-%d').date()
            if (curr_date - prev_date).days == 1:
                streak_count += 1
            else:
                longest_streak = max(longest_streak, streak_count)
                streak_count = 1
        longest_streak = max(longest_streak, streak_count)

    full_days = sum(1 for d in calendar_days if d['status'] == 'full')
    partial_days = sum(1 for d in calendar_days if d['status'] == 'partial')

    conn.close()

    return {
        'year': year,
        'month': month,
        'range_type': 'month',
        'days': calendar_days,
        'summary': {
            'total_days': len(calendar_days),
            'full_days': full_days,
            'partial_days': partial_days,
            'current_streak': current_streak,
            'longest_streak': longest_streak
        }
    }


def prefetch_clickable_aircraft():
    """Pre-fetch aircraft details for all clickable items on homepage to warm the cache."""
    global records_cache, overview_cache, leaderboard_cache, gallery_cache, aircraft_detail_cache

    icaos_to_fetch = set()

    # 1. Record holder ICAOs (Personal Records section)
    if records_cache['data']:
        for key, record in records_cache['data'].items():
            if isinstance(record, dict) and record.get('icao'):
                icaos_to_fetch.add(record['icao'].upper())

    # 2. Rare type sample ICAOs (Rarest Catches section)
    if overview_cache['data'] and 'rare_types' in overview_cache['data']:
        for rare in overview_cache['data']['rare_types'][:10]:
            for icao in rare.get('sample_icaos', []):
                if icao:
                    icaos_to_fetch.add(icao.upper())

    # 3. Top leaderboard aircraft (Aircraft Leaderboard section - top 15 from each time range)
    if leaderboard_cache['data']:
        for time_range, data in leaderboard_cache['data'].items():
            if isinstance(data, dict) and 'aircraft' in data:
                for aircraft in data['aircraft'][:15]:
                    if aircraft.get('icao'):
                        icaos_to_fetch.add(aircraft['icao'].upper())

    # 4. Gallery sample ICAOs (Aircraft Gallery section - one sample per type)
    if gallery_cache['data'] and 'types' in gallery_cache['data']:
        for type_info in gallery_cache['data']['types'][:50]:  # Top 50 types
            if type_info.get('sample_icao'):
                icaos_to_fetch.add(type_info['sample_icao'].upper())

    if not icaos_to_fetch:
        return

    log.info(f"  Pre-fetching {len(icaos_to_fetch)} aircraft details...")
    start = time.time()
    fetched = 0

    for icao in icaos_to_fetch:
        # Skip if already cached and not expired (15 min TTL)
        if icao in aircraft_detail_cache:
            cached = aircraft_detail_cache[icao]
            if time.time() - cached['time'] < 900:
                continue

        try:
            # Fetch and cache the aircraft detail
            with app.test_request_context():
                response = api_aircraft_detail(icao)
                fetched += 1
        except Exception as e:
            log.warning(f"    Failed to prefetch {icao}: {e}")

    elapsed = time.time() - start
    log.info(f"  Pre-fetched {fetched} aircraft details ({elapsed:.1f}s)")


def update_dashboard_caches():
    """Background thread to update all dashboard caches every 60 seconds."""
    global achievements_cache, leaderboard_cache, overview_cache, records_cache, gallery_cache, calendar_cache

    # Initial calculation on startup
    log.info("Calculating initial dashboard caches...")
    start_total = time.time()

    # Calculate each cache with error handling
    # NOTE: heatmap removed in v1.3.0 - saves ~130s per cycle!
    cache_configs = [
        ('leaderboard', leaderboard_cache, calculate_leaderboard),
        ('overview', overview_cache, calculate_overview),
        ('records', records_cache, calculate_records),
        ('gallery', gallery_cache, calculate_gallery),
        ('calendar', calendar_cache, calculate_calendar),
        ('achievements', achievements_cache, calculate_achievements),
    ]

    for name, cache, calc_func in cache_configs:
        try:
            start = time.time()
            cache['data'] = calc_func()
            cache['last_updated'] = time.time()
            elapsed = time.time() - start
            log.info(f"  {name} cache ready ({elapsed:.1f}s)")
        except Exception as e:
            log.error(f"  Failed {name} cache: {e}")

    total_elapsed = time.time() - start_total
    log.info(f"All dashboard caches ready in {total_elapsed:.1f}s")

    # Pre-fetch record holder details to warm the cache
    try:
        prefetch_clickable_aircraft()
    except Exception as e:
        log.error(f"Failed to prefetch record holder details: {e}")

    # Update every 10 minutes (600s)
    while True:
        time.sleep(600)
        start_total = time.time()

        for name, cache, calc_func in cache_configs:
            try:
                start = time.time()
                cache['data'] = calc_func()
                cache['last_updated'] = time.time()
                elapsed = time.time() - start
                log.info(f"  {name} cache updated ({elapsed:.1f}s)")
            except Exception as e:
                log.error(f"Failed to update {name} cache: {e}")
                # Keep serving old cache if calculation fails

        # Pre-fetch record holder details after cache update
        try:
            prefetch_clickable_aircraft()
        except Exception as e:
            log.error(f"Failed to prefetch record holder details: {e}")

        total_elapsed = time.time() - start_total
        log.info(f"Dashboard caches updated in {total_elapsed:.1f}s")


@app.route('/api/achievements')
def api_achievements():
    """Return cached achievements instantly."""
    if achievements_cache['data'] is None:
        return jsonify({'error': 'Achievements not yet calculated, please wait...'}), 503

    response = achievements_cache['data'].copy()
    response['cached'] = True
    response['cache_age'] = int(time.time() - achievements_cache['last_updated']) if achievements_cache['last_updated'] else None
    return jsonify(response)


def calculate_gallery():
    """Calculate gallery data - runs in background thread."""
    conn = get_db()
    cursor = conn.cursor()

    # Aircraft type database - maps ICAO type codes to descriptions
    # Format: 'CODE': ('Name', 'Manufacturer', 'Category')
    AIRCRAFT_DB = {
        # Boeing Commercial
        'B712': ('717-200', 'Boeing', 'jet'), 'B731': ('737-100', 'Boeing', 'jet'),
        'B732': ('737-200', 'Boeing', 'jet'), 'B733': ('737-300', 'Boeing', 'jet'),
        'B734': ('737-400', 'Boeing', 'jet'), 'B735': ('737-500', 'Boeing', 'jet'),
        'B736': ('737-600', 'Boeing', 'jet'), 'B737': ('737-700', 'Boeing', 'jet'),
        'B738': ('737-800', 'Boeing', 'jet'), 'B739': ('737-900', 'Boeing', 'jet'),
        'B37M': ('737 MAX 7', 'Boeing', 'jet'), 'B38M': ('737 MAX 8', 'Boeing', 'jet'),
        'B39M': ('737 MAX 9', 'Boeing', 'jet'), 'B3XM': ('737 MAX 10', 'Boeing', 'jet'),
        'B741': ('747-100', 'Boeing', 'jet'), 'B742': ('747-200', 'Boeing', 'jet'),
        'B743': ('747-300', 'Boeing', 'jet'), 'B744': ('747-400', 'Boeing', 'jet'),
        'B748': ('747-8', 'Boeing', 'jet'), 'B74S': ('747SP', 'Boeing', 'jet'),
        'B752': ('757-200', 'Boeing', 'jet'), 'B753': ('757-300', 'Boeing', 'jet'),
        'B762': ('767-200', 'Boeing', 'jet'), 'B763': ('767-300', 'Boeing', 'jet'),
        'B764': ('767-400', 'Boeing', 'jet'), 'B772': ('777-200', 'Boeing', 'jet'),
        'B773': ('777-300', 'Boeing', 'jet'), 'B77L': ('777-200LR', 'Boeing', 'jet'),
        'B77W': ('777-300ER', 'Boeing', 'jet'), 'B778': ('777-8', 'Boeing', 'jet'),
        'B779': ('777-9', 'Boeing', 'jet'), 'B788': ('787-8 Dreamliner', 'Boeing', 'jet'),
        'B789': ('787-9 Dreamliner', 'Boeing', 'jet'), 'B78X': ('787-10 Dreamliner', 'Boeing', 'jet'),
        # Airbus Commercial
        'A318': ('A318', 'Airbus', 'jet'), 'A319': ('A319', 'Airbus', 'jet'),
        'A320': ('A320', 'Airbus', 'jet'), 'A321': ('A321', 'Airbus', 'jet'),
        'A19N': ('A319neo', 'Airbus', 'jet'), 'A20N': ('A320neo', 'Airbus', 'jet'),
        'A21N': ('A321neo', 'Airbus', 'jet'), 'A332': ('A330-200', 'Airbus', 'jet'),
        'A333': ('A330-300', 'Airbus', 'jet'), 'A338': ('A330-800neo', 'Airbus', 'jet'),
        'A339': ('A330-900neo', 'Airbus', 'jet'), 'A342': ('A340-200', 'Airbus', 'jet'),
        'A343': ('A340-300', 'Airbus', 'jet'), 'A345': ('A340-500', 'Airbus', 'jet'),
        'A346': ('A340-600', 'Airbus', 'jet'), 'A359': ('A350-900', 'Airbus', 'jet'),
        'A35K': ('A350-1000', 'Airbus', 'jet'), 'A380': ('A380', 'Airbus', 'jet'),
        'A388': ('A380-800', 'Airbus', 'jet'),
        # Embraer
        'E170': ('E170', 'Embraer', 'jet'), 'E175': ('E175', 'Embraer', 'jet'),
        'E190': ('E190', 'Embraer', 'jet'), 'E195': ('E195', 'Embraer', 'jet'),
        'E75S': ('E175 Short', 'Embraer', 'jet'), 'E75L': ('E175 Long', 'Embraer', 'jet'),
        'E290': ('E190-E2', 'Embraer', 'jet'), 'E295': ('E195-E2', 'Embraer', 'jet'),
        'E135': ('ERJ-135', 'Embraer', 'jet'), 'E145': ('ERJ-145', 'Embraer', 'jet'),
        'E35L': ('Legacy 600/650', 'Embraer', 'jet'), 'E50P': ('Phenom 100', 'Embraer', 'jet'),
        'E55P': ('Phenom 300', 'Embraer', 'jet'), 'E545': ('Legacy 450', 'Embraer', 'jet'),
        'E550': ('Praetor 500/600', 'Embraer', 'jet'),
        # Bombardier/CRJ
        'CRJ1': ('CRJ-100', 'Bombardier', 'jet'), 'CRJ2': ('CRJ-200', 'Bombardier', 'jet'),
        'CRJ7': ('CRJ-700', 'Bombardier', 'jet'), 'CRJ9': ('CRJ-900', 'Bombardier', 'jet'),
        'CRJX': ('CRJ-1000', 'Bombardier', 'jet'), 'CL30': ('Challenger 300', 'Bombardier', 'jet'),
        'CL35': ('Challenger 350', 'Bombardier', 'jet'), 'CL60': ('Challenger 600', 'Bombardier', 'jet'),
        'GL5T': ('Global 5000', 'Bombardier', 'jet'), 'GL7T': ('Global 7500', 'Bombardier', 'jet'),
        'GLEX': ('Global Express', 'Bombardier', 'jet'),
        # Gulfstream
        'G280': ('G280', 'Gulfstream', 'jet'), 'GLF4': ('G-IV', 'Gulfstream', 'jet'),
        'GLF5': ('G-V', 'Gulfstream', 'jet'), 'GLF6': ('G650', 'Gulfstream', 'jet'),
        'G550': ('G550', 'Gulfstream', 'jet'), 'G650': ('G650', 'Gulfstream', 'jet'),
        # Cessna Jets
        'C25A': ('Citation CJ2', 'Cessna', 'jet'), 'C25B': ('Citation CJ3', 'Cessna', 'jet'),
        'C25C': ('Citation CJ4', 'Cessna', 'jet'), 'C510': ('Citation Mustang', 'Cessna', 'jet'),
        'C525': ('CitationJet', 'Cessna', 'jet'), 'C550': ('Citation II', 'Cessna', 'jet'),
        'C560': ('Citation V/Ultra', 'Cessna', 'jet'), 'C56X': ('Citation Excel', 'Cessna', 'jet'),
        'C680': ('Citation Sovereign', 'Cessna', 'jet'), 'C68A': ('Citation Latitude', 'Cessna', 'jet'),
        'C700': ('Citation Longitude', 'Cessna', 'jet'), 'C750': ('Citation X', 'Cessna', 'jet'),
        # Learjet
        'LJ35': ('Learjet 35', 'Bombardier', 'jet'), 'LJ45': ('Learjet 45', 'Bombardier', 'jet'),
        'LJ60': ('Learjet 60', 'Bombardier', 'jet'), 'LJ75': ('Learjet 75', 'Bombardier', 'jet'),
        # Dassault
        'F900': ('Falcon 900', 'Dassault', 'jet'), 'F2TH': ('Falcon 2000', 'Dassault', 'jet'),
        'FA50': ('Falcon 50', 'Dassault', 'jet'), 'FA7X': ('Falcon 7X', 'Dassault', 'jet'),
        'FA8X': ('Falcon 8X', 'Dassault', 'jet'),
        # Helicopters
        'R22': ('R22', 'Robinson', 'helicopter'), 'R44': ('R44', 'Robinson', 'helicopter'),
        'R66': ('R66', 'Robinson', 'helicopter'),
        'B06': ('JetRanger', 'Bell', 'helicopter'), 'B06T': ('JetRanger', 'Bell', 'helicopter'),
        'B206': ('JetRanger', 'Bell', 'helicopter'), 'B407': ('Bell 407', 'Bell', 'helicopter'),
        'B412': ('Bell 412', 'Bell', 'helicopter'), 'B429': ('Bell 429', 'Bell', 'helicopter'),
        'B430': ('Bell 430', 'Bell', 'helicopter'), 'B505': ('Bell 505', 'Bell', 'helicopter'),
        'EC20': ('EC120', 'Airbus Helicopters', 'helicopter'),
        'EC25': ('EC225', 'Airbus Helicopters', 'helicopter'),
        'EC30': ('EC130', 'Airbus Helicopters', 'helicopter'),
        'EC35': ('EC135', 'Airbus Helicopters', 'helicopter'),
        'EC45': ('EC145', 'Airbus Helicopters', 'helicopter'),
        'H125': ('H125 AStar', 'Airbus Helicopters', 'helicopter'),
        'H130': ('H130', 'Airbus Helicopters', 'helicopter'),
        'H135': ('H135', 'Airbus Helicopters', 'helicopter'),
        'H145': ('H145', 'Airbus Helicopters', 'helicopter'),
        'H160': ('H160', 'Airbus Helicopters', 'helicopter'),
        'H175': ('H175', 'Airbus Helicopters', 'helicopter'),
        'AS50': ('AS350 Ecureuil', 'Airbus Helicopters', 'helicopter'),
        'AS55': ('AS355 Twin', 'Airbus Helicopters', 'helicopter'),
        'AS65': ('AS365 Dauphin', 'Airbus Helicopters', 'helicopter'),
        'A109': ('AW109', 'Leonardo', 'helicopter'),
        'A139': ('AW139', 'Leonardo', 'helicopter'),
        'A169': ('AW169', 'Leonardo', 'helicopter'),
        'A189': ('AW189', 'Leonardo', 'helicopter'),
        'S76': ('S-76', 'Sikorsky', 'helicopter'),
        'S92': ('S-92', 'Sikorsky', 'helicopter'),
        'S70': ('S-70/UH-60', 'Sikorsky', 'helicopter'),
        'MD52': ('MD 520N', 'MD Helicopters', 'helicopter'),
        'MD60': ('MD 600N', 'MD Helicopters', 'helicopter'),
        # Military
        'F15': ('F-15 Eagle', 'Boeing', 'military'), 'F16': ('F-16 Fighting Falcon', 'Lockheed Martin', 'military'),
        'F18': ('F/A-18 Hornet', 'Boeing', 'military'), 'F22': ('F-22 Raptor', 'Lockheed Martin', 'military'),
        'F35': ('F-35 Lightning II', 'Lockheed Martin', 'military'), 'A10': ('A-10 Thunderbolt II', 'Fairchild', 'military'),
        'B1': ('B-1 Lancer', 'Rockwell/Boeing', 'military'), 'B2': ('B-2 Spirit', 'Northrop Grumman', 'military'),
        'B52': ('B-52 Stratofortress', 'Boeing', 'military'), 'C17': ('C-17 Globemaster', 'Boeing', 'military'),
        'C130': ('C-130 Hercules', 'Lockheed', 'military'), 'C5': ('C-5 Galaxy', 'Lockheed', 'military'),
        'C5M': ('C-5M Super Galaxy', 'Lockheed', 'military'), 'KC10': ('KC-10 Extender', 'McDonnell Douglas', 'military'),
        'KC135': ('KC-135 Stratotanker', 'Boeing', 'military'), 'KC46': ('KC-46 Pegasus', 'Boeing', 'military'),
        'V22': ('V-22 Osprey', 'Bell/Boeing', 'military'), 'E3': ('E-3 Sentry AWACS', 'Boeing', 'military'),
        'E6': ('E-6 Mercury', 'Boeing', 'military'), 'P8': ('P-8 Poseidon', 'Boeing', 'military'),
        'RC135': ('RC-135', 'Boeing', 'military'), 'U2': ('U-2', 'Lockheed', 'military'),
        # Turboprops
        'AT43': ('ATR 42-300', 'ATR', 'turboprop'), 'AT45': ('ATR 42-500', 'ATR', 'turboprop'),
        'AT46': ('ATR 42-600', 'ATR', 'turboprop'), 'AT72': ('ATR 72', 'ATR', 'turboprop'),
        'AT75': ('ATR 72-500', 'ATR', 'turboprop'), 'AT76': ('ATR 72-600', 'ATR', 'turboprop'),
        'DH8A': ('Dash 8-100', 'De Havilland', 'turboprop'), 'DH8B': ('Dash 8-200', 'De Havilland', 'turboprop'),
        'DH8C': ('Dash 8-300', 'De Havilland', 'turboprop'), 'DH8D': ('Dash 8-400', 'De Havilland', 'turboprop'),
        'SF34': ('Saab 340', 'Saab', 'turboprop'), 'SB20': ('Saab 2000', 'Saab', 'turboprop'),
        'B190': ('Beech 1900', 'Beechcraft', 'turboprop'),
        'PC12': ('PC-12', 'Pilatus', 'turboprop'), 'PC24': ('PC-24', 'Pilatus', 'jet'),
        'TBM7': ('TBM 700', 'Daher', 'turboprop'), 'TBM8': ('TBM 850/900', 'Daher', 'turboprop'),
        'TBM9': ('TBM 930/960', 'Daher', 'turboprop'),
        'C208': ('Caravan', 'Cessna', 'turboprop'), 'C408': ('SkyCourier', 'Cessna', 'turboprop'),
        # Props/GA
        'C172': ('Skyhawk', 'Cessna', 'prop'), 'C182': ('Skylane', 'Cessna', 'prop'),
        'C206': ('Stationair', 'Cessna', 'prop'), 'C210': ('Centurion', 'Cessna', 'prop'),
        'P28A': ('Cherokee', 'Piper', 'prop'), 'PA28': ('Cherokee', 'Piper', 'prop'),
        'PA32': ('Cherokee Six/Saratoga', 'Piper', 'prop'), 'PA34': ('Seneca', 'Piper', 'prop'),
        'PA44': ('Seminole', 'Piper', 'prop'), 'PA46': ('Malibu/M-Class', 'Piper', 'turboprop'),
        'BE36': ('Bonanza', 'Beechcraft', 'prop'), 'BE58': ('Baron', 'Beechcraft', 'prop'),
        'BE9L': ('King Air 90', 'Beechcraft', 'turboprop'), 'BE20': ('King Air 200', 'Beechcraft', 'turboprop'),
        'BE30': ('King Air 300', 'Beechcraft', 'turboprop'), 'B350': ('King Air 350', 'Beechcraft', 'turboprop'),
        'SR20': ('SR20', 'Cirrus', 'prop'), 'SR22': ('SR22', 'Cirrus', 'prop'),
        'SF50': ('Vision Jet', 'Cirrus', 'jet'),
        'DA40': ('DA40', 'Diamond', 'prop'), 'DA42': ('DA42', 'Diamond', 'prop'),
        'DA62': ('DA62', 'Diamond', 'prop'),
        'M20P': ('Mooney M20', 'Mooney', 'prop'), 'M20T': ('Mooney M20 Turbo', 'Mooney', 'prop'),
    }

    # Use type_summary table for INSTANT queries (vs scanning 1.6M+ positions)
    cursor.execute('''
        SELECT
            aircraft_type,
            unique_aircraft as aircraft_count,
            total_positions as sighting_count,
            first_seen,
            last_seen,
            sample_icao
        FROM type_summary
        ORDER BY unique_aircraft DESC
    ''')

    types = []
    for row in cursor.fetchall():
        ac_type = row['aircraft_type']
        type_upper = ac_type.upper() if ac_type else ''

        # Look up in database for detailed info
        db_entry = AIRCRAFT_DB.get(ac_type) or AIRCRAFT_DB.get(type_upper)

        if db_entry:
            name, manufacturer, category = db_entry
        else:
            # Fallback categorization
            name = ac_type  # Use type code as name if unknown
            manufacturer = 'Unknown'
            category = 'other'

            # Try to infer category from type code patterns
            heli_prefixes = ['R22', 'R44', 'R66', 'B06', 'B206', 'B407', 'B429', 'EC', 'H1', 'H2', 'AS', 'A109', 'A139', 'S76', 'S92', 'MD5', 'MD9', 'BK', 'NH90', 'UH', 'AW']
            if any(type_upper.startswith(h) for h in heli_prefixes):
                category = 'helicopter'
            elif any(type_upper.startswith(j) for j in ['B73', 'B74', 'B75', 'B76', 'B77', 'B78', 'A31', 'A32', 'A33', 'A34', 'A35', 'A38', 'A22', 'E17', 'E19', 'E75', 'CRJ', 'E45', 'C56', 'C68', 'C70', 'CL', 'GLF', 'G2', 'G3', 'G4', 'G5', 'G6', 'LJ', 'FA', 'H25', 'BE4', 'C25', 'C52', 'C55', 'C56', 'PC24', 'GLEX', 'GA5', 'GA6']):
                category = 'jet'
            elif any(type_upper.startswith(t) for t in ['AT', 'DH', 'SF34', 'E120', 'B190', 'PC12', 'TBM', 'C208', 'PA46']):
                category = 'turboprop'
            elif any(type_upper.startswith(p) for p in ['C1', 'C2', 'PA', 'BE', 'M20', 'SR2', 'DA', 'P28', 'C17', 'C18', 'C19', 'C20', 'C21', 'RV']):
                category = 'prop'
            elif any(type_upper.startswith(m) for m in ['F15', 'F16', 'F18', 'F22', 'F35', 'C17', 'C130', 'C5', 'KC', 'B1', 'B2', 'B52', 'A10', 'V22', 'MQ', 'RQ', 'E3', 'E8', 'P8', 'RC', 'U2']):
                category = 'military'

        types.append({
            'type': ac_type,
            'name': name,
            'manufacturer': manufacturer,
            'category': category,
            'aircraft_count': row['aircraft_count'],
            'sighting_count': row['sighting_count'],
            'first_seen': row['first_seen'],
            'last_seen': row['last_seen'],
            'sample_icao': row['sample_icao']
        })

    conn.close()

    # Get category totals
    category_counts = {}
    manufacturer_counts = {}
    for t in types:
        cat = t['category']
        mfr = t['manufacturer']
        category_counts[cat] = category_counts.get(cat, 0) + 1
        if mfr != 'Unknown':
            manufacturer_counts[mfr] = manufacturer_counts.get(mfr, 0) + 1

    return {
        'types': types,
        'total_types': len(types),
        'categories': category_counts,
        'manufacturers': manufacturer_counts,
        'description': 'The Type Gallery shows all unique ICAO aircraft type codes logged by your receiver. Each type represents a different aircraft model (e.g., B738 = Boeing 737-800). Types are categorized by aircraft class and enriched with manufacturer information where known.'
    }


@app.route('/api/gallery')
def api_gallery():
    """Return cached gallery data."""
    if gallery_cache['data'] is None:
        return jsonify({'error': 'Gallery not yet calculated, please wait...'}), 503

    response = gallery_cache['data'].copy()
    response['cached'] = True
    response['cache_age'] = int(time.time() - gallery_cache['last_updated']) if gallery_cache['last_updated'] else None
    return jsonify(response)


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    log.info("=" * 60)
    log.info("EasyADSB Flight Logger v1.3.0")
    log.info("=" * 60)
    log.info(f"Ultrafeeder: {ULTRAFEEDER_HOST}:{ULTRAFEEDER_PORT}")
    log.info(f"Interval: {LOG_INTERVAL} seconds")
    log.info(f"Retention: {LOG_RETENTION_DAYS} days (0 = forever)")
    log.info(f"Database: {DB_PATH}")
    log.info("=" * 60)
    
    # Initialize
    init_db()
    load_config()

    # Backfill summary tables if needed (one-time migration for existing data)
    backfill_summary_tables()

    # Start logger thread
    logger_thread = threading.Thread(target=logger_loop, daemon=True)
    logger_thread.start()

    # Start dashboard cache thread (calculates all caches immediately, then every 60s)
    cache_thread = threading.Thread(target=update_dashboard_caches, daemon=True)
    cache_thread.start()

    # Run initial cleanup
    cleanup_old_records()
    
    # Start Flask server
    log.info("Starting API server on port 5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
