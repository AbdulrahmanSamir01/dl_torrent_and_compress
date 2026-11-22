# Torrent Download → ZIP → VikingFile

This workflow is intended only for content you own or are authorized to download and share.

## Components

- `torrent_downloader.py`: wraps `aria2c` for `.torrent` files, magnet URIs, and HTTP(S) torrent URLs.
- `vikingfile_uploader.py`: uploads files to VikingFile using its multipart API and bounded-memory streaming.
- `pipeline_torrent.py`: coordinates download, ZIP creation, and upload.
- `main_torrent.py`: command-line entry point.
- `.github/workflows/dcw_torrent.yml`: manually triggered GitHub Actions workflow.

## Local setup

1. Install aria2 (`aria2c`) and Python 3.10+.
2. Install dependencies: `python -m pip install -r pip-torrent.txt`.
3. Optionally set `VIKINGFILE_USER_HASH` to your VikingFile account hash. If omitted, the uploader requests an anonymous upload.
4. Run:

```bash
python main_torrent.py \
  --source "magnet:?xt=urn:btih:YOUR_INFO_HASH" \
  --archive-name "my_archive" \
  --compression-level 6 \
  --timeout-hours 6 \
  --confirm-rights
```

A local `.torrent` path or an HTTP(S) URL to a `.torrent` file can also be used as `--source`.

## GitHub Actions setup

1. Commit the Python files, `pip-torrent.txt`, and `.github/workflows/dcw_torrent.yml` to your repository.
2. Open **Actions → Run Torrent Pipeline → Run workflow**.
3. Provide a magnet URI or direct `.torrent` URL and confirm you have the right to share its contents.
4. Optionally add a repository secret named `VIKINGFILE_USER_HASH`. If not configured, the API is called with an empty `user` value for anonymous upload, as described by VikingFile's API documentation.
5. The final VikingFile URL is printed in the workflow log and GitHub step summary.

## Notes

- The workflow uses the runner's available disk space. Large torrents may exceed GitHub-hosted runner storage or the six-hour job limit.
- ZIP compression may not reduce already-compressed media files; set compression level `0` for faster storage without compression.
- The downloaded content and ZIP remain under `workspace_torrent/` on the runner. GitHub-hosted runner files are temporary unless separately uploaded as artifacts.
- No automatic seeding is requested (`--seed-time=0`).
