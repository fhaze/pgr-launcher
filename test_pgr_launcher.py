import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pgr_launcher
from pgr_launcher import (
    download_file,
    extract_zip_archive,
    package_output_map,
    parse_version_info,
    resolve_resource_url,
    select_download_plan,
    validate_package_index,
)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class DownloadPlanTests(unittest.TestCase):
    def setUp(self):
        self.info = parse_version_info(
            {
                "default": {
                    "cdnList": [{"url": "https://cdn.example/"}],
                    "config": {
                        "version": "2.0.0",
                        "baseUrl": "launcher/game/G148/10011/2.0.0/newhash/zip/",
                        "indexFile": "launcher/game/G148/10011/2.0.0/index.json",
                        "size": 1000,
                        "unCompressSize": 1000,
                        "zipConfig": {
                            "version": "2.0.0",
                            "baseUrl": "launcher/game/G148/10011/1.0.0/resources/",
                            "indexFile": (
                                "launcher/game/G148/10011/2.0.0/full/index.json"
                            ),
                            "size": 900,
                            "unCompressSize": 1000,
                        },
                        "patchConfig": [
                            {
                                "version": "1.0.0",
                                "baseUrl": "launcher/game/G148/10011/2.0.0/patch/",
                                "indexFile": (
                                    "launcher/game/G148/10011/2.0.0/patch/index.json"
                                ),
                                "size": 100,
                                "unCompressSize": 120,
                            }
                        ],
                    },
                }
            }
        )

    def test_selects_zip_for_clean_install(self):
        plan = select_download_plan(self.info, None, False)
        self.assertEqual(plan["kind"], "zip")
        self.assertEqual(plan["size"], 900)

    def test_selects_matching_patch_for_existing_install(self):
        plan = select_download_plan(self.info, "1.0.0", True)
        self.assertEqual(plan["kind"], "patch")
        self.assertEqual(plan["source_version"], "1.0.0")

    def test_uses_full_plan_for_repair(self):
        self.assertEqual(
            select_download_plan(self.info, "2.0.0", True)["kind"], "full"
        )
        self.assertEqual(select_download_plan(self.info, None, True)["kind"], "full")

    def test_resolves_from_folder_before_plan_base_url(self):
        plan = select_download_plan(self.info, None, False)
        resource = {
            "dest": "folder\\file name.bin",
            "fromFolder": "launcher/game/G148/10011/2.0.0/override/",
        }
        self.assertEqual(
            resolve_resource_url(self.info, plan, resource),
            "https://cdn.example/launcher/game/G148/10011/2.0.0/override/"
            "folder/file%20name.bin",
        )
        self.assertEqual(
            resolve_resource_url(self.info, plan, {"dest": "0.krzip"}),
            "https://cdn.example/launcher/game/G148/10011/1.0.0/resources/0.krzip",
        )


class PackageManifestTests(unittest.TestCase):
    def test_archives_are_overlaid_by_direct_files_before_deletions(self):
        old = b"old"
        new = b"new"
        keep = b"keep"
        obsolete = b"obsolete"
        index = {
            "resource": [
                {"dest": "0.krzip", "size": 100, "md5": "archive"},
                {"dest": "a.txt", "size": len(new), "md5": md5(new)},
                {"dest": "b.bin", "size": len(keep), "md5": md5(keep)},
            ],
            "zipInfos": [
                {
                    "dest": "0.krzip",
                    "entries": [
                        {"dest": "a.txt", "size": len(old), "md5": md5(old)},
                        {
                            "dest": "obsolete.txt",
                            "size": len(obsolete),
                            "md5": md5(obsolete),
                        },
                    ],
                }
            ],
            "deleteFiles": ["obsolete.txt"],
        }
        expected = [
            {"dest": "a.txt", "size": len(new), "md5": md5(new)},
            {"dest": "b.bin", "size": len(keep), "md5": md5(keep)},
        ]
        outputs = package_output_map(index)
        self.assertEqual(outputs["a.txt"]["md5"], md5(new))
        self.assertNotIn("obsolete.txt", outputs)
        self.assertEqual(
            validate_package_index(index, expected, complete=True), outputs
        )

    def test_rejects_package_output_that_disagrees_with_full_manifest(self):
        index = {
            "resource": [{"dest": "a.txt", "size": 3, "md5": md5(b"bad")}]
        }
        expected = [{"dest": "a.txt", "size": 4, "md5": md5(b"good")}]
        with self.assertRaises(ValueError):
            validate_package_index(index, expected, complete=False)


class ArchiveExtractionTests(unittest.TestCase):
    def test_extracts_only_manifest_entries_and_verifies_them(self):
        payload = b"small payload"
        ignored = b"not in manifest"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.krzip"
            output = root / "output"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("data/file.txt", payload)
                zf.writestr("ignored.txt", ignored)
            extracted = extract_zip_archive(
                archive,
                output,
                [{"dest": "data/file.txt", "size": len(payload), "md5": md5(payload)}],
            )
            self.assertEqual(extracted, len(payload))
            self.assertEqual((output / "data/file.txt").read_bytes(), payload)
            self.assertFalse((output / "ignored.txt").exists())

    def test_rejects_archive_path_traversal(self):
        payload = b"bad"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.krzip"
            output = root / "output"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("../outside.txt", payload)
            with self.assertRaises(ValueError):
                extract_zip_archive(
                    archive,
                    output,
                    [
                        {
                            "dest": "../outside.txt",
                            "size": len(payload),
                            "md5": md5(payload),
                        }
                    ],
                )
            self.assertFalse((root / "outside.txt").exists())


class ResumableDownloadTests(unittest.TestCase):
    def test_resumes_partial_download_with_http_range(self):
        payload = b"abcdefghij"

        class Response:
            status_code = 206
            headers = {"Content-Range": "bytes 4-9/10"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield payload[4:]

        class Session:
            def __init__(self):
                self.range = None

            def get(self, url, **kwargs):
                self.range = kwargs["headers"].get("Range")
                return Response()

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "payload.bin"
            target.with_suffix(".bin.tmp").write_bytes(payload[:4])
            session = Session()
            error = download_file(
                "https://cdn.example/payload.bin",
                md5(payload),
                target,
                expected_size=len(payload),
                session=session,
            )
            self.assertIsNone(error)
            self.assertEqual(session.range, "bytes=4-")
            self.assertEqual(target.read_bytes(), payload)

    def test_restarts_after_invalid_content_range(self):
        payload = b"abcdefghij"

        class Response:
            def __init__(self, status_code, content_range, body):
                self.status_code = status_code
                self.headers = {"Content-Range": content_range} if content_range else {}
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield self.body

        class Session:
            def __init__(self):
                self.ranges = []

            def get(self, url, **kwargs):
                self.ranges.append(kwargs["headers"].get("Range"))
                if len(self.ranges) == 1:
                    return Response(206, "bytes 0-5/10", payload[4:])
                return Response(200, None, payload)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "payload.bin"
            target.with_suffix(".bin.tmp").write_bytes(payload[:4])
            session = Session()
            with patch("pgr_launcher.time.sleep"):
                error = download_file(
                    "https://cdn.example/payload.bin",
                    md5(payload),
                    target,
                    expected_size=len(payload),
                    session=session,
                )
            self.assertIsNone(error)
            self.assertEqual(session.ranges, ["bytes=4-", None])
            self.assertEqual(target.read_bytes(), payload)


class PackageApplicationTests(unittest.TestCase):
    def test_worker_applies_archive_then_direct_overlay_and_deletions(self):
        old = b"old"
        new = b"new"
        keep = b"keep"
        obsolete = b"obsolete"
        direct = b"direct"
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "game"
            game_dir.mkdir()
            (game_dir / "obsolete.txt").write_bytes(obsolete)
            archive_path = Path(tmp) / "source.krzip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("a.txt", old)
                zf.writestr("keep.txt", keep)
                zf.writestr("obsolete.txt", obsolete)
            archive = archive_path.read_bytes()
            document = {
                "resource": [
                    {"dest": "0.krzip", "size": len(archive), "md5": md5(archive)},
                    {"dest": "a.txt", "size": len(new), "md5": md5(new)},
                    {
                        "dest": "folder/direct.bin",
                        "size": len(direct),
                        "md5": md5(direct),
                    },
                ],
                "zipInfos": [
                    {
                        "dest": "0.krzip",
                        "entries": [
                            {"dest": "a.txt", "size": len(old), "md5": md5(old)},
                            {
                                "dest": "keep.txt",
                                "size": len(keep),
                                "md5": md5(keep),
                            },
                            {
                                "dest": "obsolete.txt",
                                "size": len(obsolete),
                                "md5": md5(obsolete),
                            },
                        ],
                    }
                ],
                "deleteFiles": ["obsolete.txt"],
            }
            plan = {
                "kind": "zip",
                "version": "2.0.0",
                "source_version": None,
                "base_url": "resources/",
                "index_file_path": "index.json",
                "index_file_md5": None,
                "size": len(archive) + len(new) + len(direct),
                "uncompress_size": len(new) + len(keep) + len(direct),
                "max_file_size": len(archive),
            }
            info = {"cdn_hosts": ["https://cdn.example/"]}
            payloads = {
                "0.krzip": archive,
                "a.txt": new,
                "folder/direct.bin": direct,
            }

            def fake_download(url, expected_md5, local_path, **kwargs):
                key = next(key for key in payloads if url.endswith(key))
                payload = payloads[key]
                self.assertEqual(md5(payload), expected_md5)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(payload)
                return None

            worker = pgr_launcher.UpdateWorker({"game_dir": str(game_dir)})
            with patch("pgr_launcher.download_file", side_effect=fake_download):
                verified = worker._apply_package_plan(info, plan, document, game_dir)

            self.assertEqual((game_dir / "a.txt").read_bytes(), new)
            self.assertEqual((game_dir / "keep.txt").read_bytes(), keep)
            self.assertEqual((game_dir / "folder/direct.bin").read_bytes(), direct)
            self.assertFalse((game_dir / "obsolete.txt").exists())
            self.assertEqual(verified, {"a.txt", "keep.txt", "folder/direct.bin"})

    def test_resume_does_not_replace_correct_game_file_with_stale_staging(self):
        current = b"good"
        stale = b"evil"
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "game"
            game_dir.mkdir()
            (game_dir / "keep.txt").write_bytes(current)
            (game_dir / "direct.txt").write_bytes(current)
            document = {
                "resource": [
                    {"dest": "0.krzip", "size": 1, "md5": md5(b"z")},
                    {
                        "dest": "direct.txt",
                        "size": len(current),
                        "md5": md5(current),
                    },
                ],
                "zipInfos": [
                    {
                        "dest": "0.krzip",
                        "entries": [
                            {
                                "dest": "keep.txt",
                                "size": len(current),
                                "md5": md5(current),
                            }
                        ],
                    }
                ],
            }
            plan = {
                "kind": "patch",
                "version": "2.0.0",
                "source_version": "1.0.0",
                "base_url": "resources/",
                "index_file_path": "index.json",
                "index_file_md5": None,
                "size": 1,
                "uncompress_size": len(current),
                "max_file_size": 1,
            }
            info = {"cdn_hosts": ["https://cdn.example/"]}
            worker = pgr_launcher.UpdateWorker({"game_dir": str(game_dir)})
            work_dir = worker._prepare_work_dir(game_dir, plan)
            staged = work_dir / "assembled" / "keep.txt"
            staged.parent.mkdir(parents=True)
            staged.write_bytes(stale)
            staged_direct = work_dir / "assembled" / "direct.txt"
            staged_direct.write_bytes(stale)

            with patch(
                "pgr_launcher.download_file",
                side_effect=AssertionError("archive should not be downloaded"),
            ):
                verified = worker._apply_package_plan(info, plan, document, game_dir)

            self.assertEqual((game_dir / "keep.txt").read_bytes(), current)
            self.assertEqual((game_dir / "direct.txt").read_bytes(), current)
            self.assertFalse(staged.exists())
            self.assertFalse(staged_direct.exists())
            self.assertEqual(verified, {"keep.txt", "direct.txt"})


if __name__ == "__main__":
    unittest.main()
