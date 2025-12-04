#!/usr/bin/env python3
"""
EasyADSB Flight Logger
Version: 1.2.1

Polls ultrafeeder for aircraft data and stores in SQLite database.
Provides REST API for dashboard integration.

Endpoints:
    GET  /health           - Health check
    GET  /api/stats        - Get logging statistics
    GET  /api/settings     - Get current settings
    POST /api/settings     - Update settings
    GET  /api/userconfig   - Get user dashboard config (e.g., ADSBx Short ID)
    POST /api/userconfig   - Update user dashboard config
    GET  /api/export       - Download logs as CSV
    GET  /api/export/json  - Download logs as JSON
    GET  /api/flights      - Query flights with filters
    GET  /api/trace/<icao> - Get flight path for aircraft
    POST /api/pause        - Pause logging
    POST /api/resume       - Resume logging
    POST /api/clear        - Clear all logs
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
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Flask, jsonify, request, Response, send_file

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

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

def get_db():
    """Get database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables."""
    log.info(f"Initializing database at {DB_PATH}")
    conn = get_db()
    cursor = conn.cursor()
    
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
            rssi REAL
        )
    ''')
    
    # Index for common queries
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON positions(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_icao ON positions(icao)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_callsign ON positions(callsign)')
    
    # Stats table for daily summaries
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            total_positions INTEGER DEFAULT 0,
            unique_aircraft INTEGER DEFAULT 0,
            unique_flights INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()
    log.info("Database initialized")

def save_aircraft(aircraft_list):
    """Save aircraft positions to database."""
    global total_logged
    
    if not aircraft_list:
        return 0
    
    conn = get_db()
    cursor = conn.cursor()
    
    count = 0
    for ac in aircraft_list:
        # Skip aircraft without position
        if 'lat' not in ac or 'lon' not in ac:
            continue
            
        cursor.execute('''
            INSERT INTO positions (icao, callsign, lat, lon, altitude, speed, track, vert_rate, squawk, category, aircraft_type, rssi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            ac.get('hex', '').upper(),
            ac.get('flight', '').strip() if ac.get('flight') else None,
            ac.get('lat'),
            ac.get('lon'),
            ac.get('alt_baro') or ac.get('alt_geom'),
            ac.get('gs'),
            ac.get('track'),
            ac.get('baro_rate') or ac.get('geom_rate'),
            ac.get('squawk'),
            ac.get('category'),
            ac.get('t'),
            ac.get('rssi')
        ))
        count += 1
    
    conn.commit()
    conn.close()
    
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
        
        # Vacuum to reclaim space (do periodically, not every cleanup)
        cursor.execute('VACUUM')
    
    conn.close()
    return count

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
    
    query = 'SELECT timestamp, lat, lon, altitude, speed, track FROM positions WHERE icao = ?'
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
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    log.info("=" * 60)
    log.info("EasyADSB Flight Logger v1.2.1")
    log.info("=" * 60)
    log.info(f"Ultrafeeder: {ULTRAFEEDER_HOST}:{ULTRAFEEDER_PORT}")
    log.info(f"Interval: {LOG_INTERVAL} seconds")
    log.info(f"Retention: {LOG_RETENTION_DAYS} days (0 = forever)")
    log.info(f"Database: {DB_PATH}")
    log.info("=" * 60)
    
    # Initialize
    init_db()
    load_config()
    
    # Start logger thread
    logger_thread = threading.Thread(target=logger_loop, daemon=True)
    logger_thread.start()
    
    # Run initial cleanup
    cleanup_old_records()
    
    # Start Flask server
    log.info("Starting API server on port 5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
