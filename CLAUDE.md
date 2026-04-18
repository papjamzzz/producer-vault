# DAW Doctor — CLAUDE.md

## What This Is
Auto cloud backup for Ableton projects. Watches a folder, detects .als/.wav/audio changes, uploads to Backblaze B2 instantly. Parses .als files for BPM, key, track count, plugins. Full version history with one-click restore.

**This is the merged home for: ProducerVault + DAW Doctor (diagnostics) + TrackTracks (monitor)**
Diagnostics and Track Monitor are stubs in the sidebar — next to build.

## Re-Entry
"Re-entry: dawdoctor" or "re-entry: producervault"

## Port
5565 (local) / Railway PORT (deployed)

## Stack
- Flask + SQLite
- watchdog (file watcher, 3s debounce)
- b2sdk (Backblaze B2)
- gzip + ElementTree (.als parser)
- Dark Ableton-palette dashboard

## How It Works
1. Watcher thread monitors WATCH_FOLDER recursively
2. On .als/.wav/audio change → debounce 3s → upload_to_b2()
3. Parse .als: BPM (MainTrack + MasterTrack paths), key (ScaleInformation), tracks, plugins (VST/AU/VST3)
4. Log to SQLite (data/vault.db) with all metadata
5. Dashboard auto-refreshes every 10s, computes version diffs

## Env Vars
- B2_KEY_ID
- B2_APPLICATION_KEY
- B2_BUCKET_NAME=ProducersVault
- WATCH_FOLDER=~/Music/Ableton

## Routes
- / + /dashboard → main table dashboard
- /project/<name> → version timeline for one project
- /restore/<id> → download backup from B2
- /landing → marketing page
- /api/backups, /api/project/<name> → JSON

## UI
- Kode Keeper layout: top bar + sidebar (VAULT/TOOLING/SYSTEM) + stats row + table
- Top bar: live clock, watcher LED, status text
- Sidebar: Dashboard, Projects (active); Diagnostics, Track Monitor, Logs (coming soon)
- Table: FILE | PROJECT | BPM | KEY | TRACKS | SAVED | SIZE | ACTIONS
- Version diff: auto-computed between consecutive saves of same file
- Onboarding: 3-step modal, localStorage flag dd_onboarded

## Status
Renamed to DAW Doctor 2026-04-18. Core backup + versioning live. UI redesigned.

## What's Next
- [ ] Stripe payment links ($3/mo + $25/yr) on landing page
- [ ] Deploy to Railway
- [ ] Diagnostics tab (port DAW Doctor CLI scans to web)
- [ ] Track Monitor tab (port TrackTracks UDP data to web)
- [ ] Submit to Ableton community / Reddit

## GitHub
https://github.com/papjamzzz/producer-vault (repo name stays, product name is DAW Doctor)

---
Last updated: 2026-04-18
