"""
DAW Doctor — Auto cloud backup for Ableton projects.
Never lose a session again.
Port 5565
"""

import os, sqlite3, threading, time, gzip, json, hashlib, uuid, smtplib, socket, glob, subprocess
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET
import psutil
import xml.etree.ElementTree as ET
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


# ── Diagnostics ───────────────────────────────────────────────────────────────

def _sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def check_cpu():
    out = []
    cpu_pct = psutil.cpu_percent(interval=0.8)
    cores   = psutil.cpu_percent(percpu=True)
    maxed   = sum(1 for c in cores if c > 90)
    if cpu_pct >= 90:
        out.append({"code":"AB-CPU-001","sev":"crit","title":"CPU Overloaded","cause":f"System at {cpu_pct:.0f}%  ·  {maxed}/{len(cores)} cores above 90%","value":f"{cpu_pct:.0f}%","fix":"Increase buffer to 1024+ · Freeze ALL tracks · Kill all other apps NOW"})
    elif cpu_pct >= 72:
        out.append({"code":"AB-CPU-001","sev":"warn","title":"High CPU Load","cause":f"System at {cpu_pct:.0f}%  ·  {maxed}/{len(cores)} cores above 90%","value":f"{cpu_pct:.0f}%","fix":"Freeze instrument tracks · Increase buffer size · Close browser/Slack"})
    else:
        out.append({"code":"AB-CPU-001","sev":"ok","title":"CPU Load Normal","cause":f"System at {cpu_pct:.0f}%","value":f"{cpu_pct:.0f}%","fix":""})
    SKIP = {"kernel_task","WindowServer","Ableton Live","python3","diagnose.py","coreaudiod","launchd","logd","mds"}
    hogs = []
    for p in psutil.process_iter(["name","cpu_percent"]):
        try:
            pct  = p.cpu_percent(interval=None)
            name = (p.info.get("name") or "").strip()
            if pct and pct > 12 and name and name not in SKIP:
                hogs.append((name, pct))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if hogs:
        hogs.sort(key=lambda x: x[1], reverse=True)
        name, pct = hogs[0]
        out.append({"code":"AB-CPU-003","sev":"crit" if pct > 40 else "warn","title":"Background App Eating CPU","cause":f"'{name}' is using {pct:.0f}% CPU","value":f"{name} @ {pct:.0f}%","fix":f"Quit '{name}' before your session for maximum headroom"})
    therm = _sh("pmset -g therm 2>/dev/null | grep CPU_Scheduler_Limit | awk '{print $NF}'")
    if therm and therm.isdigit() and int(therm) < 100:
        out.append({"code":"AB-CPU-002","sev":"crit","title":"CPU Thermal Throttling!","cause":f"macOS capped CPU speed to {therm}% due to heat","value":f"THROTTLED {therm}%","fix":"Let Mac cool down · Use a cooling stand · Check thermal paste on older Macs"})
    return out

def check_memory():
    out = []
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    free  = mem.available / 1024**3
    total = mem.total     / 1024**3
    if free < 1.0:
        out.append({"code":"AB-MEM-001","sev":"crit","title":"Critical Low RAM","cause":f"Only {free:.2f} GB free of {total:.0f} GB","value":f"{free:.2f} GB free","fix":"Freeze ALL tracks · Close every non-essential app · Bounce tracks to audio"})
    elif free < 2.5:
        out.append({"code":"AB-MEM-001","sev":"warn","title":"Low Available RAM","cause":f"{free:.1f} GB free of {total:.0f} GB  ({mem.percent:.0f}% used)","value":f"{free:.1f} GB free","fix":"Freeze sample-heavy instruments · Close Chrome, Slack, Zoom"})
    else:
        out.append({"code":"AB-MEM-001","sev":"ok","title":"RAM OK","cause":f"{free:.1f} GB free of {total:.0f} GB  ({mem.percent:.0f}% used)","value":f"{free:.1f} GB free","fix":""})
    sw_gb = swap.used / 1024**3
    if sw_gb > 1.0:
        out.append({"code":"AB-MEM-002","sev":"crit","title":"Heavy Swap = Audio Dropouts","cause":f"Swapping {sw_gb:.1f} GB to disk","value":f"{sw_gb:.1f} GB swap","fix":"Close all non-essential apps NOW · Freeze & bounce tracks"})
    elif sw_gb > 0.2:
        out.append({"code":"AB-MEM-002","sev":"warn","title":"Swap Activity Detected","cause":f"Using {sw_gb:.2f} GB swap — RAM is tight","value":f"{sw_gb:.2f} GB swap","fix":"Close browser tabs and unused background apps"})
    return out

def check_ableton():
    out = []
    all_procs = list(psutil.process_iter(["name","pid","cpu_percent","memory_info"]))
    abl = next((p for p in all_procs if "Ableton Live" in (p.info.get("name") or "")), None)
    if abl is None:
        out.append({"code":"AB-ABL-001","sev":"info","title":"Ableton Live Not Running","cause":"Ableton Live process not found","value":"NOT RUNNING","fix":"Launch Ableton Live for full process diagnostics"})
    else:
        try:
            cpu = abl.cpu_percent(interval=0.3)
            mb  = abl.memory_info().rss / 1024**2
            out.append({"code":"AB-ABL-000","sev":"ok","title":"Ableton Live Detected","cause":f"PID {abl.pid}  ·  CPU: {cpu:.1f}%  ·  RAM: {mb:.0f} MB","value":f"{cpu:.0f}% CPU","fix":""})
            if cpu > 85:
                out.append({"code":"AB-ABL-002","sev":"crit","title":"Ableton: Critical CPU Usage","cause":f"Ableton alone at {cpu:.1f}% — expect dropouts","value":f"{cpu:.0f}%","fix":"Increase buffer size · Freeze heavy tracks · Disable unused plugins"})
            elif cpu > 65:
                out.append({"code":"AB-ABL-002","sev":"warn","title":"Ableton: High CPU Usage","cause":f"Ableton using {cpu:.1f}% — getting tight","value":f"{cpu:.0f}%","fix":"Consider freezing tracks or bumping buffer size"})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    pref_globs = glob.glob(os.path.expanduser("~/Library/Preferences/Ableton/Live */Preferences.cfg"))
    if pref_globs:
        pref_path = sorted(pref_globs)[-1]
        try:
            tree = ET.parse(pref_path)
            root = tree.getroot()
            def xval(tag_name):
                for el in root.iter():
                    if el.tag == tag_name:
                        return el.get("Value")
                return None
            buf = xval("BufferSize")
            sr  = xval("SampleRate")
            if buf:
                buf_int = int(float(buf))
                if buf_int <= 64:
                    out.append({"code":"AB-ABL-003","sev":"warn","title":"Buffer Size Very Low","cause":f"Set to {buf_int} samples — stresses CPU","value":f"{buf_int} samples","fix":"Increase to 128–256 while producing"})
                elif buf_int >= 1024:
                    out.append({"code":"AB-ABL-003","sev":"info","title":"Buffer Size Large (High Latency)","cause":f"Set to {buf_int} samples — high monitor latency","value":f"{buf_int} samples","fix":"Reduce to 128–256 when recording live instruments"})
                else:
                    out.append({"code":"AB-ABL-003","sev":"ok","title":"Buffer Size OK","cause":f"Set to {buf_int} samples — balanced","value":f"{buf_int} samples","fix":""})
            if sr:
                sr_int = int(float(sr))
                if sr_int <= 48000:
                    out.append({"code":"AB-ABL-004","sev":"ok","title":f"Sample Rate: {sr_int:,} Hz","cause":"Standard rate — minimal CPU overhead","value":f"{sr_int:,} Hz","fix":""})
                else:
                    out.append({"code":"AB-ABL-004","sev":"warn","title":f"Sample Rate High: {sr_int:,} Hz","cause":"High sample rates multiply CPU load","value":f"{sr_int:,} Hz","fix":"Use 44.1 kHz for music unless you need 96k"})
        except Exception:
            pass
    return out

def check_audio():
    out = []
    IFACE_KW = ["focusrite","scarlett","apollo","universal audio","ua ","motu","rme","babyface","fireface","audient","steinberg","presonus","behringer","zoom h","ssl","neve","evo ","volt ","clarett","quantum","arrow","twin","duet","quartet","octet","saffire","tascam","roland ","yamaha ag","id4","id14","id22","id44","mackie","solid state","antelope","lynx","prism","apogee"]
    audio_json = _sh("system_profiler SPAudioDataType -json", timeout=14)
    devices = []
    iface_found = None
    try:
        data = json.loads(audio_json)
        for section in data.get("SPAudioDataType", []):
            for item in section.get("_items", []):
                name = item.get("_name","")
                devices.append(name)
                if iface_found is None and any(kw in name.lower() for kw in IFACE_KW):
                    iface_found = name
    except Exception:
        pass
    if iface_found:
        out.append({"code":"AB-AUD-001","sev":"ok","title":"External Audio Interface Found","cause":f"Detected: {iface_found}","value":iface_found[:28],"fix":""})
    elif devices:
        out.append({"code":"AB-AUD-001","sev":"warn","title":"No External Audio Interface","cause":"Using built-in Mac audio — high latency, no headroom","value":"Built-in Audio","fix":"Connect an interface (Focusrite Scarlett, Apollo Solo, Audient EVO, etc.)"})
    else:
        out.append({"code":"AB-AUD-001","sev":"info","title":"Could Not Query Audio Devices","cause":"system_profiler returned no data","value":"Unknown","fix":"Check System Settings → Sound"})
    ca_ok = any(p.info.get("name") == "coreaudiod" for p in psutil.process_iter(["name"]))
    if not ca_ok:
        out.append({"code":"AB-AUD-002","sev":"crit","title":"CoreAudio Daemon is DOWN","cause":"coreaudiod is not running — audio system is broken","value":"CRASHED","fix":"Restart your Mac to restore CoreAudio"})
    else:
        out.append({"code":"AB-AUD-002","sev":"ok","title":"CoreAudio Running","cause":"coreaudiod is active and healthy","value":"OK","fix":""})
    return out

def check_system():
    out = []
    tm = _sh("tmutil status 2>/dev/null")
    if '"Running" = 1' in tm or '"Stopping" = 1' in tm:
        out.append({"code":"AB-SYS-001","sev":"warn","title":"Time Machine Backup Running","cause":"Actively writing to disk — causes I/O spikes","value":"BACKING UP","fix":"Pause Time Machine: System Settings → General → Time Machine → Pause"})
    else:
        out.append({"code":"AB-SYS-001","sev":"ok","title":"Time Machine Idle","cause":"No backup in progress","value":"IDLE","fix":""})
    sp_procs = ["mdworker","mds_stores","mds "]
    if any(any(sp in (p.info.get("name") or "") for sp in sp_procs) for p in psutil.process_iter(["name"])):
        out.append({"code":"AB-SYS-002","sev":"warn","title":"Spotlight Indexing Active","cause":"mdworker consuming CPU & I/O","value":"INDEXING","fix":"Add Library to Spotlight Privacy exclusions"})
    lpm = _sh("pmset -g 2>/dev/null | grep lowpowermode")
    if lpm.split() and lpm.split()[-1] == "1":
        out.append({"code":"AB-SYS-003","sev":"crit","title":"Low Power Mode ENABLED","cause":"macOS throttles CPU & memory bandwidth","value":"ON","fix":"System Settings → Battery → uncheck Low Power Mode"})
    try:
        batt = psutil.sensors_battery()
        if batt is not None:
            if not batt.power_plugged:
                sev = "crit" if batt.percent < 15 else "warn"
                out.append({"code":"AB-SYS-004","sev":sev,"title":"Running on Battery (Not Plugged In)","cause":f"macOS throttles CPU on battery  ·  {batt.percent:.0f}% remaining","value":f"BAT {batt.percent:.0f}%","fix":"Plug in your power adapter for maximum performance"})
            else:
                out.append({"code":"AB-SYS-004","sev":"ok","title":"AC Power (Plugged In)","cause":"Mac is running on AC — full CPU boost available","value":"AC POWER","fix":""})
    except Exception:
        pass
    bt_state = _sh("defaults read /Library/Preferences/com.apple.Bluetooth ControllerPowerState 2>/dev/null")
    if bt_state == "1":
        bt_audio = _sh("system_profiler SPBluetoothDataType 2>/dev/null | grep -i 'connected: yes' | head -5")
        if bt_audio:
            out.append({"code":"AB-NET-001","sev":"warn","title":"Bluetooth Audio Device Connected","cause":"BT audio adds latency and can cause dropouts","value":"BT AUDIO ON","fix":"Use wired headphones/monitors during recording & mixing"})
        else:
            out.append({"code":"AB-NET-001","sev":"info","title":"Bluetooth Enabled (no audio device)","cause":"BT is on but no audio device connected — low risk","value":"BT ON","fix":"Disable BT if you experience dropouts"})
    disk  = psutil.disk_usage("/")
    free  = disk.free  / 1024**3
    total = disk.total / 1024**3
    if free < 5:
        out.append({"code":"AB-DSK-001","sev":"crit","title":"Critically Low Disk Space","cause":f"Only {free:.1f} GB free of {total:.0f} GB","value":f"{free:.1f} GB free","fix":"Delete large files, empty trash"})
    elif free < 20:
        out.append({"code":"AB-DSK-001","sev":"warn","title":"Low Disk Space","cause":f"{free:.1f} GB free of {total:.0f} GB — sample streaming may stutter","value":f"{free:.1f} GB free","fix":"Free up at least 20 GB for comfortable audio work"})
    else:
        out.append({"code":"AB-DSK-001","sev":"ok","title":"Disk Space OK","cause":f"{free:.1f} GB free of {total:.0f} GB","value":f"{free:.1f} GB","fix":""})
    try:
        io1 = psutil.disk_io_counters()
        time.sleep(0.4)
        io2 = psutil.disk_io_counters()
        if io1 and io2:
            r_mbs = (io2.read_bytes  - io1.read_bytes)  / 0.4 / 1024**2
            w_mbs = (io2.write_bytes - io1.write_bytes) / 0.4 / 1024**2
            tot   = r_mbs + w_mbs
            if tot > 300:
                out.append({"code":"AB-DSK-002","sev":"warn","title":"Heavy Disk Activity","cause":f"R: {r_mbs:.0f} MB/s  ·  W: {w_mbs:.0f} MB/s","value":f"{tot:.0f} MB/s","fix":"Check if Time Machine, Spotlight, or large file copies are running"})
    except Exception:
        pass
    return out


# ── UDP Track Monitor ──────────────────────────────────────────────────────────

_track_data = {"active": False, "tracks": [], "updated_at": 0}

def _udp_listener():
    """Background thread: listen on UDP 7400 for Ableton Remote Script data."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 7400))
        sock.settimeout(2.0)
    except Exception as e:
        print(f"[UDP] Could not bind port 7400: {e}")
        return
    print("[UDP] Track Monitor listening on UDP :7400")
    while True:
        try:
            raw, _ = sock.recvfrom(65535)
            data = json.loads(raw.decode("utf-8", errors="replace"))
            _track_data["active"] = True
            _track_data["tracks"] = data.get("tracks", [])
            _track_data["updated_at"] = time.time()
        except socket.timeout:
            # Mark stale after 5 seconds of no data
            if _track_data["active"] and time.time() - _track_data["updated_at"] > 5:
                _track_data["active"] = False
                _track_data["tracks"] = []
        except Exception:
            pass


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


@app.route("/api/diagnostics", methods=["POST"])
def api_diagnostics():
    results = []
    for fn in [check_ableton, check_cpu, check_memory, check_audio, check_system]:
        results.extend(fn())
    # Sort: crit first, then warn, then info, then ok
    sev_rank = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
    results.sort(key=lambda x: sev_rank.get(x.get("sev","ok"), 4))
    return jsonify(results)


@app.route("/api/track-monitor")
def api_track_monitor():
    data = dict(_track_data)
    data.pop("updated_at", None)
    return jsonify(data)


@app.route("/api/logs")
def api_logs():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, filename, project_name, backed_up_at, status, size_bytes FROM backups ORDER BY backed_up_at DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/settings")
def api_settings():
    return jsonify({
        "watch_folder": WATCH_FOLDER,
        "b2_bucket": os.getenv("B2_BUCKET_NAME", "ProducersVault"),
        "watcher_active": os.path.exists(WATCH_FOLDER)
    })


if __name__ == "__main__":
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    udp_thread = threading.Thread(target=_udp_listener, daemon=True)
    udp_thread.start()
    port = int(os.environ.get("PORT", 5565))
    app.run(host="0.0.0.0", port=port, debug=False)
