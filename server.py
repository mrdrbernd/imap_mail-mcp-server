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
        "Access to the mailbox. Workflow: find mails with list_mails or search_mails, "
        "read them with read_mail, then set status, move, create a draft or send. "
        "Mail IDs always come from list_mails or search_mails. "
        "Folders are created when needed. Moving (move_mail) deletes nothing. "
        "Nothing is sent or deleted without the user's approval. "
        "Drafts (create_draft, create_reply_draft) are the safe default."
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
        raise ValueError("Invalid mail ID. Please use an ID from list_mails or search_mails.")
    return mail_id.encode()


def check_recipient(addr: str) -> str:
    if "@" not in parseaddr(addr)[1]:
        raise ValueError(f"Invalid recipient address: '{addr}'")
    return addr


def imap_date(days_back: int) -> str:
    """Date in IMAP format (English month abbreviations, independent of system language)."""
    d = datetime.now() - timedelta(days=days_back)
    return f"{d.day:02d}-{IMAP_MONTHS[d.month - 1]}-{d.year}"


def quote(s: str) -> str:
    """Escapes a search term for IMAP and wraps it in quotes."""
    if not s.isascii():
        raise ValueError("Please use search terms without umlauts or special characters (e.g. 'muller' instead of 'müller').")
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
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(s: str) -> str:
    """Decodes IMAP modified UTF-7 into readable text."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            chunk = s[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def mbox(raw: str) -> str:
    """Quotes a folder name for SELECT, CREATE, COPY and APPEND."""
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_folder_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Folder name must not be empty.")
    if len(name) > 200:
        raise ValueError("Folder name is too long (maximum 200 characters).")
    if any(c in name for c in '"\\*%') or any(ord(c) < 32 for c in name):
        raise ValueError('Folder name contains invalid characters (" \\ * % or control characters).')
    return name


def list_entries(m) -> list[dict]:
    """All folders on the server: raw (IMAP name), name (readable), delim (hierarchy separator)."""
    _, data = m.list()
    entries = []
    for line in data:
        if not line:
            continue
        match = LIST_RE.search(line.decode(errors="replace"))
        if not match:
            continue
        raw = match.group("name").strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        delim = match.group("delim").strip('"')
        entries.append({
            "raw": raw,
            "name": imap_utf7_decode(raw),
            "delim": None if delim == "NIL" else delim,
            "flags": match.group("flags").split(),
        })
    return entries


def server_delim(entries: list[dict]):
    return next((e["delim"] for e in entries if e["delim"]), None)


def find_folder(entries: list[dict], name: str):
    """Finds a folder case-insensitively. INBOX is found the same way."""
    key = name.casefold()
    return next((e for e in entries if e["name"].casefold() == key), None)


def to_server_name(name: str, entries: list[dict]) -> str:
    """Translates '/' as hierarchy separator into the server's separator."""
    delim = server_delim(entries)
    if delim and delim != "/":
        return name.replace("/", delim)
    return name


def ensure_folder(m, requested: str, entries: list[dict] | None = None):
    """Returns the existing folder or creates it (including parent folders).
    Returns: (folder entry, was_created)."""
    name = validate_folder_name(requested)
    entries = list_entries(m) if entries is None else entries
    name = to_server_name(name, entries)

    found = find_folder(entries, name)
    if found:
        return found, False

    delim = server_delim(entries)
    parts = name.split(delim) if delim else [name]
    if any(not p.strip() for p in parts):
        raise ValueError(f"Invalid folder name: '{requested}' (empty part in path).")

    created_any = False
    for i in range(1, len(parts) + 1):
        prefix = delim.join(parts[:i]) if delim else name
        if find_folder(entries, prefix) is None:
            raw = imap_utf7_encode(prefix)
            typ, data = m.create(mbox(raw))
            if typ != "OK":
                raise ValueError(f"Folder '{prefix}' could not be created: {data}")
            m.subscribe(mbox(raw))
            entries.append({"raw": raw, "name": prefix, "delim": delim})
            created_any = True

    found = find_folder(entries, name)
    if found is None:
        raise ValueError(f"Folder '{requested}' could not be found after creation.")
    return found, created_any


def open_folder(m, folder: str, readonly: bool = True) -> dict:
    """Opens an existing folder. Never creates anything."""
    name = validate_folder_name(folder)
    entries = list_entries(m)
    found = find_folder(entries, to_server_name(name, entries))
    if found is None:
        existing = ", ".join(sorted(e["name"] for e in entries)) or "none"
        raise ValueError(f"Folder '{folder}' does not exist. Existing folders: {existing}")
    typ, _ = m.select(mbox(found["raw"]), readonly=readonly)
    if typ != "OK":
        raise ValueError(f"Folder '{found['name']}' could not be opened")
    return found


SPECIAL_USE = {"Trash": "\\Trash", "Drafts": "\\Drafts", "Sent": "\\Sent"}

# Folder names for providers without SPECIAL-USE flags (German and English names,
# because folders on German mail accounts are often named in German)
NAME_FALLBACKS = {
    "Trash": ["Trash", "Papierkorb", "Gelöschte Elemente", "Deleted Items", "Deleted Messages"],
    "Drafts": ["Drafts", "Entwürfe", "Entwurf"],
    "Sent": ["Sent", "Sent Items", "Sent Messages", "Gesendet", "Gesendete Elemente"],
}


def special_folder(m, role: str) -> dict:
    """Finds the special folder (Trash, Drafts, Sent) via the SPECIAL-USE flag or a known name.
    Never creates anything. If none is found, returns an error listing the existing folders."""
    entries = list_entries(m)
    flag = SPECIAL_USE[role].lower()
    for e in entries:
        if flag in [f.lower() for f in e["flags"]]:
            return e
    for candidate in NAME_FALLBACKS[role]:
        found = find_folder(entries, candidate)
        if found:
            return found
    existing = ", ".join(sorted(e["name"] for e in entries)) or "none"
    raise ValueError(f"No folder found for '{role}'. Existing folders: {existing}")


def system_folder(m, role: str) -> str:
    """Server name for Trash, Drafts or Sent. Never creates a folder."""
    return special_folder(m, role)["raw"]


# ---------- Test tools & folders ----------

@mcp.tool()
def server_time() -> str:
    """Returns the current time in Europe/Berlin as an ISO timestamp (e.g. 2025-03-14T09:30:00+01:00).

    Use when date or time is needed for replies, deadlines or references, or to test the server connection.
    Changes nothing.
    """
    return datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds")


@mcp.tool()
def mail_check() -> str:
    """Checks the IMAP login and returns 'IMAP login successful' or the error message.

    Use first when there are connection problems, or to confirm the mailbox is reachable.
    Changes nothing.
    """
    try:
        m = imap_connect()
        m.logout()
        return "IMAP login successful"
    except Exception as e:
        return f"IMAP error: {e}"


@mcp.tool()
def list_folders() -> list[str]:
    """Lists all mailbox folders by name, e.g. INBOX, Sent, Drafts, the trash or custom folders such as 'Kunden/Müller'.

    Use when a folder name is unclear, or before creating a new folder to avoid duplicates.
    Changes nothing.
    """
    m = imap_connect()
    try:
        return [e["name"] for e in list_entries(m)]
    finally:
        m.logout()


@mcp.tool()
def create_folder(
    name: Annotated[str, Field(description="Name of the new folder, e.g. 'Kunden' or 'Kunden/Müller' for a subfolder. Umlauts are allowed.")],
) -> str:
    """Creates a new folder. Missing parent folders are created too. If the folder already exists (case-insensitive), nothing happens.

    Use when the user wants a new folder or subfolder. Not needed before move_mail; the target folder is created automatically there if needed.
    Deletes and renames nothing.
    """
    m = imap_connect()
    try:
        found, created = ensure_folder(m, name)
        if created:
            return f"Folder '{found['name']}' created"
        return f"Folder '{found['name']}' already exists"
    finally:
        m.logout()


# ---------- Reading ----------

@mcp.tool()
def list_mails(
    folder: Annotated[str, Field(description="Folder name, e.g. INBOX, Sent, Drafts or a custom folder (names via list_folders)")] = "INBOX",
    limit: Annotated[int, Field(description="Number of newest mails (1 to 50)", ge=1, le=50)] = 20,
) -> list[dict]:
    """Lists the newest mails of a folder, newest first. Returns per mail: id, from, subject, date, unread (bool), flagged (bool).

    Use for a quick overview ('What is new?', 'What is in the inbox?') or to find the id for read_mail, set_mail_status, move_mail, delete_mail or create_reply_draft.
    Do not use for targeted searches by sender or subject; use search_mails instead.
    Changes nothing and does not mark any mail as read.
    """
    m = imap_connect()
    try:
        open_folder(m, folder)
        _, data = m.uid("SEARCH", None, "ALL")
        uids = data[0].split()[-limit:]
        return [parse_header_info(m, uid) for uid in reversed(uids)]
    finally:
        m.logout()


@mcp.tool()
def search_mails(
    folder: Annotated[str, Field(description="Folder name, e.g. INBOX, Sent, Drafts or a custom folder (names via list_folders)")] = "INBOX",
    from_contains: Annotated[str, Field(description="Part of the sender address or name (empty = any). ASCII characters only.")] = "",
    subject_contains: Annotated[str, Field(description="Word in the subject (empty = any). ASCII characters only.")] = "",
    days_back: Annotated[int, Field(description="Only mails from the last N days (0 = all)", ge=0, le=3650)] = 0,
    unread_only: Annotated[bool, Field(description="Only unread mails")] = False,
    limit: Annotated[int, Field(description="Maximum number of hits (1 to 50)", ge=1, le=50)] = 20,
) -> list[dict]:
    """Searches mails by sender, subject, time range or read status. Returns the same fields as list_mails, newest first.

    Use when the user asks for a specific mail ('the mail from Müller last week', 'all unread mails').
    Criteria are combined with AND. Empty fields are ignored.
    Limitation: search terms must be ASCII (no umlauts). For umlauts, search the stem without them, e.g. 'muller'.
    Changes nothing and does not mark any mail as read.
    """
    criteria = []
    if from_contains:
        criteria += ["FROM", quote(from_contains)]
    if subject_contains:
        criteria += ["SUBJECT", quote(subject_contains)]
    if days_back:
        criteria += ["SINCE", imap_date(days_back)]
    if unread_only:
        criteria.append("UNSEEN")
    if not criteria:
        criteria = ["ALL"]

    m = imap_connect()
    try:
        open_folder(m, folder)
        _, data = m.uid("SEARCH", None, *criteria)
        uids = data[0].split()[-limit:]
        return [parse_header_info(m, uid) for uid in reversed(uids)]
    finally:
        m.logout()


@mcp.tool()
def read_mail(
    folder: Annotated[str, Field(description="Folder name, e.g. INBOX, Sent, Drafts or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The mail ID from list_mails or search_mails")],
) -> str:
    """Reads the full text of a mail (plain text preferred, otherwise cleaned-up HTML) and lists attachment names at the end.

    Use when the content of a specific mail is needed, e.g. before a summary or a reply. The id comes from list_mails or search_mails.
    Attachments are only named, their content is not returned.
    Changes nothing and does not mark the mail as read. Use set_mail_status for that.
    """
    m = imap_connect()
    try:
        open_folder(m, folder)
        _, data = m.uid("FETCH", check_uid(mail_id), "(BODY.PEEK[])")
        if not data or not isinstance(data[0], tuple):
            return "Mail not found"
        mail = email.message_from_bytes(data[0][1], policy=email.policy.default)

        plain = mail.get_body(preferencelist=("plain",))
        if plain is not None:
            text = plain.get_content()
        else:
            html_part = mail.get_body(preferencelist=("html",))
            text = html_to_text(html_part.get_content()) if html_part is not None else "(no text content found)"

        attachments = [decode_text(p.get_filename()) for p in mail.walk() if p.get_filename()]
        if attachments:
            text += "\n\nAttachments: " + ", ".join(attachments)
        return text
    finally:
        m.logout()


# ---------- Status & organisation ----------

@mcp.tool()
def set_mail_status(
    folder: Annotated[str, Field(description="Folder name, e.g. INBOX, Sent, Drafts or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The mail ID from list_mails or search_mails")],
    status: Annotated[
        Literal["read", "unread", "flagged", "unflagged"],
        Field(description="read = mark as read, unread = mark as unread, flagged = add star, unflagged = remove star"),
    ],
) -> str:
    """Sets the read status or the star (important flag) of a mail.

    Use when the user wants to mark something as read/unread or important. read_mail does not mark mails as read automatically; this tool is the deliberate way to do it.
    Moves and deletes nothing.
    """
    flag_map = {
        "read": ("+FLAGS", "\\Seen"),
        "unread": ("-FLAGS", "\\Seen"),
        "flagged": ("+FLAGS", "\\Flagged"),
        "unflagged": ("-FLAGS", "\\Flagged"),
    }
    op, flag = flag_map[status]
    m = imap_connect()
    try:
        open_folder(m, folder, readonly=False)
        typ, _ = m.uid("STORE", check_uid(mail_id), op, f"({flag})")
        if typ != "OK":
            return "Status change failed"
        return f"Status set: {status}"
    finally:
        m.logout()


def _move(folder: str, mail_id: str, target_folder: str | None = None, role: str | None = None) -> str:
    m = imap_connect()
    try:
        src = open_folder(m, folder, readonly=False)
        if role:
            target, created = special_folder(m, role), False
        else:
            target, created = ensure_folder(m, target_folder)
        if target["raw"] == src["raw"]:
            return "Mail is already in the target folder"
        uid = check_uid(mail_id)
        typ, _ = m.uid("COPY", uid, mbox(target["raw"]))
        if typ != "OK":
            return "Move failed"
        m.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        m.expunge()
        prefix = f"Folder '{target['name']}' was created. " if created else ""
        return f"{prefix}Mail moved to '{target['name']}'"
    finally:
        m.logout()


@mcp.tool()
def move_mail(
    folder: Annotated[str, Field(description="Current folder, e.g. INBOX or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The mail ID from list_mails or search_mails")],
    target_folder: Annotated[str, Field(description="Target folder, e.g. 'Kunden/Müller'. If it does not exist, it is created automatically.")],
) -> str:
    """Moves a mail to another folder. If the target folder does not exist, it is created automatically (including subfolders).

    Use when the user wants to sort, file or move a mail. May be used without asking again.
    Deletes nothing. To delete into the trash, use delete_mail.
    The mail receives a new id in the target folder.
    """
    return _move(folder, mail_id, target_folder)


@mcp.tool()
def delete_mail(
    folder: Annotated[str, Field(description="Current folder, e.g. INBOX or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The mail ID from list_mails or search_mails")],
) -> str:
    """Moves a mail to the trash. Nothing is permanently deleted; the mail can be restored from the trash.

    The trash is detected via the server flag \\Trash or the name (e.g. Papierkorb, Trash). If none exists, an error is returned. No folder is created.
    Use ONLY when the user explicitly asks to delete. If in doubt, ask first.
    The mail receives a new id in the trash.
    """
    return _move(folder, mail_id, role="Trash")


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def build_forward(m, folder: str, mail_id: str, to: str, body: str) -> EmailMessage:
    """Builds a forward of the original mail including all attachments."""
    open_folder(m, folder)
    _, data = m.uid("FETCH", check_uid(mail_id), "(BODY.PEEK[])")
    if not data or not isinstance(data[0], tuple):
        raise ValueError("Original mail not found")
    orig = email.message_from_bytes(data[0][1], policy=email.policy.default)

    text_part = orig.get_body(preferencelist=("plain",))
    if text_part is not None:
        orig_text = text_part.get_content()
    else:
        html_part = orig.get_body(preferencelist=("html",))
        orig_text = html_to_text(html_part.get_content()) if html_part is not None else ""

    orig_from = decode_text(str(orig["From"] or ""))
    orig_to = decode_text(str(orig["To"] or ""))
    orig_subject = decode_text(str(orig["Subject"] or ""))
    orig_date = str(orig["Date"] or "")

    subject = orig_subject
    if not subject.lower().startswith(("fwd:", "wg:")):
        subject = f"Fwd: {subject}"

    msg = EmailMessage()
    msg["From"] = MAIL_USER
    msg["To"] = check_recipient(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MAIL_USER.split("@")[-1])

    forward_block = (
        "\n\n---------- Forwarded message ----------\n"
        f"From: {orig_from}\n"
        f"Date: {orig_date}\n"
        f"Subject: {orig_subject}\n"
        f"To: {orig_to}\n\n"
        f"{orig_text}"
    )
    msg.set_content((body or "").rstrip() + forward_block)

    total = 0
    for part in orig.walk():
        name = part.get_filename()
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        total += len(payload)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments are too large (over 20 MB). Please forward the attachments individually or request them directly from the sender.")
        maintype, subtype = part.get_content_type().split("/", 1)
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=decode_text(name))
    return msg


# ---------- Writing ----------

@mcp.tool()
def create_draft(
    to: Annotated[str, Field(description="Recipient address, e.g. name@firma.de")],
    subject: Annotated[str, Field(description="Subject of the mail")],
    body: Annotated[str, Field(description="Body of the mail as plain text")],
) -> str:
    """Saves a new mail as a draft in the Drafts folder. The draft is NOT sent.

    Use when the user wants to write a mail but not send it yet, or wants to review the text first. This is the default way for new mails.
    For a reply to an existing mail, use create_reply_draft.
    The text is stored as plain text. Before saving, tell the user the recipient and subject for review.
    """
    msg = build_message(to, subject, body)
    m = imap_connect()
    try:
        m.append(mbox(system_folder(m, "Drafts")), "\\Draft", None, msg.as_bytes())
        return "Draft saved in the Drafts folder"
    finally:
        m.logout()


@mcp.tool()
def create_reply_draft(
    folder: Annotated[str, Field(description="Folder of the original mail, e.g. INBOX or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The ID of the original mail from list_mails or search_mails")],
    body: Annotated[str, Field(description="Reply text as plain text, without quoting the original mail")],
    reply_all: Annotated[bool, Field(description="Also include all recipients of the original (To/CC)")] = False,
) -> str:
    """Saves a reply to an existing mail as a draft in the Drafts folder. The draft is NOT sent.

    Use when the user wants to reply to a specific mail (id from list_mails or search_mails).
    Recipient, subject ('Re: ...') and thread assignment are taken from the original mail. The recipient is the Reply-To address or the sender.
    reply_all=True takes over the other recipients (To/CC) of the original, excluding our own address.
    The body contains only the new reply, without quoting the original mail.
    Sends nothing. Sending happens only after explicit approval by the user.
    """
    m = imap_connect()
    try:
        open_folder(m, folder)
        _, data = m.uid("FETCH", check_uid(mail_id),
                        "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO TO CC SUBJECT MESSAGE-ID REFERENCES)])")
        if not data or not isinstance(data[0], tuple):
            return "Original mail not found"
        orig = email.message_from_bytes(data[0][1])

        reply = EmailMessage()
        reply["From"] = MAIL_USER
        reply["To"] = orig["Reply-To"] or orig["From"]

        if reply_all:
            others = getaddresses([str(orig["To"] or ""), str(orig["Cc"] or "")])
            cc = [addr for _, addr in others if addr and addr.lower() != MAIL_USER.lower()]
            if cc:
                reply["Cc"] = ", ".join(cc)

        subject = decode_text(orig["Subject"])
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        if orig["Message-ID"]:
            reply["In-Reply-To"] = orig["Message-ID"]
            refs = (orig["References"] or "").strip()
            reply["References"] = f"{refs} {orig['Message-ID']}".strip()

        reply["Date"] = formatdate(localtime=True)
        reply["Message-ID"] = make_msgid(domain=MAIL_USER.split("@")[-1])
        reply.set_content(body)

        m.append(mbox(system_folder(m, "Drafts")), "\\Draft", None, reply.as_bytes())
        return "Reply draft saved in the Drafts folder"
    finally:
        m.logout()


@mcp.tool()
def create_forward_draft(
    to: Annotated[str, Field(description="Recipient address, e.g. name@firma.de")],
    folder: Annotated[str, Field(description="Folder of the original mail, e.g. INBOX or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The ID of the mail to forward, from list_mails or search_mails")],
    body: Annotated[str, Field(description="Cover text above the forwarded message, as plain text. Leave empty to forward without comment.")] = "",
) -> str:
    """Saves a forward of an existing mail as a draft in the Drafts folder. All attachments of the original are included. The draft is NOT sent.

    Use when the user wants to forward a mail with attachments. This is the default way to forward.
    The subject gets 'Fwd:' and the original text follows a separator line with From, date, subject and To.
    Attachments up to 20 MB in total. Above that, an error is returned.
    Before saving, tell the user the recipient and subject for review.
    """
    m = imap_connect()
    try:
        msg = build_forward(m, folder, mail_id, to, body)
        m.append(mbox(system_folder(m, "Drafts")), "\\Draft", None, msg.as_bytes())
        return "Forward draft with attachments saved in the Drafts folder"
    finally:
        m.logout()


@mcp.tool()
def forward_mail(
    to: Annotated[str, Field(description="Recipient address, e.g. name@firma.de")],
    folder: Annotated[str, Field(description="Folder of the original mail, e.g. INBOX or a custom folder (names via list_folders)")],
    mail_id: Annotated[str, Field(description="The ID of the mail to forward, from list_mails or search_mails")],
    body: Annotated[str, Field(description="Cover text above the forwarded message, as plain text. Leave empty to forward without comment.")] = "",
) -> str:
    """Forwards an existing mail including all attachments immediately and irrevocably. A copy is stored in the Sent folder.

    Use ONLY when the user explicitly asks to send, e.g. 'forward the mail'. Otherwise use create_forward_draft.
    Show the recipient and subject to the user for confirmation first, unless they have clearly and fully instructed the sending.
    Attachments up to 20 MB in total. Above that, an error is returned and nothing is sent.
    """
    m = imap_connect()
    try:
        msg = build_forward(m, folder, mail_id, to, body)
    except ValueError as e:
        return f"Forward failed: {e}"
    finally:
        m.logout()

    try:
        with smtplib.SMTP(SMTP_HOST, 587, timeout=60) as s:
            s.starttls()
            s.login(MAIL_USER, MAIL_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        return f"Sending failed: {e}"

    try:
        m = imap_connect()
        try:
            m.append(mbox(system_folder(m, "Sent")), "\\Seen", None, msg.as_bytes())
        finally:
            m.logout()
    except Exception:
        return "Forward was sent (copy could not be saved to the Sent folder)"
    return "Forward with attachments was sent and saved to the Sent folder"


@mcp.tool()
def send_mail(
    to: Annotated[str, Field(description="Recipient address, e.g. name@firma.de")],
    subject: Annotated[str, Field(description="Subject of the mail")],
    body: Annotated[str, Field(description="Body of the mail as plain text")],
) -> str:
    """Sends a new mail immediately and irrevocably. A copy is stored in the Sent folder.

    Use ONLY when the user explicitly asks to send, e.g. 'send the mail'. Show recipient, subject and body to the user for confirmation first, unless they have clearly and fully instructed the sending.
    To reply to a mail: first create_reply_draft, then send after approval. This tool only sends new mails, not drafts.
    Returns a success or error message.
    """
    msg = build_message(to, subject, body)
    try:
        with smtplib.SMTP(SMTP_HOST, 587, timeout=30) as s:
            s.starttls()
            s.login(MAIL_USER, MAIL_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        return f"Sending failed: {e}"

    # Store a copy in Sent. Many SMTP servers do not do this automatically.
    # Note: providers that fill Sent themselves (e.g. Gmail) may produce a duplicate.
    try:
        m = imap_connect()
        try:
            m.append(mbox(system_folder(m, "Sent")), "\\Seen", None, msg.as_bytes())
        finally:
            m.logout()
    except Exception:
        return "Mail was sent (copy could not be saved to the Sent folder)"
    return "Mail was sent and saved to the Sent folder"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
