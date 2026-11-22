"""Orchestrate torrent download, ZIP creation, and VikingFile upload."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

from torrent_downloader import TorrentDownloader
from vikingfile_uploader import VikingFileUploader


class TorrentPipelineError(RuntimeError):
    """Raised when a torrent pipeline stage cannot be completed."""


class TorrentPipeline:
    """Download authorized torrent content, archive it, then upload the archive."""

    def __init__(
        self,
        work_dir: str = "workspace_torrent",
        aria2_path: str = "aria2c",
        timeout_seconds: int = 21_600,
        vikingfile_user_hash: str | None = None,
        max_download_rate: str | None = None,
    ) -> None:
        self.work_dir = Path(work_dir).resolve()
        self.download_dir = self.work_dir / "01_downloads"
        self.archive_dir = self.work_dir / "02_archives"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        self.downloader = TorrentDownloader(
            download_dir=self.download_dir,
            aria2_path=aria2_path,
            timeout_seconds=timeout_seconds,
            max_download_rate=max_download_rate,
        )
        self.uploader = VikingFileUploader(
            user_hash=vikingfile_user_hash or os.environ.get("VIKINGFILE_USER_HASH", "")
        )

    @staticmethod
    def _safe_archive_name(name: str | None) -> str:
        """Normalize a user-supplied archive name and prevent path traversal."""
        candidate = Path(name or "torrent_download").name.strip()
        if not candidate or candidate in {".", ".."}:
            raise ValueError("Archive name is invalid.")
        if candidate.lower().endswith(".zip"):
            candidate = candidate[:-4]
        if not candidate or any(char in candidate for char in ("/", "\\", "\0")):
            raise ValueError("Archive name is invalid.")
        return f"{candidate}.zip"

    def _create_archive(self, files: list[Path], archive_name: str, compression_level: int) -> Path:
        """Create one ZIP while preserving paths relative to the download directory."""
        if not 0 <= compression_level <= 9:
            raise ValueError("Compression level must be between 0 and 9.")

        archive_path = self.archive_dir / self._safe_archive_name(archive_name)
        compression = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED

        print(f"[INFO] Creating archive: {archive_path.name}")
        try:
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=compression,
                compresslevel=None if compression == zipfile.ZIP_STORED else compression_level,
                allowZip64=True,
            ) as archive:
                for file_path in files:
                    resolved = file_path.resolve()
                    if file_path.is_symlink() or not resolved.is_relative_to(self.download_dir):
                        raise TorrentPipelineError(f"Refusing to archive an unsafe path: {file_path}")
                    if not resolved.is_file():
                        continue
                    archive.write(resolved, arcname=resolved.relative_to(self.download_dir))
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            archive_path.unlink(missing_ok=True)
            raise TorrentPipelineError(f"Could not create ZIP archive: {exc}") from exc

        if not archive_path.is_file() or archive_path.stat().st_size == 0:
            raise TorrentPipelineError("Archive creation produced an empty ZIP file.")
        return archive_path

    def run(
        self,
        source: str,
        archive_name: str | None = None,
        compression_level: int = 6,
        remote_path: str | None = None,
    ) -> dict[str, Any]:
        """Execute download -> archive -> upload and return upload metadata."""
        print("=== [1/3] Torrent download ===")
        downloaded_files = self.downloader.download(source)

        print("=== [2/3] ZIP compression ===")
        archive_path = self._create_archive(
            files=downloaded_files,
            archive_name=archive_name or "torrent_download",
            compression_level=compression_level,
        )
        print(f"[INFO] Archive size: {archive_path.stat().st_size / (1024 * 1024):.2f} MiB")

        print("=== [3/3] VikingFile upload ===")
        upload_result = self.uploader.upload(archive_path, remote_path=remote_path)

        result = {
            "downloaded_file_count": len(downloaded_files),
            "archive_path": str(archive_path),
            "archive_size_bytes": archive_path.stat().st_size,
            "upload": upload_result,
        }
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write("## Torrent pipeline completed\n\n")
                summary.write(f"- Archive: `{archive_path.name}`\n")
                summary.write(f"- Size: `{archive_path.stat().st_size / (1024 * 1024):.2f} MiB`\n")
                summary.write(f"- VikingFile URL: {upload_result['url']}\n")

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
