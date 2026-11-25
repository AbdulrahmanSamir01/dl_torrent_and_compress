# Fast Media Torrent -> FFmpeg -> VikingFile Pipeline

A modular, lightweight Python CLI and GitHub Actions workflow that downloads authorized torrents via `aria2c`, compresses videos using fast `FFmpeg` encoding, extracts embedded subtitles into dedicated per-video folders, bundles the output, and uploads it via streaming multipart requests to **VikingFile**.

## Architecture

1. `torrent_fetcher.py`: Validates torrent sources (`magnet:`, `.torrent`, `http(s)`) and downloads via `aria2c` without seeding.
2. `media_processor.py`: Uses `ffprobe` and `ffmpeg` to extract embedded subtitle tracks into a folder matching each video's name, then re-encodes the video (`libx264`/`libx265` with fast presets) to reduce file size.
3. `viking_uploader.py`: Streams large files in bounded-memory chunks using VikingFile's multipart API.
4. `main.py`: CLI orchestrator that ties the stages together and packs the output with `ZIP_STORED` (zero redundant CPU overhead).
5. `.github/workflows/dcw_torrent.yml`: Ready-to-run GitHub Actions workflow.

## Local Usage

```bash
sudo apt install aria2 ffmpeg
pip install -r requirements.txt

python main.py \
  --source "magnet:?xt=urn:btih:YOUR_HASH" \
  --archive-name "my_videos" \
  --codec libx264 \
  --preset veryfast \
  --crf 26 \
  --confirm-rights
```
