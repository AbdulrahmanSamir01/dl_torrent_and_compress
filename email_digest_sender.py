"""Module for aggregating .txt upload records and sending a formatted HTML email digest."""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


class EmailDigestError(RuntimeError):
    """Raised when reading records, building HTML, or sending email fails."""


_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _load_dotenv_if_present(env_path: Optional[str | Path] = None) -> None:
    """Load environment variables from a .env file if python-dotenv is installed."""
    # Step 1: Import dotenv lazily so the module works even if optional dependency is absent.
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[WARN] python-dotenv is not installed; relying on existing environment variables.")
        return

    # Step 2: Load explicit path or default .env without overriding already-set variables.
    try:
        if env_path is not None:
            resolved = Path(env_path).resolve()
            if resolved.is_file():
                load_dotenv(dotenv_path=resolved, override=False)
        else:
            load_dotenv(override=False)
    except OSError as exc:
        print(f"[WARN] Could not load .env file: {exc}")


def _is_safe_http_url(url: str) -> bool:
    """Allow only valid HTTP or HTTPS URLs to prevent javascript:/data: link injection."""
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_record_file(record_path: Path) -> Dict[str, object]:
    """Read key-value fields and extract safe HTTP(S) URLs from a .txt file."""
    content = record_path.read_text(encoding="utf-8")
    fields: Dict[str, str] = {}
    urls: List[str] = []

    for line in content.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            clean_key = key.strip().lower()
            clean_val = value.strip()
            if clean_key and clean_val:
                fields[clean_key] = clean_val

        for match in _URL_PATTERN.findall(line):
            cleaned = match.rstrip(".,);]")
            if _is_safe_http_url(cleaned) and cleaned not in urls:
                urls.append(cleaned)

    return {
        "file_name": record_path.name,
        "fields": fields,
        "urls": urls,
        "raw_lines": content.splitlines(),
    }


class EmailDigestSender:
    """Collects .txt record files, skips already-sent ones via .txt.done, and emails HTML."""

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        smtp_from: Optional[str] = None,
        smtp_to: Optional[str] = None,
        smtp_use_tls: Optional[bool] = None,
        timeout: int = 30,
        env_path: Optional[str | Path] = None,
    ) -> None:
        """Load .env and SMTP configuration from parameters or environment variables."""
        # Step 1: Load .env before reading environment variables.
        _load_dotenv_if_present(env_path=env_path)

        # Step 2: Resolve SMTP connection and credential settings safely.
        self.smtp_host = (smtp_host if smtp_host is not None else os.getenv("SMTP_HOST", "smtp.gmail.com")).strip()
        env_port = (os.getenv("SMTP_PORT") or "587").strip()
        raw_port = smtp_port if smtp_port is not None else int(env_port)
        if raw_port <= 0 or raw_port > 65535:
            raise ValueError("SMTP port must be between 1 and 65535.")
        self.smtp_port = raw_port

        self.smtp_user = (smtp_user if smtp_user is not None else os.getenv("SMTP_USER", "")).strip()
        raw_password = smtp_password if smtp_password is not None else os.getenv("SMTP_PASSWORD", "")
        self.smtp_password = raw_password.strip()
        self.smtp_from = (
            smtp_from if smtp_from is not None else (os.getenv("SMTP_FROM") or self.smtp_user)
        ).strip()
        self.smtp_to = (
            smtp_to if smtp_to is not None else (os.getenv("SMTP_TO") or self.smtp_from)
        ).strip()

        if smtp_use_tls is not None:
            self.smtp_use_tls = bool(smtp_use_tls)
        else:
            env_tls = (os.getenv("SMTP_USE_TLS") or "true").strip().lower()
            self.smtp_use_tls = env_tls in {"1", "true", "yes", "on"}

        if timeout <= 0:
            raise ValueError("Timeout must be a positive integer.")
        self.timeout = int(timeout)

        # Step 3: Validate required SMTP fields without logging secrets.
        if not self.smtp_host or not self.smtp_user or not self.smtp_password:
            raise ValueError("SMTP host, user, and password are required.")
        if not self.smtp_from or not self.smtp_to:
            raise ValueError("Both sender (SMTP_FROM) and recipient (SMTP_TO) addresses are required.")

    def _validate_records_dir(self, records_dir: str | Path) -> Path:
        """Ensure records directory exists, is not a symlink, and is a directory."""
        if records_dir is None or not str(records_dir).strip():
            raise ValueError("Records directory path must not be empty.")
        try:
            resolved = Path(records_dir).resolve()
            if resolved.is_symlink() or not resolved.exists() or not resolved.is_dir():
                raise FileNotFoundError(f"Invalid records directory: {resolved}")
            return resolved
        except OSError as exc:
            raise EmailDigestError(f"Failed accessing records directory '{records_dir}': {exc}") from exc

    def _done_marker_path(self, txt_file: Path) -> Path:
        """Return companion marker path '<name>.txt.done' without modifying txt_file."""
        return txt_file.parent / f"{txt_file.name}.done"

    def _is_already_sent(self, txt_file: Path) -> bool:
        """Return True if a companion '<file>.txt.done' marker exists."""
        marker = self._done_marker_path(txt_file)
        try:
            return marker.exists() and marker.is_file() and not marker.is_symlink()
        except OSError as exc:
            raise EmailDigestError(f"Failed checking done marker '{marker}': {exc}") from exc

    def _mark_as_sent(self, txt_file: Path) -> None:
        """Create companion '<file>.txt.done' after successful email send."""
        marker = self._done_marker_path(txt_file)
        try:
            marker.write_text("sent=true\n", encoding="utf-8")
        except OSError as exc:
            raise EmailDigestError(f"Failed creating done marker '{marker}': {exc}") from exc

    def _discover_pending_txt_files(self, records_dir: Path) -> tuple[List[Path], List[Path]]:
        """Return (pending_txt_files, skipped_txt_files) sorted deterministically."""
        try:
            all_txt = sorted(
                p for p in records_dir.iterdir()
                if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".txt"
            )
        except OSError as exc:
            raise EmailDigestError(f"Failed listing .txt files in '{records_dir}': {exc}") from exc

        pending: List[Path] = []
        skipped: List[Path] = []
        for txt_path in all_txt:
            if self._is_already_sent(txt_path):
                skipped.append(txt_path)
            else:
                pending.append(txt_path)
        return pending, skipped

    def _render_link_box_html(self, url: str, index: int) -> str:
        """Render a link card with open and mailto actions using escaped attributes."""
        safe_url = html.escape(url, quote=True)
        safe_label = html.escape(url, quote=False)
        return f"""
        <div style="border:1px solid #d1d5db;border-radius:10px;padding:12px;margin:10px 0;background:#f9fafb;">
          <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">الرابط #{index}</div>
          <div style="font-family:monospace;font-size:13px;color:#111827;word-break:break-all;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;padding:8px 10px;margin-bottom:10px;">
            {safe_label}
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:8px;">
            <a href="{safe_url}" target="_blank" rel="noopener noreferrer"
               style="display:inline-block;text-decoration:none;background:#2563eb;color:#ffffff;font-weight:600;font-size:13px;padding:8px 14px;border-radius:6px;">
              الانتقال إلى الرابط
            </a>
            <a href="mailto:?subject=Link%20%23{index}&amp;body={safe_url}"
               style="display:inline-block;text-decoration:none;background:#e5e7eb;color:#111827;font-weight:600;font-size:13px;padding:8px 14px;border-radius:6px;">
              مشاركة الرابط
            </a>
          </div>
        </div>
        """

    def _build_email_bodies(self, records: List[Dict[str, object]]) -> tuple[str, str]:
        """Build plain-text fallback and responsive RTL HTML email body."""
        text_parts: List[str] = ["ملخص الملفات النصية والروابط الجديدة:\n"]
        html_cards: List[str] = []
        link_counter = 1

        for rec in records:
            file_name = html.escape(str(rec["file_name"]), quote=False)
            fields = rec["fields"] if isinstance(rec["fields"], dict) else {}
            urls = rec["urls"] if isinstance(rec["urls"], list) else []

            text_parts.append(f"=== {rec['file_name']} ===")
            for k, v in fields.items():
                text_parts.append(f"{k}: {v}")
            for u in urls:
                text_parts.append(f"URL: {u}")
            text_parts.append("")

            meta_rows = "".join(
                f"<tr><td style='padding:4px 8px;color:#4b5563;font-weight:600;'>{html.escape(str(k))}</td>"
                f"<td style='padding:4px 8px;color:#111827;word-break:break-word;'>{html.escape(str(v))}</td></tr>"
                for k, v in fields.items()
                if str(k).lower() != "download_page"
            )
            meta_table = (
                f"<table style='width:100%;border-collapse:collapse;font-size:13px;margin-bottom:8px;'>{meta_rows}</table>"
                if meta_rows
                else ""
            )

            boxes_html = ""
            for raw_url in urls:
                boxes_html += self._render_link_box_html(str(raw_url), link_counter)
                link_counter += 1

            if not boxes_html:
                boxes_html = "<div style='font-size:13px;color:#6b7280;'>لا توجد روابط HTTP/HTTPS في هذا الملف.</div>"

            html_cards.append(
                f"""
                <section style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin-bottom:16px;">
                  <h3 style="margin:0 0 10px 0;font-size:16px;color:#0f172a;word-break:break-all;">{file_name}</h3>
                  {meta_table}
                  {boxes_html}
                </section>
                """
            )

        html_body = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>تقرير الروابط والملفات</title>
</head>
<body style="margin:0;padding:16px;background:#f3f4f6;font-family:Tahoma,Arial,sans-serif;">
  <div style="max-width:680px;width:100%;margin:0 auto;">
    <h2 style="margin:0 0 14px 0;font-size:20px;color:#111827;">تقرير الملفات والروابط المرفوعة</h2>
    {''.join(html_cards)}
  </div>
</body>
</html>"""

        return "\n".join(text_parts), html_body

    def _send_smtp_message(self, subject: str, plain_text: str, html_body: str) -> None:
        """Send multipart email via SMTP / STARTTLS with explicit timeout and error handling."""
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.smtp_from
        msg["To"] = self.smtp_to
        msg.set_content(plain_text)
        msg.add_alternative(html_body, subtype="html")

        try:
            if self.smtp_use_tls:
                context = ssl.create_default_context()
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as server:
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailDigestError(f"Failed sending email via '{self.smtp_host}:{self.smtp_port}': {exc}") from exc

    def send_records_from_dir(
        self,
        records_dir: str | Path,
        subject: str = "ملخص روابط الملفات المرفوعة",
    ) -> Dict[str, object]:
        """Collect pending .txt files, email formatted HTML, and create .txt.done markers."""
        # Step 1: Validate directory and discover pending vs already-skipped .txt files.
        target_dir = self._validate_records_dir(records_dir)
        pending_files, skipped_files = self._discover_pending_txt_files(target_dir)

        for skipped in skipped_files:
            print(f"[INFO] Skipping already sent record (.done exists): {skipped.name}")

        if not pending_files:
            print("[INFO] No pending .txt files to send.")
            return {
                "sent_count": 0,
                "skipped_count": len(skipped_files),
                "sent_files": [],
                "skipped_files": [p.name for p in skipped_files],
            }

        # Step 2: Parse pending .txt files without modifying them.
        parsed_records: List[Dict[str, object]] = []
        valid_pending: List[Path] = []
        for txt_path in pending_files:
            try:
                parsed_records.append(_parse_record_file(txt_path))
                valid_pending.append(txt_path)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"[WARN] Skipping unreadable .txt file '{txt_path.name}': {exc}")

        if not valid_pending:
            raise EmailDigestError("All pending .txt files failed to parse.")

        # Step 3: Render email bodies and dispatch via SMTP.
        plain_text, html_body = self._build_email_bodies(parsed_records)
        self._send_smtp_message(subject=subject, plain_text=plain_text, html_body=html_body)

        # Step 4: Write companion <file>.txt.done markers only after send succeeds.
        for txt_path in valid_pending:
            self._mark_as_sent(txt_path)
            print(f"[INFO] Marked as sent: {txt_path.name}.done")

        return {
            "sent_count": len(valid_pending),
            "skipped_count": len(skipped_files),
            "sent_files": [p.name for p in valid_pending],
            "skipped_files": [p.name for p in skipped_files],
        }


def send_records_email(
    records_dir: str | Path,
    subject: str = "ملخص روابط الملفات المرفوعة",
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    smtp_from: Optional[str] = None,
    smtp_to: Optional[str] = None,
    smtp_use_tls: Optional[bool] = None,
    timeout: int = 30,
    env_path: Optional[str | Path] = None,
) -> Dict[str, object]:
    """Run record aggregation and HTML email sending in a single entry-point call."""
    # Step 1: Validate non-empty records directory input before building sender.
    if records_dir is None or not str(records_dir).strip():
        raise ValueError("Records directory must not be empty.")

    # Step 2: Instantiate EmailDigestSender and dispatch pending records safely.
    try:
        sender = EmailDigestSender(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            smtp_from=smtp_from,
            smtp_to=smtp_to,
            smtp_use_tls=smtp_use_tls,
            timeout=timeout,
            env_path=env_path,
        )
        return sender.send_records_from_dir(records_dir=records_dir, subject=subject)
    except (OSError, ValueError, EmailDigestError) as exc:
        print(f"[ERROR] Email digest dispatch failed: {exc}")
        raise EmailDigestError(f"Failed sending records email from '{records_dir}': {exc}") from exc