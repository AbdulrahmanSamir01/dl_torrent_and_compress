"""Upload large files to VikingFile using its multipart upload API."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import requests


class VikingFileUploadError(RuntimeError):
    """Raised when a VikingFile upload fails or returns an invalid response."""


class VikingFileUploader:
    """VikingFile multipart uploader; supports account hash or anonymous uploads."""

    API_BASE = "https://vikingfile.com/api"

    def __init__(
        self,
        user_hash: str = "",
        timeout: tuple[int, int] = (30, 300),
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        self.user_hash = user_hash.strip()
        self.timeout = timeout
        self.chunk_size = chunk_size
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "TorrentPipeline/1.0"})

    @staticmethod
    def _json_response(response: requests.Response, operation: str) -> dict:
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise VikingFileUploadError(f"VikingFile {operation} request failed: {exc}") from exc
        except ValueError as exc:
            raise VikingFileUploadError(f"VikingFile {operation} returned invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise VikingFileUploadError(f"VikingFile {operation} returned an unexpected response.")
        if payload.get("error") and payload.get("error") != "success":
            raise VikingFileUploadError(f"VikingFile {operation} failed: {payload['error']}")
        return payload

    @staticmethod
    def _validate_upload_url(url: str) -> str:
        """Require HTTPS URLs returned by the official API before sending file data."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise VikingFileUploadError("VikingFile returned an invalid or non-HTTPS part URL.")
        return url

    class _PartStream:
        """Length-aware streaming view over one part of an already-open file."""

        def __init__(self, file_handle, length: int, chunk_size: int) -> None:
            self.file_handle = file_handle
            self.remaining = length
            self.length = length
            self.chunk_size = chunk_size

        def __len__(self) -> int:
            # Lets requests set Content-Length instead of chunked transfer encoding.
            return self.length

        def __iter__(self):
            while self.remaining:
                chunk = self.file_handle.read(min(self.chunk_size, self.remaining))
                if not chunk:
                    raise VikingFileUploadError(
                        "Unexpected end of file while preparing an upload part."
                    )
                self.remaining -= len(chunk)
                yield chunk

    def upload(self, file_path: str | Path, remote_path: str | None = None) -> dict:
        """Upload a file and return VikingFile's name, size, hash, and download URL."""
        path = Path(file_path).expanduser().resolve()
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Upload file does not exist or is not a regular file: {path}")

        file_size = path.stat().st_size
        if file_size <= 0:
            raise ValueError("Cannot upload an empty file.")

        print(f"[INFO] Requesting VikingFile upload session for {path.name}...")
        try:
            response = self.session.post(
                f"{self.API_BASE}/get-upload-url",
                data={"size": str(file_size)},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise VikingFileUploadError(f"Could not request an upload session: {exc}") from exc

        session = self._json_response(response, "get-upload-url")
        upload_id = session.get("uploadId")
        key = session.get("key")
        urls = session.get("urls")
        part_size = session.get("partSize")
        number_parts = session.get("numberParts")

        if not upload_id or not key or not isinstance(urls, list) or not urls:
            raise VikingFileUploadError("VikingFile upload session is missing required fields.")
        try:
            part_size = int(part_size)
            number_parts = int(number_parts)
        except (TypeError, ValueError) as exc:
            raise VikingFileUploadError("VikingFile returned invalid multipart settings.") from exc

        expected_parts = math.ceil(file_size / part_size) if part_size > 0 else 0
        if part_size <= 0 or number_parts != expected_parts or len(urls) != number_parts:
            raise VikingFileUploadError("VikingFile returned inconsistent multipart settings.")

        completed_parts: list[tuple[int, str]] = []
        with path.open("rb") as file_handle:
            for part_number, upload_url in enumerate(urls, start=1):
                part_length = min(part_size, file_size - (part_number - 1) * part_size)
                safe_url = self._validate_upload_url(upload_url)
                print(f"[INFO] Uploading part {part_number}/{number_parts}...")
                try:
                    part_response = self.session.put(
                        safe_url,
                        data=self._PartStream(file_handle, part_length, self.chunk_size),
                        timeout=self.timeout,
                    )
                    part_response.raise_for_status()
                except requests.RequestException as exc:
                    raise VikingFileUploadError(
                        f"Upload part {part_number} failed: {exc}"
                    ) from exc

                etag = part_response.headers.get("ETag")
                if not etag:
                    raise VikingFileUploadError(
                        f"Upload part {part_number} did not return an ETag."
                    )
                completed_parts.append((part_number, etag))

        complete_data = {
            "key": key,
            "uploadId": upload_id,
            "name": path.name,
            "user": self.user_hash,
        }
        if remote_path:
            complete_data["path"] = remote_path.strip("/")
        for index, (part_number, etag) in enumerate(completed_parts):
            complete_data[f"parts[{index}][PartNumber]"] = str(part_number)
            complete_data[f"parts[{index}][ETag]"] = etag

        print("[INFO] Finalizing VikingFile upload...")
        try:
            complete_response = self.session.post(
                f"{self.API_BASE}/complete-upload",
                data=complete_data,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise VikingFileUploadError(f"Could not finalize upload: {exc}") from exc

        result = self._json_response(complete_response, "complete-upload")
        file_url = result.get("url")
        if not file_url or urlparse(file_url).scheme != "https":
            raise VikingFileUploadError("VikingFile did not return a valid HTTPS file URL.")

        print("[INFO] Upload completed successfully.")
        return result
