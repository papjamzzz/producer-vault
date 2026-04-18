"""
DAW Doctor — Auto cloud backup for Ableton projects.
Never lose a session again.
Port 5565
"""

import os, sqlite3, threading, time, gzip, json
from datetime import datetime
from xml.etree import ElementTree as ET
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
WATCH_EXTS   = {'.als', '.alc', '.adg', '.wav', '.aif', '.aiff', '.flac'}
# .asd excluded — Ableton's waveform analysis cache, temp file, not a session asset
DEBOUNCE_SEC = 3.0

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
                status       TEXT DEFAULT 'ok',
                bpm          REAL,
                track_count  INTEGER,
                plugins      TEXT,
                notes        TEXT
            )
        """)
        # Migrate: add any missing columns (safe on fresh or upgraded DBs)
        for col, typedef in [
            ('b2_path',      "TEXT NOT NULL DEFAULT ''"),
            ('project_name', 'TEXT'),
            ('bpm',          'REAL'),
            ('track_count',  'INTEGER'),
            ('key',          'TEXT'),
            ('plugins',      'TEXT'),
            ('notes',        'TEXT'),
        ]:
            try:
                conn.execute(f"ALTER TABLE backups ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        conn.commit()

init_db()

# ── .als Metadata Parser ───────────────────────────────────────────────────────

def parse_als(filepath):
    """
    Parse an Ableton Live Set (.als) file.
    Returns dict: { bpm, track_count, plugins }
    .als files are gzip-compressed XML.
    """
    result = {'bpm': None, 'track_count': None, 'key': None, 'plugins': []}
    try:
        with gzip.open(filepath, 'rb') as f:
            tree = ET.parse(f)
        root = tree.getroot()  # <Ableton>

        live_set = root.find('LiveSet')
        if live_set is None:
            return result

        # ── BPM ──
        # Ableton 11+: MainTrack//Tempo/Manual  |  Older: MasterTrack//Tempo/Manual
        try:
            for track_tag in ('MainTrack', 'MasterTrack'):
                track = live_set.find(track_tag)
                if track is not None:
                    manual = track.find('.//Tempo/Manual')
                    if manual is not None:
                        val = float(manual.get('Value', 0))
                        if val > 0:
                            result['bpm'] = round(val, 1)
                            break
        except Exception:
            pass

        # ── Key / Scale ──
        try:
            scale = live_set.find('ScaleInformation')
            inkey = live_set.find('InKey')
            if scale is not None and inkey is not None and inkey.get('Value') == 'true':
                root_note = int(scale.findtext('RootNote') or
                                scale.find('RootNote').get('Value', '0'))
                scale_name = (scale.findtext('Name') or
                              scale.find('Name').get('Value', ''))
                note_names = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']
                result['key'] = f"{note_names[root_note % 12]} {scale_name}"
        except Exception:
            pass

        # ── Track Count ──
        try:
            tracks = live_set.findall('.//Tracks/AudioTrack') + \
                     live_set.findall('.//Tracks/MidiTrack')
            result['track_count'] = len(tracks)
        except Exception:
            pass

        # ── Plugins ──
        plugins = set()
        try:
            # VST plugins
            for el in live_set.iter('VstPluginInfo'):
                name = el.findtext('PlugName') or el.get('PlugName')
                if name and name.strip():
                    plugins.add(name.strip())
            # AU plugins
            for el in live_set.iter('AuPluginInfo'):
                name = el.findtext('Name') or el.get('Name')
                if name and name.strip():
                    plugins.add(name.strip())
            # VST3
            for el in live_set.iter('Vst3PluginInfo'):
                name = el.findtext('Name') or el.get('Name')
                if name and name.strip():
                    plugins.add(name.strip())
        except Exception:
            pass

        result['plugins'] = sorted(plugins)

    except Exception as e:
        print(f"[PARSE] Could not parse {filepath}: {e}")

    return result


# ── B2 Helpers ────────────────────────────────────────────────────────────────

def get_b2_bucket():
    from b2sdk.v2 import InMemoryAccountInfo, B2Api
    info = InMemoryAccountInfo()
    api  = B2Api(info)
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
        bucket  = get_b2_bucket()
        dl      = bucket.download_file_by_name(b2_path)
        dl.save_to(dest_path)
        return True
    except Exception as e:
        print(f"[B2 DOWNLOAD ERROR] {e}")
        return False


# ── File Watcher with Debounce ─────────────────────────────────────────────────

_pending = {}
_lock    = threading.Lock()

def _do_backup(path):
    with _lock:
        _pending.pop(path, None)

    if not os.path.exists(path):
        return

    ext       = os.path.splitext(path)[1].lower()
    size      = os.path.getsize(path)
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    rel       = os.path.relpath(path, WATCH_FOLDER)
    parts     = rel.split(os.sep)
    filename  = os.path.basename(path)

    # Find the real project name — skip generic Ableton folder names,
    # strip " Project" suffix Ableton appends automatically
    GENERIC = {'projects', 'factory packs', 'live recordings', 'packs-downloaded',
               'plugins-downloaded', 'samples', 'recorded', 'backup'}
    project_name = 'root'
    for part in parts[:-1]:
        if part.lower() not in GENERIC:
            project_name = part.removesuffix(' Project').removesuffix(' project').strip()
            break

    b2_path = f"{project_name}/{timestamp}/{filename}"

    # Parse .als metadata before uploading
    meta = {'bpm': None, 'track_count': None, 'plugins': []}
    if ext == '.als':
        meta = parse_als(path)
        plugin_str = ', '.join(meta['plugins'][:10])  # cap at 10
        print(f"[VAULT] {filename} — {meta['bpm']} BPM · {meta['track_count']} tracks · {len(meta['plugins'])} plugins")

    print(f"[VAULT] Backing up: {b2_path}")
    success = upload_to_b2(path, b2_path)
    status  = 'ok' if success else 'error'

    with get_db() as conn:
        conn.execute(
            "INSERT INTO backups "
            "(filename, filepath, b2_path, project_name, size_bytes, backed_up_at, status, bpm, track_count, key, plugins) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                filename, path, b2_path, project_name, size,
                datetime.utcnow().isoformat(), status,
                meta.get('bpm'),
                meta.get('track_count'),
                meta.get('key'),
                json.dumps(meta.get('plugins', [])),
            )
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

def _format_backup(row):
    d = dict(row)
    # Defensive defaults for rows created before schema migrations
    d.setdefault('project_name', None)
    d.setdefault('b2_path', '')
    d.setdefault('bpm', None)
    d.setdefault('track_count', None)
    d.setdefault('key', None)
    d.setdefault('plugins', None)
    try:
        d['plugins_list'] = json.loads(d.get('plugins') or '[]')
    except Exception:
        d['plugins_list'] = []
    return d


def _compute_diff(current, previous):
    """
    Compare two backup dicts and return a list of human-readable diff strings.
    e.g. ["BPM 120 → 128", "+2 tracks", "+Serum", "-Massive"]
    """
    if not previous:
        return []
    diffs = []

    # BPM change
    try:
        bpm_a = previous.get('bpm')
        bpm_b = current.get('bpm')
        if bpm_a and bpm_b and bpm_a != bpm_b:
            diffs.append(f"BPM {bpm_a} → {bpm_b}")
    except Exception:
        pass

    # Key change
    try:
        key_a = previous.get('key')
        key_b = current.get('key')
        if key_a and key_b and key_a != key_b:
            diffs.append(f"key {key_a} → {key_b}")
        elif not key_a and key_b:
            diffs.append(f"key set: {key_b}")
    except Exception:
        pass

    # Track count change
    try:
        tc_a = previous.get('track_count') or 0
        tc_b = current.get('track_count') or 0
        delta = tc_b - tc_a
        if delta > 0:
            diffs.append(f"+{delta} track{'s' if delta != 1 else ''}")
        elif delta < 0:
            diffs.append(f"{delta} track{'s' if abs(delta) != 1 else ''}")
    except Exception:
        pass

    # Plugin changes
    try:
        plugins_a = set(previous.get('plugins_list') or [])
        plugins_b = set(current.get('plugins_list') or [])
        added   = plugins_b - plugins_a
        removed = plugins_a - plugins_b
        for p in sorted(added)[:3]:
            diffs.append(f"+{p}")
        for p in sorted(removed)[:3]:
            diffs.append(f"−{p}")
    except Exception:
        pass

    return diffs


def _attach_diffs(backups):
    """
    For each backup, compute diff vs the previous version of the same file.
    Mutates in place, adds 'diff' key.
    """
    # Group by (project_name, filename) → ordered list newest-first
    # We need oldest-first to compute forward diffs, then reverse
    from collections import defaultdict
    groups = defaultdict(list)
    for b in backups:
        groups[(b['project_name'], b['filename'])].append(b)

    # Within each group (already newest-first), previous = index+1
    for b_list in groups.values():
        for i, b in enumerate(b_list):
            prev = b_list[i + 1] if i + 1 < len(b_list) else None
            b['diff'] = _compute_diff(b, prev)

    return backups


@app.route("/landing")
def landing():
    return render_template("landing.html")

@app.route("/dashboard")
def dashboard():
    return index_view()

@app.route("/")
def index():
    return index_view()

def index_view():
    with get_db() as conn:
        raw      = conn.execute("SELECT * FROM backups ORDER BY backed_up_at DESC LIMIT 100").fetchall()
        backups  = _attach_diffs([_format_backup(r) for r in raw])
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


@app.route("/project/<name>")
def project_view(name):
    with get_db() as conn:
        raw = conn.execute(
            "SELECT * FROM backups WHERE project_name=? ORDER BY backed_up_at DESC",
            (name,)
        ).fetchall()
        backups = _attach_diffs([_format_backup(r) for r in raw])
        size_sum = conn.execute(
            "SELECT SUM(size_bytes) FROM backups WHERE project_name=? AND status='ok'", (name,)
        ).fetchone()[0] or 0
    return render_template("project.html",
                           name=name,
                           backups=backups,
                           total_size=humanize.naturalsize(size_sum))


@app.route("/api/backups")
def api_backups():
    with get_db() as conn:
        rows = _attach_diffs([_format_backup(r) for r in conn.execute(
            "SELECT * FROM backups ORDER BY backed_up_at DESC LIMIT 100"
        ).fetchall()])
    return jsonify(rows)


@app.route("/api/project/<name>")
def api_project(name):
    with get_db() as conn:
        rows = _attach_diffs([_format_backup(r) for r in conn.execute(
            "SELECT * FROM backups WHERE project_name=? AND status='ok' ORDER BY backed_up_at DESC",
            (name,)
        ).fetchall()])
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
    abort(500)


if __name__ == "__main__":
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    port = int(os.environ.get("PORT", 5565))
    app.run(host="0.0.0.0", port=port, debug=False)
