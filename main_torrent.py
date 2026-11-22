"""CLI entry point for the torrent download/compress/upload pipeline."""

from __future__ import annotations

import argparse
import sys

from pipeline_torrent import TorrentPipeline


def parse_args() -> argparse.Namespace:
    """Define and parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Download an authorized torrent, create a ZIP archive, and upload it to VikingFile."
    )
    parser.add_argument("--source", required=True, help="Local .torrent file, magnet URI, or HTTP(S) .torrent URL")
    parser.add_argument("--work-dir", default="workspace_torrent", help="Working directory")
    parser.add_argument("--archive-name", default="torrent_download", help="Output ZIP filename without or with .zip")
    parser.add_argument("--compression-level", type=int, choices=range(0, 10), default=6)
    parser.add_argument("--timeout-hours", type=float, default=6.0, help="Maximum torrent download time; 0 disables timeout")
    parser.add_argument("--aria2-path", default="aria2c", help="Path to aria2c executable")
    parser.add_argument("--max-download-rate", default=None, help="Optional aria2 rate limit, e.g. 5M")
    parser.add_argument("--remote-path", default=None, help="Optional destination folder in VikingFile")
    parser.add_argument(
        "--confirm-rights",
        action="store_true",
        help="Confirm that you have the right to download and share this torrent content.",
    )
    return parser.parse_args()


def main() -> int:
    """Validate user intent, run the pipeline, and return a process exit code."""
    args = parse_args()
    if not args.confirm_rights:
        print("ERROR: Confirm that you have the right to download and share this content using --confirm-rights.", file=sys.stderr)
        return 2
    if args.timeout_hours < 0:
        print("ERROR: --timeout-hours cannot be negative.", file=sys.stderr)
        return 2

    timeout_seconds = int(args.timeout_hours * 3600) if args.timeout_hours else 0
    pipeline = TorrentPipeline(
        work_dir=args.work_dir,
        aria2_path=args.aria2_path,
        timeout_seconds=timeout_seconds,
        max_download_rate=args.max_download_rate,
    )
    try:
        pipeline.run(
            source=args.source,
            archive_name=args.archive_name,
            compression_level=args.compression_level,
            remote_path=args.remote_path,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
