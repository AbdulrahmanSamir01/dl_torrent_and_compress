"""Client library to upload ZIP files to Gofile.io with .txt proof-based skip support."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests


class GofileUploaderError(Exception):
    """Custom exception for Gofile upload failures."""


def _is_safe_hostname_label(label: str) -> bool:
    """Validate hostname label to prevent host/URL injection in server endpoints."""
    return bool(label and re.fullmatch(r"[A-Za-z0-9-]+", label))


class GofileUploader:
    """Client library to upload ZIP files from a target directory to Gofile.io."""

    DEFAULT_SERVERS_URL = "https://api.gofile.io/servers"

    def __init__(
        self,
        api_token: Optional[str] = None,
        folder_id: Optional[str] = None,
        servers_url: Optional[str] = None,
        timeout: Optional[int] = None,
        records_dir: Optional[str | Path] = None,
    ) -> None:
        """Load configuration from parameters or environment variables and initialize session."""
        # Step 1: Resolve credentials, endpoint URL, timeout, and records directory.
        self.api_token = api_token if api_token is not None else os.getenv("GOFILE_TOKEN")
        self.folder_id = folder_id if folder_id is not None else os.getenv("GOFILE_FOLDER_ID")
        self.servers_url = servers_url or os.getenv("GOFILE_SERVERS_URL", self.DEFAULT_SERVERS_URL)

        env_timeout = os.getenv("GOFILE_TIMEOUT")
        raw_timeout = timeout if timeout is not None else (int(env_timeout) if env_timeout else 120)
        if raw_timeout <= 0:
            raise ValueError("Timeout must be a positive integer in seconds.")
        self.timeout = raw_timeout

        self.records_dir = Path(records_dir).resolve() if records_dir is not None else None

        # Step 2: Configure HTTP session with optional Bearer authorization header.
        self.session = requests.Session()
        if self.api_token:
            self.session.headers.update({"Authorization": f"Bearer {self.api_token}"})

    def get_best_server(self) -> str:
        """Fetch an active upload server name from the configured Gofile endpoint."""
        # Step 1: Request available servers with timeout and HTTP status validation.
        try:
            response = self.session.get(self.servers_url, timeout=min(30, self.timeout))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise GofileUploaderError(f"Failed to fetch best server: {exc}") from exc

        # Step 2: Validate response envelope and extract a safe server hostname label.
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise GofileUploaderError(f"API returned non-ok status: {payload}")

        servers = payload.get("data", {}).get("servers", [])
        if not isinstance(servers, list) or not servers:
            raise GofileUploaderError("No available upload servers found.")

        server_name = str(servers[0].get("name", "")).strip()
        if not _is_safe_hostname_label(server_name):
            raise GofileUploaderError(f"Unsafe or invalid server name received: '{server_name}'")

        return server_name

    def _validate_directory(self, directory_path: str | Path, label: str = "Target") -> Path:
        """Validate that a path exists, is not a symlink, and resolves to a real directory."""
        # Step 1: Reject empty paths early.
        if directory_path is None or not str(directory_path).strip():
            raise ValueError(f"{label} directory path must not be empty.")

        # Step 2: Resolve path and verify it is a non-symlink directory.
        try:
            resolved = Path(directory_path).resolve()
            if resolved.is_symlink() or not resolved.exists():
                raise FileNotFoundError(f"{label} directory does not exist: {resolved}")
            if not resolved.is_dir():
                raise NotADirectoryError(f"Provided path is not a directory: {resolved}")
            return resolved
        except OSError as exc:
            raise GofileUploaderError(f"Failed validating {label.lower()} directory '{directory_path}': {exc}") from exc

    def _resolve_records_dir(self, target_dir: Path, records_dir: Optional[str | Path] = None) -> Path:
        """Determine and prepare the directory where .txt upload proof files are stored."""
        # Step 1: Prefer explicit call argument, then instance records_dir, else default folder.
        chosen = records_dir if records_dir is not None else self.records_dir
        resolved_records = (
            Path(chosen).resolve() if chosen is not None else (target_dir / "upload_records").resolve()
        )

        # Step 2: Ensure records directory exists before reading or writing proof files.
        try:
            resolved_records.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GofileUploaderError(f"Failed creating records directory '{resolved_records}': {exc}") from exc

        return resolved_records

    def _build_record_path(self, zip_file: Path, records_dir: Path) -> Path:
        """Build the .txt proof file path for a ZIP file inside records_dir."""
        safe_name = Path(zip_file.name).name
        return records_dir / f"{Path(safe_name).stem}.txt"

    def _parse_record_file(self, record_path: Path) -> Dict[str, str]:
        """Parse key-value lines from a .txt upload proof file."""
        parsed: Dict[str, str] = {}
        content = record_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                parsed[key.strip()] = value.strip()
        return parsed

    def _should_skip_zip(self, zip_file: Path, record_path: Path) -> Optional[Dict[str, str]]:
        """Return parsed record metadata if a valid .txt proof exists matching ZIP byte size."""
        # Step 1: Check that record file exists, is not a symlink, and is non-empty.
        try:
            if not record_path.exists() or not record_path.is_file() or record_path.is_symlink():
                return None
            if record_path.stat().st_size <= 0:
                return None

            # Step 2: Match recorded size_bytes against current ZIP file size in bytes.
            record_data = self._parse_record_file(record_path)
            actual_size = zip_file.stat().st_size
            recorded_size_raw = record_data.get("size_bytes", "")
            if not recorded_size_raw.isdigit() or int(recorded_size_raw) != actual_size:
                return None

            return record_data
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            print(f"[WARN] Could not validate skip record '{record_path}': {exc}")
            return None

    def _write_upload_record(self, zip_file: Path, record_path: Path, data: Dict) -> None:
        """Atomically write upload details and download link into a .txt proof file."""
        # Step 1: Format human-readable proof fields including byte size and download URL.
        try:
            size_bytes = zip_file.stat().st_size
            download_page = str(data.get("downloadPage", "N/A"))
            file_id = str(data.get("id") or data.get("fileId") or "N/A")
            file_name = str(data.get("name") or zip_file.name)

            lines = [
                f"file_name: {file_name}",
                f"source_zip: {zip_file.name}",
                f"size_bytes: {size_bytes}",
                f"file_id: {file_id}",
                f"download_page: {download_page}",
            ]
            payload_text = "\n".join(lines) + "\n"

            # Step 2: Write via temp file and replace atomically to prevent partial .txt records.
            tmp_record = record_path.with_name(f".{record_path.name}.tmp")
            tmp_record.write_text(payload_text, encoding="utf-8")
            tmp_record.replace(record_path)
        except OSError as exc:
            raise GofileUploaderError(f"Failed writing upload record '{record_path}': {exc}") from exc

    def _find_zip_files(self, target_dir: Path) -> List[Path]:
        """Return sorted non-symlink .zip files inside the validated directory."""
        try:
            return sorted(
                f for f in target_dir.iterdir()
                if f.is_file() and not f.is_symlink() and f.suffix.lower() == ".zip"
            )
        except OSError as exc:
            raise GofileUploaderError(f"Failed listing ZIP files in '{target_dir}': {exc}") from exc

    def upload_single_file(self, file_path: Path, server_name: str) -> Dict:
        """Stream a single ZIP file to the selected Gofile storage server."""
        # Step 1: Validate file path and server hostname before opening file stream.
        if not _is_safe_hostname_label(server_name):
            raise GofileUploaderError(f"Invalid server name: '{server_name}'")

        try:
            resolved_file = file_path.resolve()
            if resolved_file.is_symlink() or not resolved_file.is_file():
                raise GofileUploaderError(f"ZIP file is missing or is a symlink: {file_path}")
        except OSError as exc:
            raise GofileUploaderError(f"Failed accessing ZIP file '{file_path}': {exc}") from exc

        upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
        form_data = {"folderId": self.folder_id} if self.folder_id else {}

        # Step 2: Stream file over HTTPS and verify API response status.
        try:
            with resolved_file.open("rb") as file_handle:
                files = {"file": (resolved_file.name, file_handle, "application/zip")}
                response = self.session.post(upload_url, files=files, data=form_data, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()

            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise GofileUploaderError(f"Upload rejected for {resolved_file.name}: {payload}")

            data = payload.get("data")
            if not isinstance(data, dict):
                raise GofileUploaderError(f"Invalid response payload data for {resolved_file.name}")

            return data
        except (OSError, requests.RequestException, ValueError) as exc:
            raise GofileUploaderError(f"Error uploading {resolved_file.name}: {exc}") from exc

    def upload_zip_directory(
        self,
        directory_path: str | Path,
        records_dir: Optional[str | Path] = None,
    ) -> List[Dict]:
        """Validate directory, skip already-recorded ZIPs via .txt proof, and upload remaining ones."""
        # Step 1: Validate ZIP directory and resolve .txt proof records directory.
        target_dir = self._validate_directory(directory_path, label="Source")
        resolved_records_dir = self._resolve_records_dir(target_dir, records_dir=records_dir)
        zip_files = self._find_zip_files(target_dir)

        if not zip_files:
            print(f"[INFO] No .zip files found in: {target_dir}")
            return []

        # Step 2: Filter files needing upload before querying Gofile servers.
        pending_zips: List[tuple[Path, Path]] = []
        results: List[Dict] = []

        for zip_file in zip_files:
            record_path = self._build_record_path(zip_file, resolved_records_dir)
            existing_record = self._should_skip_zip(zip_file, record_path)
            if existing_record is not None:
                print(f"[INFO] Skipping already uploaded ZIP: {zip_file.name} (record: {record_path})")
                results.append({
                    "file": zip_file.name,
                    "status": "skipped",
                    "record_file": str(record_path),
                    "data": existing_record,
                })
            else:
                pending_zips.append((zip_file, record_path))

        if not pending_zips:
            print("[INFO] All ZIP files already have valid .txt upload records. Nothing to upload.")
            return results

        # Step 3: Fetch upload server once and upload pending ZIP files sequentially.
        server = self.get_best_server()
        print(f"[INFO] Selected server: {server} | Pending ZIP file(s): {len(pending_zips)}")

        for zip_file, record_path in pending_zips:
            try:
                size_bytes = zip_file.stat().st_size
                size_mb = size_bytes / (1024 * 1024)
                print(f"[INFO] Uploading: {zip_file.name} ({size_mb:.2f} MB / {size_bytes} B)...")

                data = self.upload_single_file(zip_file, server)
                self._write_upload_record(zip_file, record_path, data)

                download_page = data.get("downloadPage", "N/A")
                print(f"[SUCCESS] {zip_file.name} -> {download_page}")
                results.append({
                    "file": zip_file.name,
                    "status": "success",
                    "record_file": str(record_path),
                    "data": data,
                })
            except GofileUploaderError as err:
                print(f"[ERROR] {err}")
                results.append({
                    "file": zip_file.name,
                    "status": "failed",
                    "record_file": str(record_path),
                    "error": str(err),
                })

        return results