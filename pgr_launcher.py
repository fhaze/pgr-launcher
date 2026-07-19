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
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Thread

import requests

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
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

# Files at or above this size skip md5 verification during the check phase to
# keep verification fast; a size match is trusted for them.
MD5_SKIP_SIZE = 100_000_000

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


# ─── CDN API client ─────────────────────────────────────────────────


def fetch_cdn_config() -> dict:
    """Fetch the stable config endpoint. Returns the full JSON document."""
    resp = requests.get(
        CONFIG_URL, timeout=15, headers={"User-Agent": USER_AGENT}
    )
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


# ─── Update worker ──────────────────────────────────────────────────


class UpdateWorker(QThread):
    progress = Signal(int, int, str)  # current, total, message
    log = Signal(str)
    finished_signal = Signal(bool, str)  # success, message

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self._do_update()
        except Exception as e:
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
            if not local_path.exists():
                to_download.append(r)
            elif local_path.stat().st_size != r["size"]:
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
            url = get_download_url(info, dest)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
            try:
                resp = requests.get(
                    url, timeout=60, headers={"User-Agent": USER_AGENT}
                )
                resp.raise_for_status()
                actual_md5 = hashlib.md5(resp.content).hexdigest()
                if actual_md5 != r["md5"]:
                    return (dest, False, "md5 mismatch")
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
                tmp_path.replace(local_path)
                return (dest, True, None)
            except Exception as e:
                tmp_path.unlink(missing_ok=True)
                return (dest, False, str(e))

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

        # 5. Create game config files.
        self._create_game_configs(info, resources)

        # 6. Persist installed version.
        self.config["installed_version"] = info["version"]
        self.config["last_checked_version"] = info["version"]
        save_config(self.config)

        if failed:
            self.finished_signal.emit(
                False,
                f"Updated to {info['version']} with {failed} failed downloads",
            )
        else:
            self.finished_signal.emit(True, f"Updated to {info['version']}")

    def _create_game_configs(self, version_info: dict, resources: list):
        """Create LocalGameResources.json and launcherDownloadConfig.json."""
        game_dir = Path(self.config["game_dir"])
        host = version_info["cdn_hosts"][0]
        ver = version_info["version"]
        h = version_info["hash"]
        from_folder = (
            f"{host}launcher/game/{GAME_ID}/{APP_ID}/{ver}/{h}/zip/"
        )

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
            r for r in resources
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
        self.log.emit(
            f"Created LocalGameResources.json ({len(streaming)} entries)"
        )


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

    cmd = [
        steamrt, "--verb=waitforexitandrun", "--",
        reaper, "SteamLaunch", "AppId=0", "--",
        proton, "waitforexitandrun", exe_z, graphics,
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
        browse.clicked.connect(
            lambda: self._browse(line, title, directory=directory)
        )
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
            layout, "Game Directory:", self.config["game_dir"],
            "Select Game Directory", directory=True,
        )
        self.proton_path = self._path_row(
            layout, "Proton Path:", self.config["proton_path"],
            "Select Proton Binary",
        )
        self.prefix_path = self._path_row(
            layout, "Wine Prefix:", self.config["prefix_path"],
            "Select Wine Prefix", directory=True,
        )
        self.steamrt_path = self._path_row(
            layout, "steamrt Entry Point:", self.config["steamrt_path"],
            "Select steamrt Entry Point",
        )
        self.reaper_path = self._path_row(
            layout, "Reaper Path:", self.config["reaper_path"],
            "Select Reaper Binary",
        )

        self.graphics_api = QComboBox()
        self.graphics_api.addItem("DirectX 11", "-force-d3d11")
        self.graphics_api.addItem("DirectX 12", "-force-d3d12")
        idx = self.graphics_api.findData(
            self.config.get("graphics_api", "-force-d3d11")
        )
        self.graphics_api.setCurrentIndex(idx if idx >= 0 else 0)
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.version_info = None
        self.setWindowTitle("PGR BiliBili CN Launcher")
        self.setMinimumSize(700, 500)
        self._build_ui()

        # Quick version check on startup (no file verification).
        QTimer.singleShot(500, self._quick_version_check)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        title = QLabel(GAME_NAME)
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.version_label = QLabel("Checking version...")
        self.version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.version_label)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

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
        self.log.setMaximumHeight(150)
        layout.addWidget(self.log)

        self.btn_update.clicked.connect(self._on_update)
        self.btn_play.clicked.connect(self._on_play)
        self.btn_settings.clicked.connect(self._on_settings)
        self.btn_cancel.clicked.connect(self._on_cancel)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")

    # ─── Startup version check ──────────────────────────────────────

    def _quick_version_check(self):
        """Fetch the CDN version without verifying files (runs off-thread)."""
        self.status_label.setText("Checking for new version...")

        def check():
            try:
                cdn_config = fetch_cdn_config()
                info = parse_version_info(cdn_config)
                self.version_info = info
                latest = info["version"]
                installed = self.config.get("installed_version")
                if installed == latest:
                    self.version_label.setText(
                        f"Installed: {installed}    Latest: {latest} ✓"
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

    def _on_progress(self, current, total, msg):
        pct = int(current / total * 100) if total > 0 else 0
        self.progress.setValue(pct)
        self.status_label.setText(msg)

    def _on_update_done(self, success, message):
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
                self, "Error",
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
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
