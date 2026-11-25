"""Main entry point for downloading, compressing, archiving, uploading, and emailing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from archive_videos_to_zip import ArchiveError, archive_videos_to_zip
from email_digest_sender import EmailDigestError, send_records_email
from gofile_uploader import GofileUploader, GofileUploaderError
from media_processor import MediaProcessError, process_media_tree
from torrent_fetcher import TorrentFetchError, download_torrent


def _resolve_source(source: str | None = None) -> str:
    """Resolve torrent source from explicit argument, CLI argv[1], or TORRENT_SOURCE env."""
    if source is not None and str(source).strip():
        return str(source).strip()

    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()

    env_source = os.getenv("TORRENT_SOURCE", "magnet:?xt=urn:btih:E0967BA97154D64A8D274F02BB10D57CA3ADA5E6&dn=Spider-Man%3A%20Brand%20New%20Day%20%282026%29%20%5B1080p%5D%20%5Bweb%5D%20%5BYTS%5D&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce&tr=udp%3A%2F%2Fopen.demonii.com%3A1337%2Fannounce&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce&tr=udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce&tr=udp%3A%2F%2Fexodus.desync.com%3A6969%2Fannounce&tr=udp%3A%2F%2Ftracker.dler.org%3A6969%2Fannounce&tr=udp%3A%2F%2Fexplodie.org%3A6969%2Fannounce&tr=https%3A%2F%2Ftracker.moeblog.cn%3A443%2Fannounce&tr=udp%3A%2F%2Fopen.dstud.io%3A6969%2Fannounce&tr=udp%3A%2F%2Ftracker.srv00.com%3A6969%2Fannounce&tr=https%3A%2F%2Ftracker.zhuqiy.com%3A443%2Fannounce&tr=https%3A%2F%2Ftracker.pmman.tech%3A443%2Fannounce").strip()
    if env_source:
        return env_source

    raise ValueError("Torrent source is required via CLI argument (python main.py <source>).")


def run_pipeline(source: str | None = None) -> int:
    """Run the pipeline stages sequentially with error handling per step."""
    # Step 1: Validate torrent source before starting pipeline stages.
    try:
        torrent_source = _resolve_source(source)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 1

    raw_dir = Path("workspace/01_raw")
    compress_dir = Path("workspace/02_compress")
    zip_dir = Path("workspace/03_zip")
    records_dir = Path("workspace/04_zips_urls")

    try:
        # Step 2: Download torrent.
        print("[INFO] Stage 1/5: Downloading torrent...")
        download_torrent(torrent_source, raw_dir)

        # Step 3: Compress videos and extract subtitles.
        print("[INFO] Stage 2/5: Processing media tree...")
        process_media_tree(raw_dir, compress_dir)

        # Step 4: Archive videos and subtitles into ZIP files.
        print("[INFO] Stage 3/5: Archiving videos into ZIPs...")
        archive_videos_to_zip(compress_dir, zip_dir)

        # Step 5: Upload ZIP files to Gofile and write .txt records.
        print("[INFO] Stage 4/5: Uploading ZIP files to Gofile...")
        uploader = GofileUploader()
        upload_results = uploader.upload_zip_directory(
            directory_path=str(zip_dir),
            records_dir=records_dir,
        )
        failed = [r for r in upload_results if r.get("status") == "failed"]
        if failed:
            raise GofileUploaderError(f"{len(failed)} ZIP upload(s) failed.")

        # Step 6: Send HTML email digest for unsent .txt records.
        print("[INFO] Stage 5/5: Sending email digest...")
        summary = send_records_email(
            records_dir=records_dir,
            subject="روابط الملفات المرفوعة الجديدة",
        )
        print(f"[INFO] Sent: {summary['sent_count']} | Skipped: {summary['skipped_count']}")
        return 0

    except (OSError, ValueError, TorrentFetchError, MediaProcessError, ArchiveError, GofileUploaderError, EmailDigestError) as exc:
        print(f"[ERROR] Pipeline failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run_pipeline())