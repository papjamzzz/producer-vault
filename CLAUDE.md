# ProducerVault — CLAUDE.md

## What This Is
Auto cloud backup for Ableton projects.
Watches a folder, detects .als/.alc/.adg/.asd changes, uploads to Backblaze B2 instantly.
Dashboard shows backup history, status, watch folder.

## Re-Entry
"Re-entry: producervault"

## Port
5565 (local) / Railway PORT (deployed)

## Stack
- Flask + SQLite
- watchdog (file watcher)
- b2sdk (Backblaze B2 cloud storage)
- Dark purple theme dashboard

## How It Works
1. Watcher thread monitors WATCH_FOLDER recursively
2. On .als/.alc/.adg/.asd change → upload_to_b2()
3. Log result to SQLite (data/vault.db)
4. Dashboard auto-refreshes every 10s

## Key Files
- app.py — Flask + watcher thread + B2 upload
- templates/index.html — Dashboard
- data/vault.db — SQLite backup log (gitignored)

## Env Vars
- B2_KEY_ID
- B2_APPLICATION_KEY
- B2_BUCKET_NAME (default: producervault)
- WATCH_FOLDER (default: ~/Music/Ableton)

## Status
Scaffolded 2026-04-17. Ready to build live on stream.

## Next Steps
- [ ] Create Backblaze B2 account + bucket
- [ ] Test with real Ableton folder
- [ ] Deploy to Railway
- [ ] Submit to Check'd
