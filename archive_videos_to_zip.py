"""Module for archiving compressed videos and matching subtitle folders into ZIP files."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


class ArchiveError(RuntimeError):
    """Raised when archiving videos into ZIP files fails."""


class VideoZipArchiver:
    """Bundle each video and its matching subtitle folder into a video-named folder inside a ZIP."""

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}

    def __init__(
        self,
        source_dir: str | Path,
        output_dir: str | Path,
        password: str | None = None,
    ) -> None:
        """Validate and store source directory, ZIP output directory, and optional password."""
        # Step 1: Resolve paths and normalize optional password.
        self.source_dir = Path(source_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.password = password if password else None

        # Step 2: Verify source exists and ensure output directory is ready.
        if not self.source_dir.is_dir():
            raise ArchiveError(f"Source directory does not exist: {self.source_dir}")
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArchiveError(f"Failed to create output directory '{self.output_dir}': {exc}") from exc

    def _build_target_zip_path(self, video_path: Path) -> Path:
        """Return target ZIP path directly inside output_dir using the video stem."""
        # Step 1: Place every archive directly at the root of output_dir.
        return self.output_dir / f"{video_path.stem}.zip"

    def _should_skip_zip(self, zip_path: Path) -> bool:
        """Return True if the target ZIP file already exists in the output path."""
        # Step 1: Safely check whether output ZIP already exists as a non-symlink file.
        try:
            return zip_path.exists() and zip_path.is_file() and not zip_path.is_symlink()
        except OSError as exc:
            raise ArchiveError(f"Failed checking existing archive '{zip_path}': {exc}") from exc

    def _collect_items_for_video(self, video_path: Path) -> list[Path]:
        """Collect the video file and its matching same-named subtitle directory if present."""
        # Step 1: Always include the target video file.
        items: list[Path] = [video_path]

        # Step 2: Locate matching folder with the exact stem in the same parent directory.
        matching_folder = video_path.parent / video_path.stem
        try:
            if matching_folder.is_dir() and not matching_folder.is_symlink():
                items.append(matching_folder)
        except OSError as exc:
            raise ArchiveError(f"Failed inspecting subtitle folder '{matching_folder}': {exc}") from exc

        return items

    def _build_archive_member_name(self, item: Path, file_path: Path, bundle_name: str) -> str:
        """Build archive entry path inside '<bundle_name>/' preserving relative layout."""
        # Step 1: Map files under '<bundle_name>/<file_name>' or '<bundle_name>/<sub_rel>'.
        if item.is_file():
            return str(Path(bundle_name) / file_path.name)
        rel_inside_folder = file_path.relative_to(item)
        return str(Path(bundle_name) / item.name / rel_inside_folder)

    def _stage_bundle_items(self, stage_root: Path, items: list[Path], bundle_name: str) -> Path:
        """Copy selected video and subtitle items under stage_root/<bundle_name>/."""
        # Step 1: Create container folder matching the video stem inside staging directory.
        bundle_dir = stage_root / bundle_name
        bundle_dir.mkdir(parents=True, exist_ok=True)

        # Step 2: Copy video file and matching subtitle directory into the bundle folder.
        for item in items:
            if item.is_file() and not item.is_symlink():
                shutil.copy2(item, bundle_dir / item.name)
            elif item.is_dir() and not item.is_symlink():
                shutil.copytree(item, bundle_dir / item.name, dirs_exist_ok=True)

        return bundle_dir

    def _write_unencrypted_zip(self, zip_path: Path, items: list[Path], bundle_name: str) -> None:
        """Create a standard compressed ZIP containing '<bundle_name>/' and its contents."""
        # Step 1: Write video and optional subtitle folder inside '<bundle_name>/' prefix.
        try:
            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for item in items:
                    if item.is_file() and not item.is_symlink():
                        arcname = self._build_archive_member_name(item, item, bundle_name)
                        zf.write(item, arcname=arcname)
                    elif item.is_dir() and not item.is_symlink():
                        for file_path in sorted(item.rglob("*")):
                            if file_path.is_file() and not file_path.is_symlink():
                                arcname = self._build_archive_member_name(item, file_path, bundle_name)
                                zf.write(file_path, arcname=arcname)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ArchiveError(f"Failed writing ZIP archive '{zip_path}': {exc}") from exc

    def _write_password_zip(self, zip_path: Path, items: list[Path], bundle_name: str, password: str) -> None:
        """Create a password-protected ZIP containing '<bundle_name>/' via 7z or zip CLI."""
        # Step 1: Locate 7z or zip binary in PATH.
        seven_z = shutil.which("7z")
        zip_bin = shutil.which("zip")
        if not seven_z and not zip_bin:
            raise ArchiveError("Password-protected ZIP creation requires '7z' or 'zip' installed on PATH.")

        # Step 2: Stage items under '<bundle_name>/' inside a temporary directory.
        try:
            with tempfile.TemporaryDirectory(prefix="zip_stage_") as tmp_dir:
                stage_root = Path(tmp_dir)
                self._stage_bundle_items(stage_root, items, bundle_name)

                # Step 3: Archive staging root so '<bundle_name>/' becomes the root entry in ZIP.
                if seven_z:
                    cmd = [seven_z, "a", "-tzip", f"-p{password}", "-mem=AES256", str(zip_path), "."]
                else:
                    cmd = [zip_bin, "-r", "-P", password, str(zip_path), "."]

                subprocess.run(cmd, cwd=stage_root, check=True, capture_output=True, text=True)
        except (OSError, shutil.Error, subprocess.CalledProcessError) as exc:
            stderr_msg = getattr(exc, "stderr", "") or str(exc)
            raise ArchiveError(f"Failed creating password-protected ZIP '{zip_path.name}': {stderr_msg}") from exc

    def _create_archive(self, video_path: Path) -> Path:
        """Skip if target ZIP already exists in output_dir; otherwise build the ZIP archive."""
        # Step 1: Resolve target ZIP path directly inside output_dir.
        zip_path = self._build_target_zip_path(video_path)
        if self._should_skip_zip(zip_path):
            print(f"[INFO] Skipping existing archive: {zip_path}")
            return zip_path

        # Step 2: Collect items and write encrypted or standard ZIP archive.
        items = self._collect_items_for_video(video_path)
        bundle_name = video_path.stem
        if self.password:
            self._write_password_zip(zip_path, items, bundle_name, self.password)
        else:
            self._write_unencrypted_zip(zip_path, items, bundle_name)

        print(f"[INFO] Archived '{video_path.name}' -> {zip_path}")
        return zip_path

    def archive_all(self) -> list[Path]:
        """Scan source_dir recursively for videos and create a ZIP archive for each."""
        # Step 1: Discover non-symlink video files under source_dir.
        try:
            video_files = sorted(
                p
                for p in self.source_dir.rglob("*")
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in self.VIDEO_EXTENSIONS
            )
        except OSError as exc:
            raise ArchiveError(f"Failed scanning source directory '{self.source_dir}': {exc}") from exc

        if not video_files:
            print(f"[WARN] No video files found under: {self.source_dir}")
            return []

        # Step 2: Archive or skip each video sequentially.
        archives: list[Path] = []
        for video_path in video_files:
            archives.append(self._create_archive(video_path))

        print(f"[INFO] Processed {len(archives)} ZIP target(s) in: {self.output_dir}")
        return archives


def archive_videos_to_zip(
    source_dir: str | Path,
    output_dir: str | Path,
    password: str | None = None,
) -> list[Path]:
    """Archive all videos and matching subtitle folders from source_dir into ZIP files."""
    # Step 1: Validate non-empty directory arguments before creating archiver instances.
    src_str = str(source_dir).strip() if source_dir is not None else ""
    dst_str = str(output_dir).strip() if output_dir is not None else ""
    if not src_str or not dst_str:
        raise ValueError("Both source_dir and output_dir must be non-empty paths.")

    # Step 2: Execute archiving with unified error reporting.
    try:
        archiver = VideoZipArchiver(
            source_dir=src_str,
            output_dir=dst_str,
            password=password,
        )
        return archiver.archive_all()
    except (OSError, ValueError, ArchiveError) as exc:
        print(f"[ERROR] Video ZIP archiving failed: {exc}")
        raise ArchiveError(f"Failed archiving videos from '{src_str}' to '{dst_str}': {exc}") from exc