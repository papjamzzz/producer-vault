"""
DAW Doctor — Auto cloud backup for Ableton projects.
Never lose a session again.
Port 5565
"""

import os, sqlite3, threading, time, gzip, json, hashlib, uuid, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET
from flask import Flask, render_template, jsonify, send_file, abort, request, Response
from dotenv import load_dotenv
from watchdog.observers.polling import PollingObserver as Observer
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
            ('file_hash',    'TEXT'),
        ]:
            try:
                conn.execute(f"ALTER TABLE backups ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        # License keys table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key          TEXT UNIQUE NOT NULL,
                email                TEXT NOT NULL,
                stripe_customer_id   TEXT,
                stripe_subscription_id TEXT,
                plan                 TEXT DEFAULT 'monthly',
                status               TEXT DEFAULT 'active',
                created_at           TEXT NOT NULL
            )
        """)
        conn.commit()

init_db()


# ── Email ──────────────────────────────────────────────────────────────────────

def send_license_email(to_email, license_key, plan='monthly'):
    """Send license key to customer via Gmail SMTP."""
    gmail_user     = os.getenv('GMAIL_USER', 'jeremiahstephensmith@gmail.com')
    gmail_password = os.getenv('GMAIL_APP_PASSWORD', '')
    from_name      = 'DAW Doctor'
    from_email     = os.getenv('FROM_EMAIL', 'jeremiah@creativekonsoles.com')

    price = '$3/month' if plan == 'monthly' else '$25/year'

    msg = MIMEMultipart('alternative')
    msg['Subject'] = 'Your DAW Doctor license key'
    msg['From']    = f'{from_name} <{from_email}>'
    msg['To']      = to_email

    text = f"""
Welcome to DAW Doctor.

Your license key: {license_key}

Add it to your .env file:
LICENSE_KEY={license_key}

Then restart DAW Doctor and your sessions are protected.

Plan: {price}
Questions? Reply to this email.

— DAW Doctor
"""
    html = f"""
<div style="font-family:Inter,sans-serif;max-width:520px;margin:40px auto;background:#1a1a1a;border-radius:10px;overflow:hidden;">
  <div style="background:#e8760a;padding:24px 32px;">
    <div style="font-size:22px;font-weight:800;color:#000;letter-spacing:-0.5px;">DAW Doctor</div>
    <div style="font-size:13px;color:rgba(0,0,0,0.6);margin-top:4px;">Session backup &amp; health</div>
  </div>
  <div style="padding:32px;">
    <p style="color:#cccccc;font-size:15px;margin-bottom:24px;">Your license key is ready. Paste it into your <code style="background:#2a2a2a;padding:2px 6px;border-radius:3px;color:#f09030;">.env</code> file to activate.</p>
    <div style="background:#2a2a2a;border:1px solid #3a3a3a;border-left:4px solid #e8760a;border-radius:6px;padding:16px 20px;margin-bottom:24px;">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;">LICENSE KEY</div>
      <div style="font-family:monospace;font-size:16px;font-weight:700;color:#eeeeee;letter-spacing:1px;">{license_key}</div>
    </div>
    <p style="color:#888;font-size:13px;line-height:1.7;">Add this line to your <code style="background:#2a2a2a;padding:2px 6px;border-radius:3px;color:#f09030;">.env</code> file:<br><br>
    <code style="background:#2a2a2a;padding:8px 12px;border-radius:4px;color:#cccccc;display:block;margin-top:8px;">LICENSE_KEY={license_key}</code></p>
    <p style="color:#555;font-size:12px;margin-top:32px;">Plan: {price} &nbsp;·&nbsp; Questions? Reply to this email.</p>
  </div>
</div>
"""
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, to_email, msg.as_string())
        print(f"[LICENSE] Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[LICENSE] Email failed: {e}")
        return False


# ── License Helpers ────────────────────────────────────────────────────────────

def create_license(email, stripe_customer_id, stripe_subscription_id, plan='monthly'):
    key = str(uuid.uuid4()).upper().replace('-', '')[:32]
    # Format: DDOC-XXXX-XXXX-XXXX-XXXX
    key = f"DDOC-{key[0:8]}-{key[8:16]}-{key[16:24]}-{key[24:32]}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO licenses (license_key, email, stripe_customer_id, stripe_subscription_id, plan, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, email, stripe_customer_id, stripe_subscription_id, plan, 'active', datetime.utcnow().isoformat())
        )
        conn.commit()
    print(f"[LICENSE] Created {key} for {email}")
    return key

def deactivate_license(stripe_subscription_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE licenses SET status='cancelled' WHERE stripe_subscription_id=?",
            (stripe_subscription_id,)
        )
        conn.commit()
    print(f"[LICENSE] Deactivated subscription {stripe_subscription_id}")

# ── File Hash ─────────────────────────────────────────────────────────────────

def sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def already_backed_up(file_hash):
    """Return True if this exact file content is already in B2."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM backups WHERE file_hash=? AND status='ok' LIMIT 1",
            (file_hash,)
        ).fetchone()
    return row is not None

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

    b2_path   = f"{project_name}/{timestamp}/{filename}"
    file_hash = sha256(path)

    # Audio files: dedup by hash — if we already have this exact content, skip upload
    AUDIO_EXTS = {'.wav', '.aif', '.aiff', '.flac'}
    if ext in AUDIO_EXTS and already_backed_up(file_hash):
        print(f"[VAULT] Skipped (unchanged): {filename}")
        return

    # Parse .als metadata before uploading
    meta = {'bpm': None, 'track_count': None, 'key': None, 'plugins': []}
    if ext == '.als':
        meta = parse_als(path)
        print(f"[VAULT] {filename} — {meta['bpm']} BPM · {meta['track_count']} tracks · {len(meta['plugins'])} plugins")

    print(f"[VAULT] Backing up: {b2_path}")
    success = upload_to_b2(path, b2_path)
    status  = 'ok' if success else 'error'

    with get_db() as conn:
        conn.execute(
            "INSERT INTO backups "
            "(filename, filepath, b2_path, project_name, size_bytes, backed_up_at, status, bpm, track_count, key, plugins, file_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                filename, path, b2_path, project_name, size,
                datetime.utcnow().isoformat(), status,
                meta.get('bpm'),
                meta.get('track_count'),
                meta.get('key'),
                json.dumps(meta.get('plugins', [])),
                file_hash,
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


# ── Stripe Webhook ────────────────────────────────────────────────────────────

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    import stripe
    payload    = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    secret     = os.getenv('STRIPE_WEBHOOK_SECRET', '')

    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
        else:
            event = json.loads(payload)
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return Response(status=400)

    etype = event.get('type', '')
    data  = event['data']['object']

    if etype == 'customer.subscription.created':
        customer_id   = data.get('customer')
        sub_id        = data.get('id')
        plan          = 'annual' if 'annual' in str(data).lower() or 'year' in str(data).lower() else 'monthly'

        # Get customer email from Stripe
        try:
            import stripe as s
            s.api_key      = os.getenv('STRIPE_SECRET_KEY', '')
            customer       = s.Customer.retrieve(customer_id)
            email          = customer.get('email', '')
        except Exception:
            email = data.get('customer_email', '')

        if email:
            key = create_license(email, customer_id, sub_id, plan)
            send_license_email(email, key, plan)

    elif etype in ('customer.subscription.deleted', 'customer.subscription.paused'):
        deactivate_license(data.get('id'))

    return Response(status=200)


# ── License Validation ─────────────────────────────────────────────────────────

@app.route("/validate/<key>")
def validate_license(key):
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, email, plan FROM licenses WHERE license_key=?", (key,)
        ).fetchone()
    if not row:
        return jsonify({'valid': False, 'reason': 'not_found'}), 404
    if row['status'] != 'active':
        return jsonify({'valid': False, 'reason': row['status']}), 403
    return jsonify({'valid': True, 'plan': row['plan'], 'email': row['email']})


# ── Activation Page ────────────────────────────────────────────────────────────

@app.route("/activate")
def activate():
    return render_template("activate.html")


# ── Admin: manual license (for early customers) ────────────────────────────────

@app.route("/admin/issue-license", methods=["POST"])
def admin_issue_license():
    admin_key = request.headers.get('X-Admin-Key', '')
    if admin_key != os.getenv('ADMIN_KEY', ''):
        abort(403)
    data  = request.get_json()
    email = data.get('email', '')
    plan  = data.get('plan', 'monthly')
    if not email:
        abort(400)
    key = create_license(email, 'manual', 'manual', plan)
    send_license_email(email, key, plan)
    return jsonify({'license_key': key, 'email': email})


if __name__ == "__main__":
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    port = int(os.environ.get("PORT", 5565))
    app.run(host="0.0.0.0", port=port, debug=False)
