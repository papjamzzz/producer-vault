# Producer Vault — Auto Cloud Backup for Ableton Live

**Continuous versioning for your sessions. Never lose a project again.**

Producer Vault runs in the background and silently backs up your Ableton Live projects to the cloud. Diff-aware — only sends what changed. Instant restore to any version.

---

## Features

- **Auto-detection** — watches your Ableton Projects folder for changes
- **Diff-aware backup** — only uploads changed files, keeps bandwidth low
- **Version history** — every save is a snapshot you can restore
- **Instant restore** — one click to roll back to any previous state
- **Email alerts** — notifies you when backups complete or fail
- **Session health check** — scans for corrupt files, missing samples, bloated projects

## Stack

```
Python · Flask · Watchdog · SQLite · Vanilla JS
```

## Run Locally

```bash
pip install -r requirements.txt
cp .env.example .env
python app.py
# → http://127.0.0.1:5565
```

Or double-click `launch.command` on Mac.

---

*A Creative Konsoles project. Built by a producer, for producers.*
