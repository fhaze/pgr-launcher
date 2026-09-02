#!/usr/bin/env python3
"""PGR BiliBili CN Updater + Launcher.

Standalone updater and launcher for Punishing: Gray Raven (BiliBili CN) on
Linux. Fetches Kurogame's CDN metadata, applies official bundled or incremental
packages in manifest order, verifies the resulting game files, repairs any
remaining mismatches, writes the in-game downloader configuration, and launches
the game through Proton/steamrt.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread, local
from urllib.parse import quote, urljoin

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
STAGING_DIR_NAME = ".pgr-launcher-staging"

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


def _make_download_plan(
    config: dict, kind: str, target_version: str, source_version: str | None = None
) -> dict:
    return {
        "kind": kind,
        "version": target_version,
        "source_version": source_version,
        "base_url": config["baseUrl"],
        "index_file_path": config["indexFile"],
        "index_file_md5": config.get("indexFileMd5"),
        "size": config.get("size", 0),
        "uncompress_size": config.get("unCompressSize", config.get("size", 0)),
        "max_file_size": config.get("ext", {}).get("maxFileSize", 0),
    }


def parse_version_info(cdn_config: dict) -> dict:
    """Extract all usable download plans from the CDN config."""
    default = cdn_config["default"]
    cdn_hosts = [c["url"] for c in default["cdnList"]]
    cfg = default["config"]
    version = cfg["version"]
    # baseUrl is "launcher/game/<gameId>/<appId>/<version>/<HASH>/zip/", so the
    # hash is the segment right after the version (index 5, not 4).
    base_parts = cfg["baseUrl"].strip("/").split("/")
    full_plan = _make_download_plan(cfg, "full", version)
    zip_plan = None
    if cfg.get("zipConfig"):
        zip_plan = _make_download_plan(cfg["zipConfig"], "zip", version)
    patch_plans = [
        _make_download_plan(p, "patch", version, p["version"])
        for p in cfg.get("patchConfig", [])
    ]
    return {
        "version": version,
        "hash": base_parts[5],
        "cdn_hosts": cdn_hosts,
        "index_file_path": cfg["indexFile"],
        "full_size": cfg["size"],
        "full_plan": full_plan,
        "zip_plan": zip_plan,
        "patch_plans": patch_plans,
    }


def select_download_plan(
    version_info: dict, installed_version: str | None, game_present: bool
) -> dict:
    if game_present and installed_version != version_info["version"]:
        for plan in version_info["patch_plans"]:
            if plan["source_version"] == installed_version:
                return plan
    if not game_present and version_info["zip_plan"] is not None:
        return version_info["zip_plan"]
    return version_info["full_plan"]


def _resolve_cdn_path(host: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return urljoin(host.rstrip("/") + "/", path.lstrip("/"))


def _normalize_resource_dest(dest: str) -> str:
    if not dest or "\x00" in dest:
        raise ValueError(f"unsafe resource path: {dest!r}")
    normalized = dest.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"unsafe resource path: {dest!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"unsafe resource path: {dest!r}")
    return "/".join(parts)


def resolve_resource_url(
    version_info: dict, plan: dict, resource: dict, host_index: int = 0
) -> str:
    host = version_info["cdn_hosts"][host_index]
    folder = resource.get("fromFolder") or plan["base_url"]
    base = _resolve_cdn_path(host, folder).rstrip("/") + "/"
    return base + quote(_normalize_resource_dest(resource["dest"]), safe="/")


def fetch_index_document(version_info: dict, plan: dict | None = None) -> dict:
    plan = plan or version_info["full_plan"]
    errors = []
    for host in version_info["cdn_hosts"]:
        url = _resolve_cdn_path(host, plan["index_file_path"])
        try:
            resp = requests.get(
                url, timeout=(15, 90), headers={"User-Agent": USER_AGENT}
            )
            resp.raise_for_status()
            payload = resp.content
            expected_md5 = plan.get("index_file_md5")
            if expected_md5 and hashlib.md5(payload).hexdigest() != expected_md5:
                raise RuntimeError("md5 mismatch")
            return resp.json()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {e}")
    raise RuntimeError(
        f"Could not fetch {plan['kind']} index from any CDN: {'; '.join(errors)}"
    )


def fetch_index_file(version_info: dict) -> list:
    """Fetch the full indexFile and return the resource list."""
    return fetch_index_document(version_info)["resource"]


def get_download_url(version_info: dict, dest: str) -> str:
    """Construct the full-plan download URL for a game file."""
    return resolve_resource_url(
        version_info, version_info["full_plan"], {"dest": dest}
    )


def package_output_map(index_document: dict) -> dict[str, dict]:
    outputs = {}
    transformed = set()
    for key in ("zipInfos", "patchInfos"):
        for group in index_document.get(key, []):
            transformed.add(group["dest"])
            for entry in group["entries"]:
                outputs[entry["dest"]] = entry
    for group in index_document.get("groupInfos", []):
        transformed.add(group["dest"])
        for entry in group.get("dstFiles", []):
            outputs[entry["dest"]] = entry
    for resource in index_document["resource"]:
        if resource["dest"] not in transformed:
            outputs[resource["dest"]] = resource
    for dest in index_document.get("deleteFiles", []):
        outputs.pop(dest, None)
    return outputs


def validate_package_index(
    index_document: dict, full_resources: list, complete: bool
) -> dict[str, dict]:
    outputs = package_output_map(index_document)
    expected = {r["dest"]: r for r in full_resources}
    invalid = [
        dest
        for dest, resource in outputs.items()
        if dest not in expected
        or (resource["size"], resource["md5"])
        != (expected[dest]["size"], expected[dest]["md5"])
    ]
    invalid_deletes = [
        dest for dest in index_document.get("deleteFiles", []) if dest in expected
    ]
    if invalid or invalid_deletes:
        raise ValueError(
            f"package manifest disagrees with full index "
            f"({len(invalid)} outputs, {len(invalid_deletes)} deletions)"
        )
    if complete and set(outputs) != set(expected):
        raise ValueError(
            f"complete package has {len(outputs)} outputs; expected {len(expected)}"
        )
    return outputs


def _safe_destination(root: Path, dest: str) -> Path:
    relative = Path(_normalize_resource_dest(dest))
    if relative.is_absolute():
        raise ValueError(f"unsafe resource path: {dest!r}")
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as e:
        raise ValueError(f"resource path escapes destination: {dest!r}") from e
    if resolved == resolved_root:
        raise ValueError(f"unsafe resource path: {dest!r}")
    return resolved


def _hash_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path: Path, resource: dict) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == resource["size"]
        and _hash_file(path) == resource["md5"]
    )


def extract_zip_archive(
    archive_path: Path,
    destination: Path,
    entries: list,
    cancel_check=None,
    on_progress=None,
) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    extracted = 0
    total = sum(entry["size"] for entry in entries)
    with zipfile.ZipFile(archive_path) as archive:
        members = {}
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename.rstrip("/")
            if name in members:
                raise ValueError(f"archive contains duplicate entry: {name}")
            members[name] = member
        for entry in entries:
            if cancel_check and cancel_check():
                raise RuntimeError("cancelled")
            dest = entry["dest"].replace("\\", "/")
            target = _safe_destination(destination, dest)
            member = members.get(dest)
            if member is None:
                raise ValueError(f"archive is missing {dest}")
            if member.file_size != entry["size"]:
                raise ValueError(f"archive entry has the wrong size: {dest}")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f"archive entry is a symlink: {dest}")
            if _file_matches(target, entry):
                extracted += entry["size"]
                if on_progress:
                    on_progress(extracted, total, dest)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.with_name(target.name + ".extracting")
            digest = hashlib.md5()
            written = 0
            try:
                with archive.open(member) as src, open(tmp_path, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        if cancel_check and cancel_check():
                            raise RuntimeError("cancelled")
                        dst.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        if written > entry["size"]:
                            raise RuntimeError(
                                f"archive entry exceeds expected size: {dest}"
                            )
                if written != entry["size"] or digest.hexdigest() != entry["md5"]:
                    raise RuntimeError(f"archive entry verification failed: {dest}")
                tmp_path.replace(target)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            extracted += written
            if on_progress:
                on_progress(extracted, total, dest)
    return extracted


def _entries_match(root: Path, entries: list, cancel_check=None) -> bool:
    for entry in entries:
        if cancel_check and cancel_check():
            raise RuntimeError("cancelled")
        if not _file_matches(_safe_destination(root, entry["dest"]), entry):
            return False
    return True


def _delete_manifest_paths(root: Path, destinations: list):
    for dest in destinations:
        path = _safe_destination(root, dest)
        if path.is_dir():
            raise ValueError(f"refusing to delete directory from manifest: {dest}")
        path.unlink(missing_ok=True)


def _merge_staged_tree(
    source: Path, destination: Path, cancel_check=None, on_progress=None
):
    files = [path for path in source.rglob("*") if path.is_file()]
    total = len(files)
    for index, path in enumerate(files, 1):
        if cancel_check and cancel_check():
            raise RuntimeError("cancelled")
        if path.is_symlink():
            raise ValueError(f"staged file is a symlink: {path}")
        relative = path.relative_to(source).as_posix()
        target = _safe_destination(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        if on_progress:
            on_progress(index, total, relative)


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


_download_session_state = local()


def _get_download_session() -> requests.Session:
    session = getattr(_download_session_state, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _download_session_state.session = session
    return session


def download_file(
    url: str,
    expected_md5: str,
    local_path: Path,
    cancel_check=None,
    on_retry=None,
    byte_counter=None,
    expected_size: int | None = None,
    session=None,
) -> str | None:
    """Download *url* atomically with retries and HTTP range resumption."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
    client = session or requests
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        if cancel_check and cancel_check():
            return "cancelled"
        try:
            resume_from = tmp_path.stat().st_size if tmp_path.exists() else 0
            if expected_size is None or resume_from > expected_size:
                tmp_path.unlink(missing_ok=True)
                resume_from = 0
            if expected_size is not None and resume_from == expected_size:
                if _hash_file(tmp_path) == expected_md5:
                    tmp_path.replace(local_path)
                    return None
                tmp_path.unlink()
                resume_from = 0

            headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            with client.get(
                url,
                timeout=(15, 60),
                stream=True,
                headers=headers,
            ) as resp:
                if resume_from and resp.status_code == 416:
                    tmp_path.unlink(missing_ok=True)
                    raise RuntimeError("CDN rejected the partial range; restarting")
                resp.raise_for_status()
                append = resume_from > 0 and resp.status_code == 206
                if append:
                    content_range = resp.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {resume_from}-"):
                        tmp_path.unlink(missing_ok=True)
                        raise RuntimeError("CDN returned an invalid range; restarting")
                    digest = hashlib.md5()
                    with open(tmp_path, "rb") as existing:
                        while chunk := existing.read(1024 * 1024):
                            digest.update(chunk)
                    mode = "ab"
                else:
                    resume_from = 0
                    digest = hashlib.md5()
                    mode = "wb"
                with open(tmp_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if cancel_check and cancel_check():
                            return "cancelled"
                        if not chunk:
                            continue
                        f.write(chunk)
                        digest.update(chunk)
                        if byte_counter is not None:
                            byte_counter.add(len(chunk))
            actual_size = tmp_path.stat().st_size
            if expected_size is not None and actual_size != expected_size:
                raise RuntimeError(
                    f"size mismatch ({actual_size} bytes, expected {expected_size})"
                )
            if digest.hexdigest() != expected_md5:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError("md5 mismatch (truncated or corrupt download)")
            tmp_path.replace(local_path)
            return None
        except Exception as e:  # noqa: BLE001
            if expected_size is None:
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
            message = "Cancelled" if str(e) == "cancelled" else f"Error: {e}"
            self.finished_signal.emit(False, message)

    def _do_update(self):
        game_dir = Path(self.config["game_dir"])
        game_dir.mkdir(parents=True, exist_ok=True)

        # 1. Fetch CDN config.
        self.log.emit("Fetching CDN config...")
        cdn_config = fetch_cdn_config()
        info = parse_version_info(cdn_config)
        self.log.emit(f"Latest version: {info['version']}")

        # 2. Fetch indexFile.
        self.log.emit("Fetching full file index...")
        full_document = fetch_index_document(info)
        resources = full_document["resource"]
        self.log.emit(f"Total files in index: {len(resources)}")

        game_present = (game_dir / GAME_EXE).is_file()
        installed = detect_installed_version(game_dir) if game_present else None
        plan = select_download_plan(info, installed, game_present)
        verified = set()
        used_package = False

        if plan["kind"] != "full":
            self.log.emit(f"Fetching {plan['kind']} package index...")
            package_document = fetch_index_document(info, plan)
            unsupported = package_document.get("patchInfos") or package_document.get(
                "groupInfos"
            )
            try:
                if unsupported:
                    raise ValueError("package uses unsupported binary-diff entries")
                validate_package_index(
                    package_document, resources, complete=plan["kind"] == "zip"
                )
            except (KeyError, ValueError) as e:
                self.log.emit(f"Package plan is unusable; using full repair: {e}")
                plan = info["full_plan"]
            else:
                self._check_package_space(game_dir, plan)
                if plan["kind"] == "zip":
                    self.log.emit(
                        f"Using bundled install: {len(package_document['resource'])} "
                        f"download objects"
                    )
                else:
                    self.log.emit(
                        f"Using patch {plan['source_version']} → {info['version']}: "
                        f"{len(package_document['resource'])} download objects"
                    )
                verified = self._apply_package_plan(
                    info, plan, package_document, game_dir
                )
                used_package = True

        # 3. Verify all files by size + md5.
        to_download = self._find_invalid_resources(resources, game_dir, verified)
        if to_download:
            total_dl_size = sum(r["size"] for r in to_download)
            self.log.emit(
                f"Need to repair {len(to_download)} files "
                f"({total_dl_size / 1024 / 1024:.0f} MB)"
            )

            # 4. Download missing/mismatched files in parallel.
            self._download_full_resources(info, to_download, game_dir)
        else:
            self.log.emit("All files verified. No additional repair needed.")

        if self._cancel:
            raise RuntimeError("cancelled")

        # Do NOT write the game configs or record the version on a partial
        # download: detect_installed_version() reads
        # launcherDownloadConfig.json, so recording the new version would
        # make the next startup report "up to date" with files still
        # missing. Re-running the update re-verifies everything and
        # downloads only the files that are still wrong.
        # 5. Create game config files.
        self._create_game_configs(info, resources)

        # 6. Persist installed version.
        self.config["installed_version"] = info["version"]
        self.config["last_checked_version"] = info["version"]
        save_config(self.config)
        shutil.rmtree(game_dir / STAGING_DIR_NAME, ignore_errors=True)

        if used_package or to_download or installed != info["version"]:
            message = f"Updated to {info['version']}"
        else:
            message = f"Ready — version {info['version']}"
        self.finished_signal.emit(True, message)

    def _run_tasks(self, resources: list, task, label: str) -> set[str]:
        if not resources:
            return set()
        completed = set()
        errors = []
        total = len(resources)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(task, resource): resource for resource in resources
            }
            for done, future in enumerate(as_completed(futures), 1):
                resource = futures[future]
                try:
                    completed.add(future.result())
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{resource['dest']}: {e}")
                if done % 50 == 0 or done == total:
                    self.progress.emit(done, total, f"{label}... {done}/{total}")
                if self._cancel:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("cancelled")
        if errors:
            for error in errors[:10]:
                self.log.emit(f"Failed: {error}")
            raise RuntimeError(
                f"{label} failed for {len(errors)} of {total} objects"
            )
        return completed

    def _download_resource(
        self, version_info: dict, plan: dict, resource: dict, target: Path
    ) -> str:
        if _file_matches(target, resource):
            return resource["dest"]
        errors = []
        for host_index in range(len(version_info["cdn_hosts"])):
            err = download_file(
                resolve_resource_url(version_info, plan, resource, host_index),
                resource["md5"],
                target,
                cancel_check=lambda: self._cancel,
                byte_counter=self.byte_counter,
                expected_size=resource["size"],
                session=_get_download_session(),
                on_retry=lambda attempt, e: self.log.emit(
                    f"Retrying {resource['dest']} "
                    f"({attempt + 1}/{MAX_DOWNLOAD_RETRIES}): {e}"
                ),
            )
            if err is None:
                return resource["dest"]
            if err == "cancelled":
                raise RuntimeError("cancelled")
            errors.append(err)
            if host_index + 1 < len(version_info["cdn_hosts"]):
                self.log.emit(f"Switching CDN for {resource['dest']}: {err}")
        raise RuntimeError("; ".join(errors))

    def _download_resources(
        self,
        version_info: dict,
        plan: dict,
        resources: list,
        destination: Path,
        label: str,
    ) -> set[str]:
        def download_one(resource):
            target = _safe_destination(destination, resource["dest"])
            return self._download_resource(version_info, plan, resource, target)

        return self._run_tasks(resources, download_one, label)

    def _plan_signature(self, plan: dict) -> dict:
        return {
            "kind": plan["kind"],
            "version": plan["version"],
            "source_version": plan["source_version"],
            "index_file_path": plan["index_file_path"],
            "index_file_md5": plan["index_file_md5"],
        }

    def _check_package_space(self, game_dir: Path, plan: dict):
        state_path = game_dir / STAGING_DIR_NAME / "plan.json"
        if state_path.is_file():
            try:
                if json.loads(state_path.read_text()) == self._plan_signature(plan):
                    return
            except (OSError, json.JSONDecodeError):
                pass
        required = max(plan["size"], plan["uncompress_size"]) + plan["max_file_size"]
        available = shutil.disk_usage(game_dir).free
        if available < required:
            raise RuntimeError(
                f"Not enough disk space for {plan['kind']} plan: "
                f"need {required / 1024**3:.1f} GiB free, "
                f"have {available / 1024**3:.1f} GiB"
            )

    def _prepare_work_dir(self, game_dir: Path, plan: dict) -> Path:
        work_dir = game_dir / STAGING_DIR_NAME
        signature = self._plan_signature(plan)
        state_path = work_dir / "plan.json"
        current = None
        if state_path.is_file():
            try:
                current = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        if work_dir.is_symlink():
            raise ValueError(f"staging directory is a symlink: {work_dir}")
        if work_dir.exists() and current != signature:
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        tmp_state = work_dir / "plan.json.tmp"
        tmp_state.write_text(json.dumps(signature, indent=2))
        tmp_state.replace(state_path)
        return work_dir

    def _apply_package_plan(
        self, version_info: dict, plan: dict, document: dict, game_dir: Path
    ) -> set[str]:
        work_dir = self._prepare_work_dir(game_dir, plan)
        archive_dir = work_dir / "archives"
        raw_dir = work_dir / "raw"
        assembled_dir = work_dir / "assembled"
        for directory in (archive_dir, raw_dir, assembled_dir):
            if directory.is_symlink():
                raise ValueError(f"staging path is a symlink: {directory}")
            directory.mkdir(parents=True, exist_ok=True)

        zip_group_list = document.get("zipInfos", [])
        zip_groups = {group["dest"]: group for group in zip_group_list}
        if len(zip_groups) != len(zip_group_list):
            raise ValueError("zipInfos contains duplicate archives")
        resource_by_dest = {
            resource["dest"]: resource for resource in document["resource"]
        }
        if len(resource_by_dest) != len(document["resource"]):
            raise ValueError("package contains duplicate resources")
        try:
            archive_resources = [
                resource_by_dest[group["dest"]] for group in zip_group_list
            ]
        except KeyError as e:
            raise ValueError("zipInfos does not match archive resources") from e
        direct_resources = [
            resource
            for resource in document["resource"]
            if resource["dest"] not in zip_groups
        ]
        direct_destinations = {resource["dest"] for resource in direct_resources}
        deleted = set(document.get("deleteFiles", []))
        outputs = package_output_map(document)
        verified = set()

        archive_entries = {
            resource["dest"]: [
                entry
                for entry in zip_groups[resource["dest"]]["entries"]
                if entry["dest"] not in direct_destinations
                and entry["dest"] not in deleted
                and entry["dest"] in outputs
                and (entry["size"], entry["md5"])
                == (
                    outputs[entry["dest"]]["size"],
                    outputs[entry["dest"]]["md5"],
                )
            ]
            for resource in archive_resources
        }
        needed_archives = []
        for resource in archive_resources:
            entries = archive_entries[resource["dest"]]
            assembled_ready = _entries_match(
                assembled_dir, entries, lambda: self._cancel
            )
            game_ready = not assembled_ready and _entries_match(
                game_dir, entries, lambda: self._cancel
            )
            if assembled_ready:
                verified.update(entry["dest"] for entry in entries)
                _safe_destination(archive_dir, resource["dest"]).unlink(missing_ok=True)
            elif game_ready:
                _delete_manifest_paths(
                    assembled_dir, [entry["dest"] for entry in entries]
                )
                verified.update(entry["dest"] for entry in entries)
                _safe_destination(archive_dir, resource["dest"]).unlink(missing_ok=True)
            else:
                needed_archives.append(resource)
        self._download_resources(
            version_info, plan, needed_archives, archive_dir, "Downloading archives"
        )
        for index, resource in enumerate(needed_archives, 1):
            entries = archive_entries[resource["dest"]]
            archive_path = _safe_destination(archive_dir, resource["dest"])
            self.log.emit(
                f"Extracting archive {index}/{len(needed_archives)}: "
                f"{resource['dest']} ({len(entries)} files)"
            )
            last_reported = [0]

            def extraction_progress(done, total, dest):
                if done - last_reported[0] >= 16 * 1024 * 1024 or done == total:
                    last_reported[0] = done
                    self.progress.emit(
                        done,
                        total,
                        f"Extracting {resource['dest']}... "
                        f"{done / 1024**2:.0f}/{total / 1024**2:.0f} MB",
                    )

            extract_zip_archive(
                archive_path,
                assembled_dir,
                entries,
                cancel_check=lambda: self._cancel,
                on_progress=extraction_progress,
            )
            archive_path.unlink()
            verified.update(entry["dest"] for entry in entries)

        def download_direct(resource):
            final_path = _safe_destination(game_dir, resource["dest"])
            assembled_path = _safe_destination(assembled_dir, resource["dest"])
            assembled_ready = _file_matches(assembled_path, resource)
            if assembled_ready:
                return resource["dest"]
            if _file_matches(final_path, resource):
                _delete_manifest_paths(assembled_dir, [resource["dest"]])
                return resource["dest"]
            raw_path = _safe_destination(raw_dir, resource["dest"])
            self._download_resource(version_info, plan, resource, raw_path)
            assembled_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.replace(assembled_path)
            return resource["dest"]

        verified.update(
            self._run_tasks(direct_resources, download_direct, "Downloading game files")
        )
        expected_verified = set(outputs)
        if verified != expected_verified:
            raise RuntimeError(
                f"package verified {len(verified)} outputs; "
                f"expected {len(expected_verified)}"
            )
        _delete_manifest_paths(assembled_dir, list(deleted))
        self.log.emit("Installing staged files...")

        def merge_progress(done, total, dest):
            if done % 100 == 0 or done == total:
                self.progress.emit(done, total, f"Installing... {done}/{total}")

        _merge_staged_tree(
            assembled_dir,
            game_dir,
            cancel_check=lambda: self._cancel,
            on_progress=merge_progress,
        )
        _delete_manifest_paths(game_dir, list(deleted))
        return verified

    def _find_invalid_resources(
        self, resources: list, game_dir: Path, verified: set[str]
    ) -> list:
        self.log.emit("Verifying files...")
        invalid = []
        total = len(resources)
        for index, resource in enumerate(resources, 1):
            if self._cancel:
                raise RuntimeError("cancelled")
            local_path = _safe_destination(game_dir, resource["dest"])
            if (
                not local_path.is_file()
                or local_path.stat().st_size != resource["size"]
            ):
                invalid.append(resource)
            elif resource["dest"] not in verified and resource["size"] < MD5_SKIP_SIZE:
                if _hash_file(local_path) != resource["md5"]:
                    invalid.append(resource)
            if index % 500 == 0 or index == total:
                self.progress.emit(index, total, f"Verifying... {index}/{total}")
        return invalid

    def _download_full_resources(
        self, version_info: dict, resources: list, game_dir: Path
    ):
        plan = version_info["full_plan"]

        def download_one(resource):
            target = _safe_destination(game_dir, resource["dest"])
            return self._download_resource(version_info, plan, resource, target)

        self._run_tasks(resources, download_one, "Downloading repairs")

    def _create_game_configs(self, version_info: dict, resources: list):
        """Create LocalGameResources.json and launcherDownloadConfig.json."""
        game_dir = Path(self.config["game_dir"])
        host = version_info["cdn_hosts"][0]
        ver = version_info["version"]
        from_folder = (
            _resolve_cdn_path(host, version_info["full_plan"]["base_url"]).rstrip("/")
            + "/"
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
