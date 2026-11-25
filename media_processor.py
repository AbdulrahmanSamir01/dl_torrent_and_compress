"""Module for video transcoding and subtitle handling using FFmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class MediaProcessError(RuntimeError):
    """Raised when FFmpeg or FFprobe fails during media processing."""


class MediaProcessor:
    """Compress videos, extract embedded subtitles, and copy external subtitles."""

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}
    SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

    def __init__(
        self,
        output_dir: str | Path,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        crf: int = 26,
        preset: str = "veryfast",
        codec: str = "libx264",
        sample_seconds: float = 3.0,
    ) -> None:
        """Initialize media processor settings and verify output directory creation."""
        self.output_dir = Path(output_dir).resolve()
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.crf = crf
        self.preset = preset
        self.codec = codec
        self.sample_seconds = max(0.5, float(sample_seconds))

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaProcessError(f"Failed to create output directory '{self.output_dir}': {exc}") from exc

    def _check_binaries(self) -> tuple[str, str]:
        """Verify that ffmpeg and ffprobe are available in PATH."""
        ffmpeg_exec = shutil.which(self.ffmpeg_bin)
        ffprobe_exec = shutil.which(self.ffprobe_bin)
        if not ffmpeg_exec or not ffprobe_exec:
            raise MediaProcessError("Both ffmpeg and ffprobe must be installed on the system.")
        return ffmpeg_exec, ffprobe_exec

    def _probe_duration_seconds(self, ffprobe_exec: str, media_path: Path) -> float:
        """Return media duration in seconds via ffprobe, or 0.0 if unavailable."""
        cmd = [
            ffprobe_exec,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(media_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout or "{}")
            raw_dur = data.get("format", {}).get("duration")
            duration = float(raw_dur) if raw_dur is not None else 0.0
            return duration if duration > 0.0 else 0.0
        except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
            print(f"[WARN] Could not read duration for {media_path.name}: {exc}")
            return 0.0

    def _probe_segment_frames(
        self,
        ffmpeg_exec: str,
        media_path: Path,
        start_sec: float,
        duration_sec: float,
    ) -> int:
        """Decode a short segment at start_sec and return decoded video frame count."""
        _ = ffmpeg_exec
        probe_cmd = [
            shutil.which(self.ffprobe_bin) or self.ffprobe_bin,
            "-v", "error",
            "-read_intervals", f"{max(0.0, start_sec):.2f}%+{max(0.2, duration_sec):.2f}",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=nb_read_frames",
            "-of", "json",
            str(media_path),
        ]
        try:
            result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout or "{}")
            streams = data.get("streams", [])
            if not streams:
                return 0
            raw_frames = streams[0].get("nb_read_frames")
            return int(raw_frames) if raw_frames is not None else 0
        except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError, TypeError, OSError):
            return 0

    def _is_output_video_healthy(self, ffmpeg_exec: str, ffprobe_exec: str, output_video: Path) -> bool:
        """Check start, middle, and near-end segments of output_video for valid decoded frames."""
        try:
            resolved = output_video.resolve()
            if not resolved.is_file() or resolved.is_symlink():
                return False
            if resolved.stat().st_size <= 0:
                return False
        except OSError:
            return False

        duration = self._probe_duration_seconds(ffprobe_exec, resolved)
        if duration <= 0.0:
            return False

        seg = min(self.sample_seconds, max(0.5, duration / 4.0))
        start_point = 0.0
        mid_point = max(0.0, (duration / 2.0) - (seg / 2.0))
        end_point = max(0.0, duration - seg)

        for label, point in (("start", start_point), ("middle", mid_point), ("end", end_point)):
            decode_cmd = [
                ffmpeg_exec,
                "-v", "error",
                "-ss", f"{point:.2f}",
                "-t", f"{seg:.2f}",
                "-i", str(resolved),
                "-map", "0:v:0",
                "-f", "null",
                "-",
            ]
            try:
                subprocess.run(decode_cmd, check=True)
            except subprocess.CalledProcessError:
                print(f"[WARN] Decode error detected at {label} ({point:.2f}s) in {resolved.name}")
                return False

            frames = self._probe_segment_frames(ffmpeg_exec, resolved, point, seg)
            if frames <= 0:
                print(f"[WARN] No video frames decoded at {label} ({point:.2f}s) in {resolved.name}")
                return False

        return True

    def _should_skip_video(
        self,
        ffmpeg_exec: str,
        ffprobe_exec: str,
        output_video: Path,
    ) -> bool:
        """Two-stage skip check: (1) output file exists, (2) start/mid/end segments are healthy."""
        try:
            if not output_video.exists() or not output_video.is_file() or output_video.is_symlink():
                return False
        except OSError as exc:
            print(f"[WARN] Could not access output file '{output_video}': {exc}")
            return False

        return self._is_output_video_healthy(ffmpeg_exec, ffprobe_exec, output_video)

    def _probe_subtitles(self, ffprobe_exec: str, video_path: Path) -> list[dict]:
        """Inspect video container via ffprobe to locate embedded subtitle streams."""
        cmd = [
            ffprobe_exec,
            "-v", "error",
            "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json",
            str(video_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout or "{}")
            return data.get("streams", [])
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
            print(f"[WARN] Could not probe subtitles for {video_path.name}: {exc}")
            return []

    def _extract_subtitles(
        self,
        ffmpeg_exec: str,
        ffprobe_exec: str,
        video_path: Path,
        target_folder: Path,
    ) -> None:
        """Extract all embedded subtitle streams into the video's dedicated subtitle folder."""
        streams = self._probe_subtitles(ffprobe_exec, video_path)
        if not streams:
            return

        try:
            target_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[WARN] Could not create subtitle folder '{target_folder}': {exc}")
            return

        bitmap_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}

        for idx, stream in enumerate(streams):
            codec_name = stream.get("codec_name", "")
            tags = stream.get("tags", {})
            lang = tags.get("language", f"track{idx}")
            stream_index = stream.get("index")

            if codec_name in bitmap_codecs:
                ext = "sup" if codec_name == "hdmv_pgs_subtitle" else "sub"
                sub_codec = "copy"
            else:
                ext = "srt"
                sub_codec = "srt"

            sub_filename = f"{video_path.stem}.{lang}.{idx}.{ext}"
            sub_output = target_folder / sub_filename

            cmd = [
                ffmpeg_exec,
                "-y",
                "-v", "error",
                "-i", str(video_path),
                "-map", f"0:{stream_index}",
                "-c:s", sub_codec,
                str(sub_output),
            ]
            try:
                subprocess.run(cmd, check=True)
                print(f"[INFO] Extracted subtitle: {sub_filename}")
            except (subprocess.CalledProcessError, OSError):
                print(f"[WARN] Failed to extract subtitle stream {stream_index} from {video_path.name}")

    def _copy_external_subtitles(self, video_path: Path, target_folder: Path) -> None:
        """Copy external subtitle files matching video_path stem into target_folder."""
        # Collect external subtitle files that belong to this video stem (e.g. movie.srt or movie.en.srt).
        try:
            candidates = [
                p for p in video_path.parent.iterdir()
                if p.is_file()
                and not p.is_symlink()
                and p.suffix.lower() in self.SUBTITLE_EXTENSIONS
                and (p.stem == video_path.stem or p.stem.startswith(f"{video_path.stem}."))
            ]
        except OSError as exc:
            print(f"[WARN] Could not scan external subtitles near '{video_path.name}': {exc}")
            return

        if not candidates:
            return

        try:
            target_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[WARN] Could not create subtitle folder '{target_folder}': {exc}")
            return

        for sub_file in sorted(candidates):
            dest_file = target_folder / sub_file.name
            try:
                shutil.copy2(sub_file, dest_file)
                print(f"[INFO] Copied external subtitle: {sub_file.name} -> {dest_file}")
            except OSError as exc:
                print(f"[WARN] Failed to copy subtitle '{sub_file.name}': {exc}")

    def _compress_video(self, ffmpeg_exec: str, video_path: Path, output_video: Path) -> Path:
        """Transcode video safely via a temporary file and atomically replace target on success."""
        tmp_output = output_video.with_name(f".{output_video.stem}.tmp{output_video.suffix}")
        try:
            output_video.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MediaProcessError(f"Failed to create target folder for '{output_video}': {exc}") from exc

        cmd = [
            ffmpeg_exec,
            "-y",
            "-v", "error",
            "-stats",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", self.codec,
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(tmp_output),
        ]

        print(f"[INFO] Compressing video: {video_path.name} -> {output_video}")
        try:
            subprocess.run(cmd, check=True)
            tmp_output.replace(output_video)
        except (subprocess.CalledProcessError, OSError) as exc:
            if tmp_output.exists():
                try:
                    tmp_output.unlink()
                except OSError:
                    pass
            raise MediaProcessError(f"FFmpeg compression failed for {video_path.name}: {exc}") from exc

        try:
            orig_mb = video_path.stat().st_size / (1024 * 1024)
            new_mb = output_video.stat().st_size / (1024 * 1024)
            print(f"[INFO] Compressed {video_path.name}: {orig_mb:.2f} MiB -> {new_mb:.2f} MiB")
        except OSError as exc:
            print(f"[WARN] Could not read output file size for '{output_video}': {exc}")

        return output_video

    def _process_video_item(
        self,
        ffmpeg_exec: str,
        ffprobe_exec: str,
        video_path: Path,
        compressed_video_path: Path,
        subtitles_dir: Path,
    ) -> None:
        """Handle external/embedded subtitles and compress video if not already healthy."""
        # Always sync external subtitles even if the compressed video is already up to date.
        self._copy_external_subtitles(video_path, subtitles_dir)

        if self._should_skip_video(ffmpeg_exec, ffprobe_exec, compressed_video_path):
            print(f"[INFO] Skipping valid existing output: {compressed_video_path}")
            return

        self._extract_subtitles(ffmpeg_exec, ffprobe_exec, video_path, subtitles_dir)
        self._compress_video(ffmpeg_exec, video_path, compressed_video_path)

    def process_tree(self, source_dir: str | Path) -> Path:
        """Recursively scan source_dir for videos and mirror output structure in output_dir."""
        root_source = Path(source_dir).resolve()
        if not root_source.is_dir():
            raise MediaProcessError(f"Source directory does not exist: {root_source}")

        ffmpeg_exec, ffprobe_exec = self._check_binaries()
        try:
            video_files = sorted(
                p for p in root_source.rglob("*")
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in self.VIDEO_EXTENSIONS
            )
        except OSError as exc:
            raise MediaProcessError(f"Failed scanning source directory '{root_source}': {exc}") from exc

        for video_path in video_files:
            rel_parent = video_path.parent.relative_to(root_source)
            target_dir = self.output_dir / rel_parent
            compressed_video_path = target_dir / f"{video_path.stem}.mp4"
            subtitles_dir = target_dir / video_path.stem

            self._process_video_item(
                ffmpeg_exec=ffmpeg_exec,
                ffprobe_exec=ffprobe_exec,
                video_path=video_path,
                compressed_video_path=compressed_video_path,
                subtitles_dir=subtitles_dir,
            )

        return self.output_dir

    def process_files(self, files: list[Path]) -> Path:
        """Process an explicit list of files into output_dir."""
        ffmpeg_exec, ffprobe_exec = self._check_binaries()

        for file_path in files:
            try:
                suffix = file_path.suffix.lower()
                if suffix in self.VIDEO_EXTENSIONS:
                    video_folder = self.output_dir / file_path.stem
                    compressed_path = video_folder / f"{file_path.stem}.mp4"
                    self._process_video_item(
                        ffmpeg_exec=ffmpeg_exec,
                        ffprobe_exec=ffprobe_exec,
                        video_path=file_path,
                        compressed_path=compressed_path,
                        subtitles_dir=video_folder,
                    )
                elif suffix in self.SUBTITLE_EXTENSIONS:
                    matching_folder = self.output_dir / file_path.stem.split(".")[0]
                    dest_dir = matching_folder if matching_folder.exists() else self.output_dir
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file_path, dest_dir / file_path.name)
                else:
                    shutil.copy2(file_path, self.output_dir / file_path.name)
            except OSError as exc:
                raise MediaProcessError(f"Failed processing file '{file_path}': {exc}") from exc

        return self.output_dir


def process_media_tree(source_dir: str | Path, output_dir: str | Path) -> Path:
    """Run recursive video compression and subtitle handling with full error handling."""
    src_str = str(source_dir).strip() if source_dir is not None else ""
    dst_str = str(output_dir).strip() if output_dir is not None else ""
    if not src_str or not dst_str:
        raise ValueError("Both source_dir and output_dir must be non-empty paths.")

    try:
        processor = MediaProcessor(output_dir=dst_str)
        return processor.process_tree(src_str)
    except (OSError, ValueError, MediaProcessError) as exc:
        print(f"[ERROR] Media tree processing failed: {exc}")
        raise MediaProcessError(f"Failed processing media from '{src_str}' to '{dst_str}': {exc}") from exc