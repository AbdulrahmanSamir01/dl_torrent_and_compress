"""Module for multipart streaming uploads to VikingFile."""

from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import urlparse

import requests


class VikingUploadError(RuntimeError):
    """Raised when VikingFile API calls or part uploads fail."""


class _StreamSlice:
    """Memory-efficient file reader yielding bounded chunks for a single multipart segment."""

    def __init__(self, handle, length: int, chunk_size: int) -> None:
        self.handle = handle
        self.remaining = length
        self.length = length
        self.chunk_size = chunk_size

    def __len__(self) -> int:
        return self.length

    def __iter__(self):
        while self.remaining > 0:
            data = self.handle.read(min(self.chunk_size, self.remaining))
            if not data:
                raise VikingUploadError("Unexpected EOF while streaming upload part.")
            self.remaining -= len(data)
            yield data


class VikingUploader:
    """Client for VikingFile's S3-compatible multipart upload API."""

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
        self.session.headers.update({"User-Agent": "FastMediaTorrentPipeline/1.0"})

    def _parse_json(self, response: requests.Response, step: str) -> dict:
        """Safely parse and validate JSON responses from VikingFile."""
        try:
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise VikingUploadError(f"VikingFile [{step}] failed: {exc}") from exc

        if not isinstance(data, dict):
            raise VikingUploadError(f"VikingFile [{step}] returned non-dictionary JSON.")
        if data.get("error") and data.get("error") != "success":
            raise VikingUploadError(f"VikingFile [{step}] API error: {data['error']}")
        return data

    def upload(self, file_path: str | Path, remote_folder: str | None = None) -> dict:
        """Upload a file in parts and return the final metadata dictionary."""
        target = Path(file_path).resolve()
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError(f"Invalid file for upload: {target}")

        file_size = target.stat().st_size
        if file_size == 0:
            raise ValueError("Cannot upload an empty file.")

        print(f"[INFO] Initializing VikingFile upload for: {target.name} ({file_size / (1024*1024):.2f} MiB)")
        resp = self.session.post(
            f"{self.API_BASE}/get-upload-url",
            data={"size": str(file_size)},
            timeout=self.timeout,
        )
        session_info = self._parse_json(resp, "get-upload-url")

        upload_id = session_info.get("uploadId")
        key = session_info.get("key")
        urls = session_info.get("urls", [])
        part_size = int(session_info.get("partSize", 0))
        num_parts = int(session_info.get("numberParts", 0))

        if not upload_id or not key or part_size <= 0 or len(urls) != num_parts:
            raise VikingUploadError("Incomplete or inconsistent multipart parameters from VikingFile.")

        etags: list[tuple[int, str]] = []
        with target.open("rb") as fh:
            for part_idx, part_url in enumerate(urls, start=1):
                parsed = urlparse(part_url)
                if parsed.scheme != "https":
                    raise VikingUploadError("Insecure non-HTTPS upload URL rejected.")

                bytes_to_send = min(part_size, file_size - (part_idx - 1) * part_size)
                print(f"[INFO] Uploading part {part_idx}/{num_parts}...")
                put_resp = self.session.put(
                    part_url,
                    data=_StreamSlice(fh, bytes_to_send, self.chunk_size),
                    timeout=self.timeout,
                )
                put_resp.raise_for_status()

                etag = put_resp.headers.get("ETag")
                if not etag:
                    raise VikingUploadError(f"Missing ETag header on part {part_idx}.")
                etags.append((part_idx, etag))

        payload = {
            "key": key,
            "uploadId": upload_id,
            "name": target.name,
            "user": self.user_hash,
        }
        if remote_folder:
            payload["path"] = remote_folder.strip("/")
        for i, (p_num, etag) in enumerate(etags):
            payload[f"parts[{i}][PartNumber]"] = str(p_num)
            payload[f"parts[{i}][ETag]"] = etag

        print("[INFO] Completing VikingFile upload...")
        done_resp = self.session.post(
            f"{self.API_BASE}/complete-upload",
            data=payload,
            timeout=self.timeout,
        )
        result = self._parse_json(done_resp, "complete-upload")
        if not str(result.get("url", "")).startswith("https://"):
            raise VikingUploadError("VikingFile did not return a valid HTTPS download link.")

        print(f"[INFO] Upload succeeded: {result['url']}")
        return result
