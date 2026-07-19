# PGR BiliBili CN Launcher

Standalone updater and launcher for Punishing: Gray Raven (BiliBili CN) on Linux.
Replaces Twintail Launcher for this specific game — avoids krdiff patch bugs.

## Requirements

Python 3, plus PySide6 and requests. On externally-managed distros (Arch/Manjaro)
use a virtualenv:

```bash
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

Alternatively install the system packages: `sudo pacman -S pyside6 python-requests`.

## Usage

```bash
.venv/bin/python pgr_launcher.py
```

- **Check for Updates**: Verifies all game files by md5, downloads any missing/mismatched
- **Play**: Launches PGR via Proton
- **Settings**: Configure game directory and runner paths

## How it works

1. Fetches version info from Kurogame's stable CDN config endpoint
2. Downloads the full indexFile (~45k entries with md5 hashes)
3. Verifies every file against the index — no krdiff, no zip blobs
4. Downloads missing/mismatched files directly from the launcher CDN `zip/` path
5. Creates `LocalGameResources.json` + `launcherDownloadConfig.json` so the in-game downloader works
6. Launches via Proton/steamrt with the same command TTL uses

## Why not just use TTL?

TTL v2.3.0 has two bugs that break PGR updates:
- Doesn't extract `0.krzip` blob → ~27 core files left stale
- Doesn't create `LocalGameResources.json` → in-game downloader gets 404s

This launcher avoids both by doing full verification + direct CDN downloads.
