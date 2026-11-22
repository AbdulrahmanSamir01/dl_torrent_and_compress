"""Download authorized BitTorrent content through the aria2 command-line client."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Union
from urllib.parse import urlparse

TorrentSource = Union[str, Path]


class TorrentDownloadError(RuntimeError):
    """Raised when aria2 cannot complete a torrent download."""


class TorrentDownloader:
    """Small, testable wrapper around aria2c for torrent and magnet downloads."""

    def __init__(
        self,
        download_dir: Union[str, Path],
        aria2_path: str = "aria2c",
        timeout_seconds: int = 21_600,
        max_download_rate: str | None = None,
    ) -> None:
        self.download_dir = Path(download_dir).resolve()
        self.aria2_path = aria2_path
        self.timeout_seconds = timeout_seconds
        self.max_download_rate = max_download_rate
        self.download_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_source(source: TorrentSource) -> str:
        """Accept an existing .torrent file, a magnet URI, or an HTTP(S) torrent URL."""
        value = str(source).strip()
        if not value:
            raise ValueError("Torrent source cannot be empty.")

        local_path = Path(value).expanduser()
        if local_path.is_file():
            if local_path.suffix.lower() != ".torrent":
                raise ValueError("Local torrent source must be a .torrent file.")
            return str(local_path.resolve())

        parsed = urlparse(value)
        if parsed.scheme == "magnet":
            if "xt=urn:btih:" not in value.lower():
                raise ValueError("Magnet URI must contain a BitTorrent info-hash (xt=urn:btih).")
            return value

        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value

        raise ValueError("Source must be a local .torrent file, magnet URI, or HTTP(S) URL.")

    def download(self, source: TorrentSource) -> list[Path]:
        """Run aria2 and return downloaded files after a successful completion."""
        executable = shutil.which(self.aria2_path)
        if executable is None and not Path(self.aria2_path).is_file():
            raise TorrentDownloadError(
                f"aria2 executable was not found: {self.aria2_path}. Install aria2 first."
            )
        executable = executable or self.aria2_path
        validated_source = self._validate_source(source)

        command = [
            executable,
            "--dir", str(self.download_dir),
            "--continue=true",
            "--check-integrity=true",
            "--follow-torrent=true",
            "--seed-time=0",
            "--seed-ratio=0.0",
            "--summary-interval=30",
            "--console-log-level=notice",
            "--download-result=full",
            "--file-allocation=none",
            validated_source,
        ]
        if self.max_download_rate:
            command.extend(["--max-download-limit", self.max_download_rate])

        print("[INFO] Starting aria2 torrent download...")
        try:
            subprocess.run(
                command,
                check=True,
                timeout=self.timeout_seconds if self.timeout_seconds > 0 else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise TorrentDownloadError(
                f"Torrent download exceeded the {self.timeout_seconds}-second timeout."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise TorrentDownloadError(
                f"aria2 failed with exit code {exc.returncode}. Check the aria2 output above."
            ) from exc
        except OSError as exc:
            raise TorrentDownloadError(f"Could not start aria2: {exc}") from exc

        downloaded_files = [
            path for path in self.download_dir.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not path.name.endswith(".aria2")
        ]
        if not downloaded_files:
            raise TorrentDownloadError("aria2 finished, but no downloaded content files were found.")

        print(f"[INFO] Download completed. Found {len(downloaded_files)} file(s).")
        return sorted(downloaded_files)
