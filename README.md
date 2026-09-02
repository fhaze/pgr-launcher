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

- **Check for Updates**: Applies the best CDN plan, then verifies and repairs game files
- **Play**: Launches PGR via Proton
- **Settings**: Configure game directory and runner paths

## How it works

1. Fetches version info and the full file index from Kurogame's stable CDN config endpoint
2. Selects the matching `patchConfig` for updates or `zipConfig` for clean installs
3. Downloads package objects with connection reuse, retries, and HTTP range resumption
4. Safely extracts `.krzip` bundles, overlays newer direct files, then removes obsolete files
5. Verifies the result against the full index and directly repairs anything still missing or mismatched
6. Creates `LocalGameResources.json` + `launcherDownloadConfig.json` so the in-game downloader works
7. Launches via Proton/steamrt with the same command TTL uses

A clean install currently uses 4,763 CDN objects instead of downloading all ~46k game files individually. If package metadata is unavailable or uses an unsupported binary-diff format, the launcher falls back to full direct-file repair.

## Why not just use TTL?

TTL v2.3.0 did not reliably apply PGR's package metadata and did not create `LocalGameResources.json`, which could leave stale core files and make the in-game downloader request invalid URLs. This launcher validates the archive/direct-file overlay order against the full manifest and only records the new version after the resulting installation passes verification.
