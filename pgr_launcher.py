#!/usr/bin/env python3
"""PGR BiliBili CN Updater + Launcher.

Standalone updater and launcher for Punishing: Gray Raven (BiliBili CN) on
Linux. Fetches version info from Kurogame's stable CDN config endpoint, verifies
every game file by md5 against the indexFile, downloads any missing/mismatched
files directly from the launcher CDN, writes the game config files the in-game
downloader needs, and launches the game through Proton/steamrt.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

import requests
from PySide6.QtCore import Qt, QLockFile, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ─── Constants ───────────────────────────────────────────────────────

APP_ID = "10011"
GAME_ID = "G148"
APP_KEY = "qYQv6TyyyhCKD3ox3gssyolNPwMoCPZt"
CONFIG_URL = (
    f"https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/"
    f"{GAME_ID}/{APP_ID}_{APP_KEY}/index.json"
)
GAME_NAME = "Punishing: Gray Raven (BiliBili CN)"
GAME_EXE = "PGR.exe"
CONFIG_PATH = Path(__file__).parent / "config.json"
USER_AGENT = "PGR-Launcher/1.0"

# Launcher UI config (banner/background assets).
LAUNCHER_INDEX_URL = (
    f"https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/launcher/"
    f"{APP_ID}_{APP_KEY}/{GAME_ID}/index.json"
)
LAUNCHER_BG_TEMPLATE = (
    f"https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/"
    f"{APP_ID}_{APP_KEY}/{GAME_ID}/background/{{slug}}/{{locale}}.json"
)
BANNER_LOCALES = ("zh-Hans", "zh-CN", "zh", "en")
BANNER_CACHE_DIR = Path(__file__).parent / "assets" / "cache"

# Files at or above this size skip md5 verification during the check phase to
# keep verification fast; a size match is trusted for them.
MD5_SKIP_SIZE = 100_000_000

# Per-file retries for transient CDN errors (timeouts, stalls, dropped or
# truncated connections). Attempts are spaced by a linear backoff.
MAX_DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# ─── Config ─────────────────────────────────────────────────────────


def default_config() -> dict:
    home = Path.home()
    ttl = home / ".local/share/twintaillauncher"
    return {
        "game_dir": str(ttl / "games/pgr_bilibili/tirc9x0gjip6tevnw31ju2df"),
        "proton_path": str(
            ttl / "compatibility/runners/11.0-20260521-proton-cachyos/proton"
        ),
        "prefix_path": str(
            ttl / "compatibility/prefixes/pgr_bilibili/tirc9x0gjip6tevnw31ju2df"
        ),
        "steamrt_path": str(
            ttl / "compatibility/runners/steamrt/steamrt4/_v2-entry-point"
        ),
        "reaper_path": "/usr/lib/twintaillauncher/resources/reaper",
        "graphics_api": "-force-d3d11",
        "cdn_host": "https://zspms-alicdn-gamestarter.kurogame.com",
        "last_checked_version": None,
        "installed_version": None,
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # Backfill any keys added in newer versions.
        merged = default_config()
        merged.update(cfg)
        return merged
    return default_config()


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def detect_installed_version(game_dir) -> str | None:
    """Read the installed version from the game directory, if present.

    Prefers launcherDownloadConfig.json (proper JSON, written by the launcher);
    falls back to the game's own version.json, which uses a non-standard format
    like ``{package_version:4.6.0}``.
    """
    game_dir = Path(game_dir)

    dl = game_dir / "launcherDownloadConfig.json"
    if dl.exists():
        try:
            with open(dl) as f:
                version = json.load(f).get("version")
            if version:
                return version
        except (OSError, json.JSONDecodeError):
            pass

    vj = game_dir / "version.json"
    if vj.exists():
        try:
            match = re.search(r"package_version\s*:\s*([0-9][0-9.]*)", vj.read_text())
            if match:
                return match.group(1)
        except OSError:
            pass

    return None


# ─── CDN API client ─────────────────────────────────────────────────


def fetch_cdn_config() -> dict:
    """Fetch the stable config endpoint. Returns the full JSON document."""
    resp = requests.get(CONFIG_URL, timeout=15, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


def parse_version_info(cdn_config: dict) -> dict:
    """Extract version, hash, CDN hosts, and indexFile URL from the config."""
    default = cdn_config["default"]
    cdn_hosts = [c["url"] for c in default["cdnList"]]
    cfg = default["config"]
    version = cfg["version"]
    # baseUrl is "launcher/game/<gameId>/<appId>/<version>/<HASH>/zip/", so the
    # hash is the segment right after the version (index 5, not 4).
    base_parts = cfg["baseUrl"].strip("/").split("/")
    version_hash = base_parts[5]
    return {
        "version": version,
        "hash": version_hash,
        "cdn_hosts": cdn_hosts,
        "index_file_path": cfg["indexFile"],
        "full_size": cfg["size"],
    }


def fetch_index_file(version_info: dict) -> list:
    """Fetch the full indexFile and return the resource list."""
    host = version_info["cdn_hosts"][0]  # alicdn, highest priority
    url = host + version_info["index_file_path"]
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()["resource"]


def get_download_url(version_info: dict, dest: str) -> str:
    """Construct the download URL for a game file."""
    host = version_info["cdn_hosts"][0]
    ver = version_info["version"]
    h = version_info["hash"]
    return f"{host}launcher/game/{GAME_ID}/{APP_ID}/{ver}/{h}/zip/{dest}"


class ByteCounter:
    """Thread-safe byte counter the UI can poll to display download speed.

    ``count`` is a plain int attribute: writes go through the lock so the 8
    download workers never lose an increment, while reads from the GUI thread
    are a single atomic attribute load (safe under the GIL).
    """

    def __init__(self):
        self._lock = Lock()
        self.count = 0

    def add(self, n: int):
        with self._lock:
            self.count += n


def format_speed(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024**3:
        return f"{bytes_per_sec / 1024**3:.2f} GB/s"
    if bytes_per_sec >= 1024**2:
        return f"{bytes_per_sec / 1024**2:.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def download_file(
    url: str,
    expected_md5: str,
    local_path: Path,
    cancel_check=None,
    on_retry=None,
    byte_counter=None,
) -> str | None:
    """Download *url* to *local_path* atomically, retrying transient errors.

    Streams to a ``.tmp`` sibling and md5-hashes incrementally, so multi-GB
    game files are never buffered in RAM (8 parallel workers each holding a
    full file would exhaust memory). Retries timeouts, dropped/truncated
    connections, and md5 mismatches (a truncated body can also close cleanly
    and only surface as a hash mismatch). The local file is only replaced
    after the md5 check passes, so a failed attempt never corrupts the
    existing file.

    *cancel_check* (optional) is polled before each attempt so a cancelled
    update run stops retrying promptly. *on_retry* (optional) is called as
    ``on_retry(failed_attempt, error)`` before each backoff sleep.
    *byte_counter* (optional) is fed each chunk's length so the UI can
    display aggregate download speed.

    Returns None on success, or an error string.
    """
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        if cancel_check and cancel_check():
            tmp_path.unlink(missing_ok=True)
            return "cancelled"
        try:
            md5 = hashlib.md5()
            with requests.get(
                url,
                timeout=60,
                stream=True,
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        md5.update(chunk)
                        if byte_counter is not None:
                            byte_counter.add(len(chunk))
            if md5.hexdigest() != expected_md5:
                raise RuntimeError("md5 mismatch (truncated or corrupt download)")
            tmp_path.replace(local_path)
            return None
        except Exception as e:  # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            if attempt >= MAX_DOWNLOAD_RETRIES or (cancel_check and cancel_check()):
                return str(e)
            if on_retry:
                on_retry(attempt, str(e))
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return "unreachable"  # pragma: no cover


def fetch_banner_url() -> str:
    """Fetch the official launcher banner image URL for the current patch.

    Follows the same 3-step chain the official Kuro launcher uses:
      1. GET launcher index -> extracts functionCode.background slug
      2. GET per-locale background descriptor -> extracts firstFrameImage URL
    Tries each locale in BANNER_LOCALES until one returns 200.
    Returns the static banner (webp) URL.
    """
    resp = requests.get(
        LAUNCHER_INDEX_URL, timeout=15, headers={"User-Agent": USER_AGENT}
    )
    resp.raise_for_status()
    bg_slug = resp.json()["functionCode"]["background"]

    for locale in BANNER_LOCALES:
        url = LAUNCHER_BG_TEMPLATE.format(slug=bg_slug, locale=locale)
        r = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            data = r.json()
            return data.get("firstFrameImage", "")

    raise RuntimeError("Could not fetch banner URL (all locales failed)")


def download_banner(url: str, version: str) -> Path:
    """Download the banner image and cache it locally. Returns the local path."""
    BANNER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local_path = BANNER_CACHE_DIR / f"banner_{version}.webp"
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    return local_path


def get_cached_banner_path(preferred_version: str | None = None) -> Path | None:
    """Find the best cached banner path without any network calls.

    Tries, in order:
      1. The banner matching *preferred_version* (usually the locally installed version).
      2. The most recently modified banner_*.webp file in the cache directory.
    Returns None if no usable cached banner exists.
    """
    if not BANNER_CACHE_DIR.is_dir():
        return None

    if preferred_version:
        p = BANNER_CACHE_DIR / f"banner_{preferred_version}.webp"
        if p.exists() and p.stat().st_size > 0:
            return p

    candidates = [
        p
        for p in BANNER_CACHE_DIR.glob("banner_*.webp")
        if p.is_file() and p.stat().st_size > 0
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# ─── Update worker ──────────────────────────────────────────────────


class UpdateWorker(QThread):
    progress = Signal(int, int, str)  # current, total, message
    log = Signal(str)
    finished_signal = Signal(bool, str)  # success, message

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self._cancel = False
        # Created here (not in _do_update) so the GUI's speed sampler can
        # safely read it even before the download phase starts.
        self.byte_counter = ByteCounter()

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._do_update()
        except Exception as e:  # noqa: BLE001
            self.finished_signal.emit(False, f"Error: {e}")

    def _do_update(self):
        game_dir = Path(self.config["game_dir"])

        # 1. Fetch CDN config.
        self.log.emit("Fetching CDN config...")
        cdn_config = fetch_cdn_config()
        info = parse_version_info(cdn_config)
        self.log.emit(f"Latest version: {info['version']}")

        # 2. Fetch indexFile.
        self.log.emit("Fetching file index...")
        resources = fetch_index_file(info)
        self.log.emit(f"Total files in index: {len(resources)}")

        # 3. Verify all files by size + md5.
        self.log.emit("Verifying files...")
        to_download = []
        total = len(resources)
        for i, r in enumerate(resources):
            if self._cancel:
                self.finished_signal.emit(False, "Cancelled")
                return
            local_path = game_dir / r["dest"]
            if not local_path.exists() or local_path.stat().st_size != r["size"]:
                to_download.append(r)
            elif r["size"] < MD5_SKIP_SIZE:
                with open(local_path, "rb") as f:
                    actual_md5 = hashlib.md5(f.read()).hexdigest()
                if actual_md5 != r["md5"]:
                    to_download.append(r)

            if (i + 1) % 500 == 0 or i == total - 1:
                self.progress.emit(i + 1, total, f"Verifying... {i + 1}/{total}")

        if not to_download:
            self.log.emit("All files verified. No update needed.")
            self._create_game_configs(info, resources)
            self.config["installed_version"] = info["version"]
            self.config["last_checked_version"] = info["version"]
            save_config(self.config)
            self.finished_signal.emit(True, f"Ready — version {info['version']}")
            return

        total_dl_size = sum(r["size"] for r in to_download)
        self.log.emit(
            f"Need to download {len(to_download)} files "
            f"({total_dl_size / 1024 / 1024:.0f} MB)"
        )

        # 4. Download missing/mismatched files in parallel.
        total_dl = len(to_download)
        downloaded = 0
        failed = 0

        def download_one(r):
            dest = r["dest"]
            local_path = game_dir / dest
            local_path.parent.mkdir(parents=True, exist_ok=True)
            err = download_file(
                get_download_url(info, dest),
                r["md5"],
                local_path,
                cancel_check=lambda: self._cancel,
                byte_counter=self.byte_counter,
                on_retry=lambda attempt, e: self.log.emit(
                    f"Retrying {dest} ({attempt + 1}/{MAX_DOWNLOAD_RETRIES}): {e}"
                ),
            )
            return (dest, err is None, err)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(download_one, r): r for r in to_download}
            for future in as_completed(futures):
                if self._cancel:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.finished_signal.emit(False, "Cancelled")
                    return
                dest, ok, err = future.result()
                if ok:
                    downloaded += 1
                else:
                    failed += 1
                    self.log.emit(f"Failed: {dest}: {err}")
                done = downloaded + failed
                if done % 50 == 0 or done == total_dl:
                    self.progress.emit(
                        done, total_dl, f"Downloading... {done}/{total_dl}"
                    )

        self.log.emit(f"Downloaded {downloaded}/{total_dl} ({failed} failed)")

        if failed:
            # Do NOT write the game configs or record the version on a partial
            # download: detect_installed_version() reads
            # launcherDownloadConfig.json, so recording the new version would
            # make the next startup report "up to date" with files still
            # missing. Re-running the update re-verifies everything and
            # downloads only the files that are still wrong.
            self.finished_signal.emit(
                False,
                f"Update incomplete: {failed} of {total_dl} files failed after "
                f"{MAX_DOWNLOAD_RETRIES} attempts. Run the update again to "
                f"retry them.",
            )
            return

        # 5. Create game config files.
        self._create_game_configs(info, resources)

        # 6. Persist installed version.
        self.config["installed_version"] = info["version"]
        self.config["last_checked_version"] = info["version"]
        save_config(self.config)

        self.finished_signal.emit(True, f"Updated to {info['version']}")

    def _create_game_configs(self, version_info: dict, resources: list):
        """Create LocalGameResources.json and launcherDownloadConfig.json."""
        game_dir = Path(self.config["game_dir"])
        host = version_info["cdn_hosts"][0]
        ver = version_info["version"]
        h = version_info["hash"]
        from_folder = f"{host}launcher/game/{GAME_ID}/{APP_ID}/{ver}/{h}/zip/"

        # launcherDownloadConfig.json
        dl_config = {
            "version": ver,
            "reUseVersion": "",
            "state": "",
            "isPreDownload": False,
            "appId": APP_ID,
        }
        with open(game_dir / "launcherDownloadConfig.json", "w") as f:
            json.dump(dl_config, f, indent=4)
        self.log.emit("Created launcherDownloadConfig.json")

        # LocalGameResources.json — only streaming-asset resource entries, each
        # pointed at the full CDN URL (the game's built-in XUrlPerfixConfig
        # points at wrong hosts).
        streaming = [
            r
            for r in resources
            if r["dest"].startswith("PGR_Data/StreamingAssets/resource/")
        ]
        local_res = {
            "resource": [
                {
                    "dest": r["dest"],
                    "size": r["size"],
                    "md5": r["md5"],
                    "fromFolder": from_folder,
                    "chunkInfos": r.get("chunkInfos"),
                }
                for r in streaming
            ]
        }
        with open(game_dir / "LocalGameResources.json", "w") as f:
            json.dump(local_res, f, indent=4)
        self.log.emit(f"Created LocalGameResources.json ({len(streaming)} entries)")


# ─── Game launcher ──────────────────────────────────────────────────


def launch_game(config: dict) -> subprocess.Popen:
    """Launch PGR via Proton/steamrt."""
    game_dir = Path(config["game_dir"])
    steamrt = config["steamrt_path"]
    reaper = config["reaper_path"]
    proton = config["proton_path"]
    prefix = config["prefix_path"]
    graphics = config.get("graphics_api", "-force-d3d11")

    # Convert the game exe path to Windows z: drive format.
    exe_path = game_dir / GAME_EXE
    exe_z = f"z:{exe_path}".replace("/", "\\")

    env = os.environ.copy()
    env["WINEPREFIX"] = prefix
    env["STEAM_COMPAT_DATA_PATH"] = prefix
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = prefix
    env["STEAM_COMPAT_APP_ID"] = "0"
    env["STEAM_COMPAT_INSTALL_PATH"] = str(game_dir)
    env["SteamAppId"] = "0"
    env["SteamGameId"] = "0"

    # reaper runs on the host and wraps the steamrt entry point, which enters
    # the pressure-vessel container and runs Proton. reaper must be outermost:
    # it lives under /usr, which the container refuses to bind-mount, so it can
    # never be executed from inside the container.
    cmd = [
        reaper,
        "SteamLaunch",
        "AppId=0",
        "--",
        steamrt,
        "--verb=waitforexitandrun",
        "--",
        proton,
        "waitforexitandrun",
        exe_z,
        graphics,
    ]

    return subprocess.Popen(cmd, env=env, cwd=str(game_dir))


# ─── Settings dialog ────────────────────────────────────────────────


class SettingsDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.config = config.copy()
        self._build_ui()

    def _path_row(self, layout, label, value, title, directory=False):
        line = QLineEdit(value)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._browse(line, title, directory=directory))
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(line)
        row.addWidget(browse)
        layout.addRow(label, holder)
        return line

    def _build_ui(self):
        layout = QFormLayout(self)

        self.game_dir = self._path_row(
            layout,
            "Game Directory:",
            self.config["game_dir"],
            "Select Game Directory",
            directory=True,
        )
        self.proton_path = self._path_row(
            layout,
            "Proton Path:",
            self.config["proton_path"],
            "Select Proton Binary",
        )
        self.prefix_path = self._path_row(
            layout,
            "Wine Prefix:",
            self.config["prefix_path"],
            "Select Wine Prefix",
            directory=True,
        )
        self.steamrt_path = self._path_row(
            layout,
            "steamrt Entry Point:",
            self.config["steamrt_path"],
            "Select steamrt Entry Point",
        )
        self.reaper_path = self._path_row(
            layout,
            "Reaper Path:",
            self.config["reaper_path"],
            "Select Reaper Binary",
        )

        self.graphics_api = QComboBox()
        self.graphics_api.addItem("DirectX 11", "-force-d3d11")
        self.graphics_api.addItem("DirectX 12", "-force-d3d12")
        idx = self.graphics_api.findData(
            self.config.get("graphics_api", "-force-d3d11")
        )
        self.graphics_api.setCurrentIndex(max(idx, 0))
        layout.addRow("Graphics API:", self.graphics_api)

        buttons = QHBoxLayout()
        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addRow(buttons)

    def _browse(self, line_edit: QLineEdit, title: str, directory: bool = False):
        if directory:
            path = QFileDialog.getExistingDirectory(self, title)
        else:
            path, _ = QFileDialog.getOpenFileName(self, title)
        if path:
            line_edit.setText(path)

    def get_config(self) -> dict:
        self.config["game_dir"] = self.game_dir.text()
        self.config["proton_path"] = self.proton_path.text()
        self.config["prefix_path"] = self.prefix_path.text()
        self.config["steamrt_path"] = self.steamrt_path.text()
        self.config["reaper_path"] = self.reaper_path.text()
        self.config["graphics_api"] = self.graphics_api.currentData()
        return self.config


# ─── Main window ────────────────────────────────────────────────────


DARK_STYLESHEET = """
QMainWindow {
    background-color: #0f0f1a;
    color: #e0e0e0;
    font-size: 13px;
}
QLabel {
    color: #d0d0d0;
    background: transparent;
}
QPushButton {
    background-color: rgba(42, 42, 74, 220);
    color: #e0e0e0;
    border: 1px solid #3d3d66;
    border-radius: 4px;
    padding: 8px 18px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: rgba(61, 61, 102, 230);
    border-color: #6c5ce7;
}
QPushButton:pressed {
    background-color: rgba(30, 30, 58, 230);
}
QPushButton:disabled {
    background-color: rgba(26, 26, 42, 220);
    color: #666;
    border-color: #2a2a3a;
}
QProgressBar {
    border: 1px solid #3d3d66;
    border-radius: 4px;
    text-align: center;
    background-color: rgba(26, 26, 46, 220);
    color: #e0e0e0;
}
QProgressBar::chunk {
    background-color: #6c5ce7;
    border-radius: 3px;
}
QTextEdit {
    background-color: rgba(17, 17, 31, 220);
    color: #b0b0b0;
    border: 1px solid #2a2a4a;
    border-radius: 4px;
    font-family: "Consolas", "Monaco", monospace;
    font-size: 12px;
}
QComboBox, QLineEdit {
    background-color: rgba(26, 26, 46, 230);
    color: #e0e0e0;
    border: 1px solid #3d3d66;
    border-radius: 3px;
    padding: 4px;
}
QDialog {
    background-color: #0f0f1a;
}
"""


class BackgroundWidget(QWidget):
    """Widget that paints a banner image as its background (cover mode)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._banner_pixmap = None
        self._scaled_banner = None

    def set_banner(self, path: str):
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        self._banner_pixmap = pixmap
        self._scaled_banner = None
        self.update()

    def _get_scaled_banner(self):
        if self._banner_pixmap is None:
            return None
        size = self.size()
        if self._scaled_banner is not None and self._scaled_banner[0] == size:
            return self._scaled_banner[1]
        scaled = self._banner_pixmap.scaled(
            size,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self._scaled_banner = (size, scaled)
        return scaled

    def paintEvent(self, event):
        banner = self._get_scaled_banner()
        painter = QPainter(self)

        if banner is not None:
            x = (self.width() - banner.width()) // 2
            y = (self.height() - banner.height()) // 2
            painter.drawPixmap(x, y, banner)
        else:
            super().paintEvent(event)

        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(15, 15, 26, 0))
        gradient.setColorAt(0.45, QColor(15, 15, 26, 40))
        gradient.setColorAt(0.75, QColor(15, 15, 26, 180))
        gradient.setColorAt(1.0, QColor(15, 15, 26, 220))
        painter.fillRect(self.rect(), gradient)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scaled_banner = None
        self.update()


class MainWindow(QMainWindow):
    banner_loaded = Signal(str)  # emits local path to cached banner image

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.version_info = None
        self.setWindowTitle("PGR BiliBili CN Launcher")
        self.setMinimumSize(960, 570)
        self.resize(1280, 760)
        self.setStyleSheet(DARK_STYLESHEET)
        self._build_ui()
        self.banner_loaded.connect(self._apply_banner)

        # Show the best cached banner immediately so the UI is not blank while
        # the network version check runs in the background. The cached image
        # will be replaced later if a newer banner is available.
        installed_version = detect_installed_version(
            self.config["game_dir"]
        ) or self.config.get("installed_version")
        cached_banner = get_cached_banner_path(installed_version)
        if cached_banner is not None:
            self.bg_widget.set_banner(str(cached_banner))

        # Quick version check + banner load on startup.
        QTimer.singleShot(500, self._quick_version_check)

    def _build_ui(self):
        self.bg_widget = BackgroundWidget()
        self.setCentralWidget(self.bg_widget)

        layout = QVBoxLayout(self.bg_widget)
        layout.setContentsMargins(48, 24, 48, 48)
        layout.setSpacing(12)

        layout.addStretch(10)

        title = QLabel(GAME_NAME)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #a29bfe;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.version_label = QLabel("Checking version...")
        self.version_label.setAlignment(Qt.AlignCenter)
        self.version_label.setStyleSheet("color: #c0c0c0; font-size: 13px;")
        layout.addWidget(self.version_label)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedHeight(22)
        layout.addWidget(self.progress)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #d0d0d0;")
        self.speed_label = QLabel("")
        self.speed_label.setStyleSheet("color: #a0a0a0;")
        self.speed_label.setVisible(False)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        status_row.addWidget(self.speed_label)
        layout.addLayout(status_row)

        btn_layout = QHBoxLayout()
        self.btn_update = QPushButton("Check for Updates")
        self.btn_play = QPushButton("Play")
        self.btn_settings = QPushButton("Settings")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        btn_layout.addWidget(self.btn_update)
        btn_layout.addWidget(self.btn_play)
        btn_layout.addWidget(self.btn_settings)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(160)
        layout.addWidget(self.log)

        self.btn_update.clicked.connect(self._on_update)
        self.btn_play.clicked.connect(self._on_play)
        self.btn_settings.clicked.connect(self._on_settings)
        self.btn_cancel.clicked.connect(self._on_cancel)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    # ─── Banner background ──────────────────────────────────────────

    def _apply_banner(self, path: str):
        """Load the banner image and set it as the central widget background."""
        self.bg_widget.set_banner(path)

    # ─── Startup version check ──────────────────────────────────────

    def _quick_version_check(self):
        """Fetch the CDN version without verifying files (runs off-thread)."""
        self.status_label.setText("Checking for new version...")

        # Detect the installed version from the game files (source of truth),
        # falling back to whatever a prior update run recorded in config.
        installed = detect_installed_version(
            self.config["game_dir"]
        ) or self.config.get("installed_version")
        if installed and installed != self.config.get("installed_version"):
            self.config["installed_version"] = installed
            save_config(self.config)

        def check():
            banner_path = None
            try:
                cdn_config = fetch_cdn_config()
                info = parse_version_info(cdn_config)
                self.version_info = info
                latest = info["version"]

                # Load banner for the latest version.
                try:
                    banner_url = fetch_banner_url()
                    if banner_url:
                        banner_path = str(download_banner(banner_url, latest))
                except Exception as be:
                    print(f"Banner load failed: {be}", file=sys.stderr)

                if installed == latest:
                    self.version_label.setText(
                        f"Installed: {installed}    Latest: {latest} \u2713"
                    )
                    self.status_label.setText("Up to date")
                else:
                    self.version_label.setText(
                        f"Installed: {installed or '?'}    "
                        f"Latest: {latest} (update available)"
                    )
                    self.status_label.setText("Update available")
            except Exception as e:
                self.version_label.setText("Version check failed")
                self.status_label.setText(f"Error: {e}")

            if banner_path:
                self.banner_loaded.emit(banner_path)

        Thread(target=check, daemon=True).start()

    # ─── Update ─────────────────────────────────────────────────────

    def _on_update(self):
        self.btn_update.setEnabled(False)
        self.btn_play.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)

        self.worker = UpdateWorker(self.config)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished_signal.connect(self._on_update_done)
        self.worker.start()

        # Sample the worker's byte counter once a second to display download
        # speed. The counter only advances during the download phase, so the
        # label stays hidden while files are being verified.
        self._speed_last = 0
        self._speed_timer = QTimer(self)
        self._speed_timer.timeout.connect(self._on_speed_tick)
        self._speed_timer.start(1000)

    def _on_progress(self, current, total, msg):
        pct = int(current / total * 100) if total > 0 else 0
        self.progress.setValue(pct)
        self.status_label.setText(msg)

    def _on_speed_tick(self):
        count = self.worker.byte_counter.count
        delta = count - self._speed_last
        self._speed_last = count
        # Hidden while no bytes arrived this tick: covers the verify phase
        # and retry backoffs, where a "0 B/s" readout would be misleading.
        if delta > 0:
            self.speed_label.setText(format_speed(delta))
            self.speed_label.setVisible(True)
        else:
            self.speed_label.setVisible(False)

    def _on_update_done(self, success, message):
        self._speed_timer.stop()
        self.speed_label.setVisible(False)
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.status_label.setText(message)
        self.btn_update.setEnabled(True)
        self.btn_play.setEnabled(True)
        if success:
            self.version_label.setText(
                f"Installed: {self.config.get('installed_version', '?')}"
            )
        self._log(message)

    def _on_cancel(self):
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.cancel()
            self._log("Cancelling...")

    # ─── Play ───────────────────────────────────────────────────────

    def _on_play(self):
        exe_path = Path(self.config["game_dir"]) / GAME_EXE
        if not exe_path.exists():
            QMessageBox.warning(
                self,
                "Error",
                "PGR.exe not found. Check game directory in Settings.",
            )
            return

        self._log("Launching PGR...")
        self.btn_play.setEnabled(False)
        self.status_label.setText("Game running...")

        try:
            self.game_process = launch_game(self.config)
            self._play_timer = QTimer()
            self._play_timer.timeout.connect(self._check_game_process)
            self._play_timer.start(2000)
        except Exception as e:
            QMessageBox.critical(self, "Launch Error", str(e))
            self.btn_play.setEnabled(True)
            self.status_label.setText("Ready")

    def _check_game_process(self):
        if self.game_process.poll() is not None:
            self.btn_play.setEnabled(True)
            self.status_label.setText("Ready")
            self._log(f"Game exited (code {self.game_process.returncode})")
            self._play_timer.stop()

    # ─── Settings ───────────────────────────────────────────────────

    def _on_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.config = dialog.get_config()
            save_config(self.config)
            self._log("Settings saved")


# ─── Entry point ────────────────────────────────────────────────────


def _run_api_test():
    cfg = fetch_cdn_config()
    info = parse_version_info(cfg)
    print(f"Version: {info['version']}")
    print(f"Hash: {info['hash']}")
    print(f"CDN hosts: {info['cdn_hosts']}")
    resources = fetch_index_file(info)
    print(f"Total files: {len(resources)}")


def main():
    if "--test-api" in sys.argv:
        _run_api_test()
        return
    app = QApplication(sys.argv)

    # Single-instance guard: two launchers downloading into the same game dir
    # race on the same .tmp files (the loser's atomic rename fails with ENOENT
    # after the winner already moved the file into place). QLockFile also
    # recovers the lock automatically after a crash via dead-PID detection.
    lock = QLockFile(str(Path(__file__).parent / ".launcher.lock"))
    if not lock.tryLock(0):
        QMessageBox.critical(
            None,
            GAME_NAME,
            "Another instance of the launcher is already running.\n"
            "Close it before starting a new one.",
        )
        sys.exit(1)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
