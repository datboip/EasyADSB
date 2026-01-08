#!/usr/bin/env python3
"""
EasyADSB Backup Service
Simple API to create and download backups for migration
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allow dashboard to call this API

# Paths
BACKUP_DIR = Path("/backups")
CONFIG_DIR = Path("/config")
DATA_DIR = Path("/data")

BACKUP_DIR.mkdir(exist_ok=True)


@app.route("/api/backup/status")
def status():
    """Check if backup service is running"""
    return jsonify({"status": "ok", "service": "easyadsb-backup"})


@app.route("/api/backup/create/<backup_type>", methods=["POST", "GET"])
def create_backup(backup_type):
    """Create a backup - config, logs, or full"""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if backup_type == "config":
            # Just config files
            backup_name = f"easyadsb-config-{timestamp}.tar.gz"
            backup_path = BACKUP_DIR / backup_name

            files = []
            if (CONFIG_DIR / ".env").exists():
                files.append(".env")
            if (CONFIG_DIR / "dashboard-config.js").exists():
                files.append("dashboard-config.js")

            if not files:
                return jsonify({"success": False, "error": "No config files found"}), 400

            subprocess.run(
                ["tar", "-czf", str(backup_path)] + files,
                cwd=CONFIG_DIR, check=True
            )

        elif backup_type == "full":
            # Everything - config + all data
            backup_name = f"easyadsb-full-{timestamp}.tar.gz"
            backup_path = BACKUP_DIR / backup_name

            # Create tar with config files first
            cmd = ["tar", "-czf", str(backup_path), "--warning=no-file-changed"]

            # Add config files
            config_files = []
            if (CONFIG_DIR / ".env").exists():
                config_files.extend(["-C", str(CONFIG_DIR), ".env"])
            if (CONFIG_DIR / "dashboard-config.js").exists():
                config_files.extend(["-C", str(CONFIG_DIR), "dashboard-config.js"])

            # Add data directory
            if DATA_DIR.exists():
                config_files.extend(["-C", "/", "data"])

            # Run tar - exit code 1 is OK (means files changed during archive, but archive is still valid)
            result = subprocess.run(cmd + config_files, capture_output=True)
            if result.returncode not in [0, 1]:
                raise subprocess.CalledProcessError(result.returncode, cmd)

        else:
            return jsonify({"success": False, "error": "Invalid backup type. Use 'config' or 'full'"}), 400

        size = backup_path.stat().st_size

        return jsonify({
            "success": True,
            "filename": backup_name,
            "size": size,
            "type": backup_type,
            "download_url": f"/api/backup/download/{backup_name}"
        })

    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "error": f"Backup failed: {e}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/backup/download/<filename>")
def download_backup(filename):
    """Download a backup file"""
    # Security: only allow files in backup dir
    if ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    backup_path = BACKUP_DIR / filename
    if not backup_path.exists():
        return jsonify({"error": "Backup not found"}), 404

    return send_file(
        backup_path,
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/backup/list")
def list_backups():
    """List available backups"""
    backups = []
    for f in sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True):
        backups.append({
            "name": f.name,
            "size": f.stat().st_size,
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        })
    return jsonify({"backups": backups})


@app.route("/api/backup/delete/<filename>", methods=["POST", "DELETE"])
def delete_backup(filename):
    """Delete a backup file"""
    if ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    backup_path = BACKUP_DIR / filename
    if backup_path.exists():
        backup_path.unlink()
        return jsonify({"success": True})
    return jsonify({"error": "Backup not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("BACKUP_PORT", 8085))
    app.run(host="0.0.0.0", port=port, debug=False)
