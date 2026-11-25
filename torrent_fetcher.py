""""Module responsible for downloading authorized torrents via aria2c with state caching."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class TorrentFetchError(RuntimeError):
    """Raised when torrent download fails or times out."""


def _extract_torrent_id(source: str) -> str:
    """Derive a deterministic torrent ID (btih hash if magnet, else source sha256)."""
    clean_src = source.strip()
    parsed = urlparse(clean_src)

    # Extract BitTorrent v1 info-hash from magnet xt=urn:btih:<hash> when available.
    if parsed.scheme == "magnet":
        xt_values = parse_qs(parsed.query).get("xt", [])
        for xt in xt_values:
            match = re.search(r"urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", xt.strip())
            if match:
                return match.group(1).lower()

    # Fallback deterministic identifier for local .torrent files or HTTP(S) URLs.
    return hashlib.sha256(clean_src.encode("utf-8")).hexdigest()[:16]


def _collect_completed_files(base_dir: Path) -> list[Path]:
    """Collect valid non-symlink files excluding incomplete .aria2 control files."""
    files = [
        p
        for p in base_dir.rglob("*")
        if p.is_file() and not p.is_symlink() and not p.name.endswith(".aria2")
    ]
    return sorted(files)


def _has_incomplete_aria2_files(base_dir: Path) -> bool:
    """Return True if any unfinished .aria2 metadata/control file exists in base_dir."""
    return any(p.is_file() and p.name.endswith(".aria2") for p in base_dir.rglob("*"))


class TorrentFetcher:
    """Wrapper around aria2c CLI with per-torrent JSON state verification."""

    def __init__(
        self,
        download_dir: str | Path,
        state_dir: str | Path | None = None,
        aria2_bin: str = "aria2c",
        timeout_seconds: int = 21600,
        max_download_rate: str | None = None,
    ) -> None:
        """Initialize download paths, separate JSON state directory, and execution limits."""
        self.download_dir = Path(download_dir).resolve()
        default_state = self.download_dir.parent / "torrent_states"
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else default_state
        self.aria2_bin = aria2_bin
        self.timeout_seconds = timeout_seconds
        self.max_download_rate = max_download_rate

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _validate_source(self, source: str) -> str:
        """Validate magnet URI, HTTP(S) URL, or existing local .torrent path."""
        clean_src = source.strip()
        if not clean_src:
            raise ValueError("Torrent source cannot be empty.")

        local_file = Path(clean_src).expanduser()
        if local_file.is_file():
            if local_file.suffix.lower() != ".torrent":
                raise ValueError("Local file must have a .torrent extension.")
            return str(local_file.resolve())

        parsed = urlparse(clean_src)
        if parsed.scheme == "magnet" and "xt=urn:btih:" in clean_src.lower():
            return clean_src
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return clean_src

        raise ValueError("Invalid source: provide a magnet link, HTTP(S) URL, or .torrent path.")

    def _state_file_path(self, torrent_id: str) -> Path:
        """Return path to the dedicated JSON manifest file inside state_dir."""
        return self.state_dir / f"{torrent_id}.json"

    def _build_file_records(self, files: list[Path]) -> list[dict[str, int | str]]:
        """Build detailed file entries with sizes measured in bytes."""
        records: list[dict[str, int | str]] = []
        for file_path in files:
            resolved = file_path.resolve()
            stat_info = resolved.stat()
            records.append(
                {
                    "name": resolved.name,
                    "relative_path": resolved.relative_to(self.download_dir).as_posix(),
                    "full_path": str(resolved),
                    "size_bytes": int(stat_info.st_size),
                }
            )
        return records

    def _try_skip_if_completed(self, torrent_id: str, validated_source: str) -> list[Path] | None:
        """Return file list if JSON manifest matches and all files exist with exact byte sizes."""
        state_path = self._state_file_path(torrent_id)
        if not state_path.is_file():
            return None

        if _has_incomplete_aria2_files(self.download_dir):
            return None

        try:
            # Step 1: Parse JSON manifest and verify torrent identity and source match.
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if payload.get("torrent_id") != torrent_id:
                return None
            if payload.get("source") != validated_source:
                return None

            recorded_files = payload.get("files")
            if not isinstance(recorded_files, list) or not recorded_files:
                return None

            # Step 2: Confirm every recorded file exists, is not a symlink, and matches byte size.
            verified_paths: list[Path] = []
            total_recorded_bytes = 0

            for entry in recorded_files:
                rel_path = str(entry["relative_path"])
                expected_bytes = int(entry["size_bytes"])
                candidate = (self.download_dir / rel_path).resolve()

                if not candidate.is_file() or candidate.is_symlink():
                    return None
                if candidate.stat().st_size != expected_bytes:
                    return None

                total_recorded_bytes += expected_bytes
                verified_paths.append(candidate)

            if int(payload.get("total_size_bytes", -1)) != total_recorded_bytes:
                return None

            return sorted(verified_paths)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # Ignore corrupted state file and proceed with aria2c verification/download.
            return None

    def _write_state_manifest(
        self,
        torrent_id: str,
        validated_source: str,
        files: list[Path],
    ) -> None:
        """Write detailed torrent download metadata with byte-level sizes to state_dir."""
        file_records = self._build_file_records(files)
        total_size_bytes = sum(int(item["size_bytes"]) for item in file_records)
        state_path = self._state_file_path(torrent_id)

        manifest = {
            "torrent_id": torrent_id,
            "source": validated_source,
            "download_dir": str(self.download_dir),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "file_count": len(file_records),
            "total_size_bytes": total_size_bytes,
            "files": file_records,
        }

        try:
            # Write atomically via temp file in state_dir to avoid partial JSON writes.
            tmp_path = state_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(state_path)
        except OSError as exc:
            print(f"[WARN] Failed to write torrent state JSON '{state_path}': {exc}")

    def fetch(self, source: str) -> list[Path]:
        """Skip if already downloaded; otherwise run aria2c and save JSON state."""
        # Step 1: Validate source and compute deterministic torrent ID.
        validated_source = self._validate_source(source)
        torrent_id = _extract_torrent_id(validated_source)

        # Step 2: Fast local skip check against dedicated JSON manifest in state_dir.
        cached_files = self._try_skip_if_completed(torrent_id, validated_source)
        if cached_files is not None:
            print(f"[INFO] Skipping download; torrent '{torrent_id}' already completed.")
            return cached_files

        # Step 3: Verify aria2c binary exists in PATH before spawning subprocess.
        executable = shutil.which(self.aria2_bin)
        if not executable:
            raise TorrentFetchError(f"aria2c binary not found: {self.aria2_bin}")

        # Step 4: Build deterministic aria2c CLI arguments.
        cmd = [
            executable,
            "--dir",
            str(self.download_dir),
            "--continue=true",
            "--check-integrity=true",
            "--follow-torrent=true",
            "--seed-time=0",
            "--seed-ratio=0.0",
            "--summary-interval=60",
            "--console-log-level=notice",
            "--file-allocation=none",
            validated_source,
        ]
        if self.max_download_rate:
            cmd.extend(["--max-download-limit", self.max_download_rate])

        # Step 5: Execute aria2c with timeout and exit-code error handling.
        print(f"[INFO] Starting torrent download ({torrent_id}) into: {self.download_dir}")
        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=self.timeout_seconds if self.timeout_seconds > 0 else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise TorrentFetchError(f"Download exceeded timeout ({self.timeout_seconds}s).") from exc
        except subprocess.CalledProcessError as exc:
            raise TorrentFetchError(f"aria2c exited with error code {exc.returncode}.") from exc

        # Step 6: Collect completed files and persist detailed JSON manifest in state_dir.
        files = _collect_completed_files(self.download_dir)
        if not files:
            raise TorrentFetchError("Download finished but no valid files were produced.")

        self._write_state_manifest(torrent_id, validated_source, files)
        print(f"[INFO] Download complete. Retrieved {len(files)} file(s).")
        return files


def download_torrent(
    source: str | Path,
    download_dir: str | Path,
    state_dir: str | Path | None = None,
) -> list[Path]:
    """Run a single-call torrent download with JSON state skip support."""
    src_str = str(source).strip() if source is not None else ""
    if not src_str:
        raise ValueError("Torrent source must not be empty.")
    if not str(download_dir).strip():
        raise ValueError("Download directory must not be empty.")

    try:
        fetcher = TorrentFetcher(download_dir=download_dir, state_dir=state_dir)
        return fetcher.fetch(src_str)
    except (OSError, ValueError, TorrentFetchError) as exc:
        print(f"[ERROR] Torrent download failed: {exc}")
        raise TorrentFetchError(f"Failed to download torrent '{src_str}': {exc}") from exc