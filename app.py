"""
ProducerVault — Auto cloud backup for Ableton projects.
Never lose a session again.
Port 5565
"""

import os, sqlite3, threading, time
from datetime import datetime
from flask import Flask, render_template, jsonify, send_file, abort
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import humanize
import tempfile

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'), override=True)

app = Flask(__name__)
DB  = os.path.join(os.path.dirname(__file__), 'data', 'vault.db')

WATCH_FOLDER = os.getenv('WATCH_FOLDER', os.path.expanduser('~/Music/Ableton'))
WATCH_EXTS   = {'.als', '.alc', '.adg', '.asd', '.wav', '.aif', '.aiff', '.flac'}
DEBOUNCE_SEC = 3.0   # Ableton fires multiple events per save — wait for settle

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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                filename     TEXT NOT NULL,
                filepath     TEXT NOT NULL,
                b2_path      TEXT NOT NULL,
                project_name TEXT,
                size_bytes   INTEGER,
                backed_up_at TEXT NOT NULL,
                status       TEXT DEFAULT 'ok'
            )
        """)
        conn.commit()

init_db()

# ── B2 Helpers ────────────────────────────────────────────────────────────────

def get_b2_bucket():
    from b2sdk.v2 import InMemoryAccountInfo, B2Api
    info   = InMemoryAccountInfo()
    api    = B2Api(info)
    api.authorize_account("production",
                           os.getenv("B2_KEY_ID"),
                           os.getenv("B2_APPLICATION_KEY"))
    return api.get_bucket_by_name(os.getenv("B2_BUCKET_NAME", "ProducersVault"))

def upload_to_b2(filepath, b2_path):
    try:
        bucket = get_b2_bucket()
        bucket.upload_local_file(local_file=filepath, file_name=b2_path)
        return True
    except Exception as e:
        print(f"[B2 ERROR] {e}")
        return False

def download_from_b2(b2_path, dest_path):
    try:
        bucket = get_b2_bucket()
        downloaded = bucket.download_file_by_name(b2_path)
        downloaded.save_to(dest_path)
        return True
    except Exception as e:
        print(f"[B2 DOWNLOAD ERROR] {e}")
        return False

def b2_total_size():
    """Return total bytes stored across all files in bucket."""
    try:
        bucket = get_b2_bucket()
        total = sum(f.size for f, _ in bucket.ls(show_versions=False))
        return total
    except Exception:
        return 0

# ── File Watcher with Debounce ─────────────────────────────────────────────────

_pending   = {}   # path -> timer
_lock      = threading.Lock()

def _do_backup(path):
    """Actually run the backup — called after debounce settles."""
    with _lock:
        _pending.pop(path, None)

    if not os.path.exists(path):
        return

    ext          = os.path.splitext(path)[1].lower()
    size         = os.path.getsize(path)
    timestamp    = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")

    # Build a human project name from the nearest parent folder
    rel          = os.path.relpath(path, WATCH_FOLDER)
    parts        = rel.split(os.sep)
    project_name = parts[0] if len(parts) > 1 else "root"
    filename     = os.path.basename(path)
    b2_path      = f"{project_name}/{timestamp}/{filename}"

    print(f"[VAULT] Backing up: {b2_path}")
    success = upload_to_b2(path, b2_path)
    status  = 'ok' if success else 'error'

    with get_db() as conn:
        conn.execute(
            "INSERT INTO backups (filename, filepath, b2_path, project_name, size_bytes, backed_up_at, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (filename, path, b2_path, project_name, size, datetime.utcnow().isoformat(), status)
        )
        conn.commit()

    print(f"[VAULT] {'✓' if success else '✗'} {filename} ({humanize.naturalsize(size)})")


class AbletonHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def _schedule(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext not in WATCH_EXTS:
            return
        with _lock:
            # Cancel any existing timer for this file
            if path in _pending:
                _pending[path].cancel()
            t = threading.Timer(DEBOUNCE_SEC, _do_backup, args=[path])
            _pending[path] = t
            t.start()


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
        backups  = conn.execute(
            "SELECT * FROM backups ORDER BY backed_up_at DESC LIMIT 100"
        ).fetchall()
        total    = conn.execute("SELECT COUNT(*) FROM backups WHERE status='ok'").fetchone()[0]
        errors   = conn.execute("SELECT COUNT(*) FROM backups WHERE status='error'").fetchone()[0]
        projects = conn.execute(
            "SELECT project_name, COUNT(*) as cnt, MAX(backed_up_at) as last_backup "
            "FROM backups WHERE status='ok' GROUP BY project_name ORDER BY last_backup DESC"
        ).fetchall()
        size_sum = conn.execute("SELECT SUM(size_bytes) FROM backups WHERE status='ok'").fetchone()[0] or 0

    return render_template("index.html",
                           backups=backups,
                           total=total,
                           errors=errors,
                           projects=projects,
                           total_size=humanize.naturalsize(size_sum),
                           watch_folder=WATCH_FOLDER)

@app.route("/api/backups")
def api_backups():
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM backups ORDER BY backed_up_at DESC LIMIT 100"
        ).fetchall()]
    return jsonify(rows)

@app.route("/restore/<int:backup_id>")
def restore(backup_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
    if not row:
        abort(404)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(row['filename'])[1])
    tmp.close()

    if download_from_b2(row['b2_path'], tmp.name):
        return send_file(tmp.name, as_attachment=True, download_name=row['filename'])
    else:
        abort(500)


if __name__ == "__main__":
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    port = int(os.environ.get("PORT", 5565))
    app.run(host="0.0.0.0", port=port, debug=False)
