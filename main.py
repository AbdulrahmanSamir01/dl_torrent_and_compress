"""CLI entry point supporting a configurable torrent variable without requiring --source."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path

from media_processor import MediaProcessor
from torrent_fetcher import TorrentFetcher
from viking_uploader import VikingUploader

# Set a default torrent variable here or via TORRENT_SOURCE env var when --source is omitted.
DEFAULT_TORRENT_SOURCE: str = "magnet:?xt=urn:btih:ccd11275672a908fe9b7aaa6ce4e5783c7b352eb&dn=Trigger.2025.S01.1080p.ENG.ITA.KOR.H265-TheBlackKing&xl=13418455796&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337&tr=udp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce&tr=udp%3A%2F%2Ftracker.torrent.eu.org%3A451%2Fannounce&tr=udp%3A%2F%2Ftracker.bittor.pw%3A1337%2Fannounce&tr=udp%3A%2F%2Fpublic.popcorn-tracker.org%3A6969%2Fannounce&tr=udp%3A%2F%2Ftracker.dler.org%3A6969%2Fannounce&tr=udp%3A%2F%2Fexodus.desync.com%3A6969&tr=udp%3A%2F%2Fopen.demonii.com%3A1337%2Fannounce&tr=udp%3A%2F%2Fglotorrents.pw%3A6969%2Fannounce&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969&tr=udp%3A%2F%2Ftorrent.gresille.org%3A80%2Fannounce&tr=udp%3A%2F%2Fp4p.arenabg.com%3A1337&tr=udp%3A%2F%2Ftracker.internetwarriors.net%3A1337"


def sanitize_name(name: str) -> str:
    """Sanitize folder or archive name to prevent path traversal or invalid characters."""
    # Remove path segments and keep only safe ASCII characters
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).stem.strip())
    if not cleaned or cleaned in {".", ".."}:
        return "processed_media"
    return cleaned


def resolve_torrent_source(cli_source: str | None) -> str:
    """Resolve torrent source from CLI argument, environment variable, or module variable."""
    # Prefer explicit CLI input, then environment variable, then module-level default
    for candidate in (cli_source, os.environ.get("TORRENT_SOURCE"), DEFAULT_TORRENT_SOURCE):
        if candidate and candidate.strip():
            return candidate.strip()
    raise ValueError("No torrent source provided. Set DEFAULT_TORRENT_SOURCE, TORRENT_SOURCE env, or --source.")


def collect_files(directory: Path) -> list[Path]:
    """Collect regular non-symlink files from a directory while ignoring incomplete aria2 files."""
    # Ensure directory exists before listing files
    if not directory.is_dir():
        raise FileNotFoundError(f"Directory does not exist: {directory}")

    # Filter out symlinks and temporary .aria2 metadata files
    return [
        item for item in sorted(directory.rglob("*"))
        if item.is_file() and not item.is_symlink() and not item.name.endswith(".aria2")
    ]


def write_github_summary(summary: dict) -> None:
    """Append pipeline summary to GITHUB_STEP_SUMMARY when running inside GitHub Actions."""
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not gh_summary:
        return

    # Write markdown summary safely and warn if writing fails
    try:
        with open(gh_summary, "a", encoding="utf-8") as fh:
            fh.write("## Media Pipeline Completed\n\n")
            fh.write(f"- **Archives Created:** `{summary['archive_count']}`\n")
            fh.write(f"- **Total Size:** `{summary['total_size_mib']} MiB`\n")
            fh.write(f"- **Download Links:** {', '.join(summary['vikingfile_urls']) or 'N/A'}\n")
    except OSError as exc:
        print(f"[WARN] Could not write GitHub step summary: {exc}", file=sys.stderr)


def step_download(args: argparse.Namespace, source: str, raw_dir: Path) -> list[Path]:
    """Step 1: Download torrent content into raw_dir using aria2c."""
    # Configure fetcher with timeout and optional rate limit, then execute download
    fetcher = TorrentFetcher(
        download_dir=raw_dir,
        timeout_seconds=int(args.timeout_hours * 3600),
        max_download_rate=args.max_download_rate,
    )
    return fetcher.fetch(source)


def step_process_media(args: argparse.Namespace, raw_dir: Path, processed_dir: Path) -> Path:
    """Step 2: Compress videos and extract subtitles from raw_dir into processed_dir."""
    # Read downloaded files from disk so this step can run standalone
    source_files = collect_files(raw_dir)
    if not source_files:
        raise FileNotFoundError(f"No downloaded files found in {raw_dir} to process.")

    processor = MediaProcessor(
        output_dir=processed_dir,
        codec=args.codec,
        preset=args.preset,
        crf=args.crf,
    )
    return processor.process_files(source_files)


def write_folder_zip(folder_path: Path, bundle_dir: Path) -> Path | None:
    """Zip all files inside a single directory into a ZIP file named after the folder."""
    # Collect valid files inside this folder and skip empty folders
    folder_files = collect_files(folder_path)
    if not folder_files:
        print(f"[WARN] Skipping empty folder: {folder_path.name}")
        return None

    archive_path = bundle_dir / f"{sanitize_name(folder_path.name)}.zip"
    print(f"[INFO] Packaging folder '{folder_path.name}' ({len(folder_files)} file(s)) into: {archive_path.name}")

    # Store files without re-compressing already encoded media
    with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for item in folder_files:
            zf.write(item, arcname=item.relative_to(folder_path))

    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise RuntimeError(f"Generated archive is empty or missing: {archive_path}")

    return archive_path


def step_bundle_archive(args: argparse.Namespace, processed_dir: Path, bundle_dir: Path) -> list[Path]:
    """Step 3: Zip each folder inside processed_dir into its own ZIP archive in bundle_dir."""
    if not processed_dir.is_dir():
        raise FileNotFoundError(f"Processed directory does not exist: {processed_dir}")

    bundle_dir.mkdir(parents=True, exist_ok=True)
    archives: list[Path] = []

    # Zip every direct subfolder using that subfolder's name
    subfolders = [entry for entry in sorted(processed_dir.iterdir()) if entry.is_dir() and not entry.is_symlink()]
    for folder in subfolders:
        archive_path = write_folder_zip(folder, bundle_dir)
        if archive_path is not None:
            archives.append(archive_path)

    # Fallback: if processed_dir contains loose files directly with no subfolders, zip them once
    if not archives:
        loose_files = [entry for entry in sorted(processed_dir.iterdir()) if entry.is_file() and not entry.is_symlink() and not entry.name.endswith(".aria2")]
        if not loose_files:
            raise FileNotFoundError(f"No folders or files found in {processed_dir} to bundle.")

        fallback_name = sanitize_name(args.archive_name or processed_dir.name)
        fallback_path = bundle_dir / f"{fallback_name}.zip"
        print(f"[INFO] Packaging {len(loose_files)} loose file(s) into: {fallback_path.name}")
        with zipfile.ZipFile(fallback_path, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for item in loose_files:
                zf.write(item, arcname=item.name)

        if not fallback_path.is_file() or fallback_path.stat().st_size == 0:
            raise RuntimeError(f"Generated archive is empty or missing: {fallback_path}")
        archives.append(fallback_path)

    return archives


def collect_upload_files(target_dir: Path) -> list[Path]:
    """Collect regular non-symlink files inside a directory for upload."""
    # Ensure directory exists before scanning files
    if not target_dir.is_dir():
        raise FileNotFoundError(f"Upload directory does not exist: {target_dir}")

    # Return regular non-symlink files sorted deterministically
    return [
        item for item in sorted(target_dir.rglob("*"))
        if item.is_file() and not item.is_symlink() and not item.name.endswith(".aria2")
    ]


def step_upload_vikingfile(args: argparse.Namespace, upload_dir: Path) -> list[dict]:
    """Step 4: Upload all existing files inside upload_dir to VikingFile."""
    # Gather every valid file in the directory instead of expecting a fixed archive name
    files_to_upload = collect_upload_files(upload_dir)
    if not files_to_upload:
        raise FileNotFoundError(f"No files found to upload in: {upload_dir}")

    uploader = VikingUploader(user_hash=os.environ.get("VIKINGFILE_USER_HASH", "I4FpE4pGTi"))
    results: list[dict] = []

    # Upload each file sequentially and collect its response metadata
    for file_path in files_to_upload:
        results.append(uploader.upload(file_path, remote_folder=args.remote_folder))

    return results


def extract_vikingfile_urls(upload_meta: list[dict] | dict | None) -> list[str]:
    """Safely extract all available URLs from upload metadata."""
    urls: list[str] = []
    # Support both list of upload responses and a single dict response
    if isinstance(upload_meta, list):
        for item in upload_meta:
            if isinstance(item, dict) and item.get("url"):
                urls.append(str(item["url"]))
    elif isinstance(upload_meta, dict) and upload_meta.get("url"):
        urls.append(str(upload_meta["url"]))
    return urls


def parse_args() -> argparse.Namespace:
    """Parse optional CLI arguments so the pipeline can run with defaults/variables."""
    parser = argparse.ArgumentParser(description="Torrent -> FFmpeg Video Compressor -> VikingFile Pipeline")
    parser.add_argument("--source", default=None, help="Optional magnet URI, .torrent path, or HTTP(S) URL")
    parser.add_argument("--work-dir", default="workspace", help="Base working directory")
    parser.add_argument("--archive-name", default="media_bundle", help="Fallback ZIP name if no subfolders exist")
    parser.add_argument("--codec", default="libx264", choices=["libx264", "libx265"], help="FFmpeg video codec")
    parser.add_argument("--preset", default="veryfast", help="FFmpeg encoding speed preset")
    parser.add_argument("--crf", type=int, default=26, help="FFmpeg Constant Rate Factor (18-32)")
    parser.add_argument("--timeout-hours", type=float, default=5.5, help="Max download time in hours")
    parser.add_argument("--max-download-rate", default=None, help="Optional aria2 speed limit (e.g., 10M)")
    parser.add_argument("--remote-folder", default=None, help="Optional destination folder in VikingFile")
    parser.add_argument("--confirm-rights", action="store_true", help="Confirm legal authorization for the torrent")
    return parser.parse_args()


def main() -> int:
    """Run enabled pipeline steps and print final summary."""
    args = parse_args()
    if not args.confirm_rights:
        print("ERROR: You must pass --confirm-rights to verify authorization.", file=sys.stderr)
        return 2

    work_dir = Path(args.work_dir).resolve()
    raw_dir = work_dir / "01_raw"
    processed_dir = work_dir / "02_processed"
    bundle_dir = work_dir / "03_bundle"

    try:
        # Resolve source only when step_download is active
        source = resolve_torrent_source(args.source)

        # Comment out any step below; later steps read directly from their disk folders.
        # step_download(args, source, raw_dir)
        # step_process_media(args, raw_dir, processed_dir)
        archive_paths = step_bundle_archive(args, processed_dir, bundle_dir)
        upload_meta = step_upload_vikingfile(args, bundle_dir)

        total_bytes = sum(path.stat().st_size for path in archive_paths)
        summary = {
            "status": "success",
            "archive_count": len(archive_paths),
            "archives": [path.name for path in archive_paths],
            "total_size_mib": round(total_bytes / (1024 * 1024), 2),
            "vikingfile_urls": extract_vikingfile_urls(upload_meta),
        }
        print(json.dumps(summary, indent=2))
        write_github_summary(summary)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())