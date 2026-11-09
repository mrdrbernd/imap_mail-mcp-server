#!/usr/bin/env python3
# server.py - MCP mail server (IMAP read/draft, SMTP send) for Raspberry Pi
# Credentials are loaded from ~/mail-mcp/.env (see .env.example).
import os
import re
import base64
import imaplib
import smtplib
import email
import email.policy
import html as html_lib
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr, getaddresses
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import Field
from mcp.server.fastmcp import FastMCP

# Load credentials from the .env file (before reading the variables)
load_dotenv(os.path.expanduser("~/mail-mcp/.env"))

IMAP_HOST = os.environ["IMAP_HOST"]
SMTP_HOST = os.environ["SMTP_HOST"]
MAIL_USER = os.environ["MAIL_USER"]
MAIL_PASSWORD = os.environ["MAIL_PASSWORD"]

IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')

mcp = FastMCP(
    "mail",
    instructions=(
        "Zugriff auf das E-Mail-Postfach. Ablauf: Mails mit list_mails oder search_mails finden, "
        "mit read_mail lesen, dann Status setzen, verschieben, Entwurf erstellen oder senden. "
        "Mail-IDs stammen immer aus list_mails oder search_mails. "
        "Ordner werden bei Bedarf angelegt. Verschieben (move_mail) löscht nichts. "
        "Nichts wird ohne Zustimmung des Nutzers versendet oder gelöscht. "
        "Entwürfe (create_draft, create_reply_draft) sind der sichere Standard."
    ),
    host="127.0.0.1",
    port=3000,
)


# ---------- Helpers: connection & text ----------

def imap_connect():
    """Opens an IMAP connection over SSL and logs in."""
    m = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30)
    m.login(MAIL_USER, MAIL_PASSWORD)
    return m


def decode_text(value) -> str:
    """Converts encoded header values (e.g. umlauts) into readable text."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def check_uid(mail_id: str) -> bytes:
    """Mail IDs are IMAP UIDs (stable, digits only)."""
    if not mail_id.isdigit():
        raise ValueError("Ungültige Mail-ID. Bitte eine ID aus list_mails oder search_mails verwenden.")
    return mail_id.encode()


def check_recipient(addr: str) -> str:
    if "@" not in parseaddr(addr)[1]:
        raise ValueError(f"Ungültige Empfängeradresse: '{addr}'")
    return addr


def imap_date(days_back: int) -> str:
    """Date in IMAP format (English month abbreviations, independent of system language)."""
    d = datetime.now() - timedelta(days=days_back)
    return f"{d.day:02d}-{IMAP_MONTHS[d.month - 1]}-{d.year}"


def quote(s: str) -> str:
    """Escapes a search term for IMAP and wraps it in quotes."""
    if not s.isascii():
        raise ValueError("Suchbegriffe bitte ohne Umlaute/Sonderzeichen angeben (z. B. 'muller' statt 'müller').")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def html_to_text(raw: str) -> str:
    """Simple HTML-to-text conversion, used when no plain-text part exists."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def parse_header_info(m, uid: bytes) -> dict:
    """Reads sender, subject, date and read status without loading the body."""
    _, data = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    meta = data[0][0].decode(errors="replace")
    header = email.message_from_bytes(data[0][1])
    flags_match = re.search(r"FLAGS \(([^)]*)\)", meta)
    flags = flags_match.group(1).split() if flags_match else []
    return {
        "id": uid.decode(),
        "from": decode_text(header["From"]),
        "subject": decode_text(header["Subject"]),
        "date": header["Date"] or "",
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
    }


def build_message(to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = MAIL_USER
    msg["To"] = check_recipient(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MAIL_USER.split("@")[-1])
    msg.set_content(body)
    return msg


# ---------- Helpers: folders ----------

def imap_utf7_encode(s: str) -> str:
    """Encodes a folder name in IMAP modified UTF-7 (umlauts etc.)."""
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).enc