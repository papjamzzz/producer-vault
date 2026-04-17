"""
ProducerVault — Auto cloud backup for Ableton projects.
Never lose a session again.
Port 5565
"""

import os, sqlite3, threading, time
from datetime import datetime
from flask import Flask, render_template, jsonify
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import humanize

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'), override=True)

app = Flask(__name__)
DB = os.path.join(os.path.dirname(__file__), 'data', 'vault.db')

WATCH_FOLDER = os.getenv('WATCH_FOLDER', os.path.expanduser('~/Music/Ableton'))
WATCH_EXTS   = {'.als', '.alc', '.adg', '.asd'}

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                filepath    TEXT NOT NULL,
                size_bytes  INTEGER,
                backed_up_at TEXT NOT NULL,
                status      TEXT DEFAULT 'ok'
            )
        """)
        conn.commit()

init_db()

# ── B2 Upload ─────────────────────────────────────────────────────────────────

def upload_to_b2(filepath):
    try:
        from b2sdk.v2 import InMemoryAccountInfo, B2Api
        info   = InMemoryAccountInfo()
        b2_api = B2Api(info)
        b2_api.authorize_account("production",
                                  os.getenv("B2_KEY_ID"),
                                  os.getenv("B2_APPLICATION_KEY"))
        bucket = b2_api.get_bucket_by_name(os.getenv("B2_BUCKET_NAME", "producervault"))
        bucket.upload_local_file(
            local_file=filepath,
            file_name=os.path.basename(filepath)
        )
        return True
    except Exception as e:
        print(f"[B2 ERROR] {e}")
        return False

# ── File Watcher ──────────────────────────────────────────────────────────────

class AbletonHandler(FileSystemEventHandler):
    def on_modified(self, event):
        self._handle(event.src_path)

    def on_created(self, event):
        self._handle(event.src_path)

    def _handle(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext not in WATCH_EXTS:
            return
        print(f"[VAULT] Detected change: {path}")
        size    = os.path.getsize(path) if os.path.exists(path) else 0
        success = upload_to_b2(path)
        status  = 'ok' if success else 'error'
        with get_db() as conn:
            conn.execute(
                "INSERT INTO backups (filename, filepath, size_bytes, backed_up_at, status) VALUES (?,?,?,?,?)",
                (os.path.basename(path), path, size, datetime.utcnow().isoformat(), status)
            )
            conn.commit()
        print(f"[VAULT] Backup {'succeeded' if success else 'FAILED'}: {path}")


def start_watcher():
    if not os.path.exists(WATCH_FOLDER):
        print(f"[VAULT] Watch folder not found: {WATCH_FOLDER}")
        return
    observer = Observer()
    observer.schedule(AbletonHandler(), WATCH_FOLDER, recursive=True)
    observer.start()
    print(f"[VAULT] Watching: {WATCH_FOLDER}")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with get_db() as conn:
        backups = conn.execute(
            "SELECT * FROM backups ORDER BY backed_up_at DESC LIMIT 50"
        ).fetchall()
        total   = conn.execute("SELECT COUNT(*) FROM backups WHERE status='ok'").fetchone()[0]
        errors  = conn.execute("SELECT COUNT(*) FROM backups WHERE status='error'").fetchone()[0]
    return render_template("index.html",
                           backups=backups,
                           total=total,
                           errors=errors,
                           watch_folder=WATCH_FOLDER)

@app.route("/api/backups")
def api_backups():
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM backups ORDER BY backed_up_at DESC LIMIT 50"
        ).fetchall()]
    return jsonify(rows)


if __name__ == "__main__":
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    port = int(os.environ.get("PORT", 5565))
    app.run(host="0.0.0.0", port=port, debug=False)
