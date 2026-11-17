"""Place the sample HR emails into the Gmail sandbox inbox as unread messages.

Each .eml file in week-04/hartwell-hr/sample-emails is uploaded with IMAP
APPEND straight into INBOX, dated now and without the \\Seen flag, so it shows
up as a new, unread message. Nothing is sent: no SMTP is used.

Usage, from the top of the repository:
    .venv\\Scripts\\python.exe week-04\\load_inbox.py

The login is read at run time from Agent-Email.txt in the folder above the
repository. Accepted layouts:
    the address on one line and the password on another, or
    labeled lines such as  EMAIL_ADDRESS=...  /  APP_PASSWORD=...
    (also "email:", "password:", "user", with ":" or "=")
Neither the address nor the password is ever printed or saved. For Gmail it must be an app password
(Google Account > Security > App passwords), with IMAP enabled in Gmail.

Running it again adds the messages again.
"""

import imaplib
import re
import sys
import time
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
LOGIN_FILE = REPO_ROOT.parent / "Agent-Email.txt"
EMAILS_DIR = HERE / "hartwell-hr" / "sample-emails"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
MAILBOX = "INBOX"

USER_LABELS = ("email", "e mail", "email address", "address", "user", "username",
               "user name", "login", "account")
PASSWORD_LABELS = ("password", "app password", "pass", "pwd")


def read_login():
    """(address, password) from LOGIN_FILE. Error messages never include the file's contents."""
    if not LOGIN_FILE.is_file():
        sys.exit(f"Login file not found: {LOGIN_FILE}")
    lines = [line.strip() for line in LOGIN_FILE.read_text(encoding="utf-8-sig").splitlines()
             if line.strip()]

    user = password = None
    unlabeled = []
    for line in lines:
        match = re.match(r"([A-Za-z][A-Za-z \-_]*?)\s*[:=]\s*(.+)$", line)
        # "EMAIL_ADDRESS", "email-address" and "Email address" all become "email address".
        label = re.sub(r"[\s_\-]+", " ", match.group(1)).strip().lower() if match else ""
        if label in USER_LABELS:
            user = match.group(2).strip()
        elif label in PASSWORD_LABELS:
            password = match.group(2).strip()
        else:
            unlabeled.append(line)

    # Unlabeled layout: the line with an @ is the address, the other one the password.
    if user is None:
        user = next((line for line in unlabeled if re.fullmatch(r"\S+@\S+\.\S+", line)), None)
    if password is None:
        others = [line for line in unlabeled if line != user]
        password = others[0] if len(others) == 1 else None

    if not user or not password:
        sys.exit(f"Could not find both an email address and a password in {LOGIN_FILE.name}. "
                 "Put the address on one line and the password on the next, or use "
                 "'email: ...' and 'password: ...' lines.")
    # Google shows app passwords in groups of four ("abcd efgh ijkl mnop").
    if re.fullmatch(r"[a-z]{4}( [a-z]{4}){3}", password):
        password = password.replace(" ", "")
    return user, password


def prepare(path):
    """The message as bytes, dated now, with a fresh Message-ID."""
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    for header in ("Date", "Message-ID"):
        del msg[header]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="hartwellfoods.example")
    return msg, msg.as_bytes(policy=policy.SMTP)  # CRLF line endings, as IMAP expects


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    files = sorted(EMAILS_DIR.glob("*.eml"))
    if not files:
        sys.exit(f"No .eml files found in {EMAILS_DIR}")

    user, password = read_login()
    print(f"Signing in to {IMAP_HOST} (sandbox account from {LOGIN_FILE.name})...")
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    except OSError as exc:
        sys.exit(f"Could not connect to {IMAP_HOST}: {exc}")
    try:
        try:
            imap.login(user, password)
        except imaplib.IMAP4.error:
            sys.exit("Gmail rejected the login. Check the address and app password in "
                     f"{LOGIN_FILE.name}, and that IMAP is enabled in Gmail settings.")
        finally:
            del password

        added = 0
        for path in files:
            msg, data = prepare(path)
            # No flags: the message arrives without \Seen, so it is unread.
            status, response = imap.append(MAILBOX, "", imaplib.Time2Internaldate(time.time()), data)
            if status == "OK":
                added += 1
                print(f"  Added {path.name}: {msg['From']} | {msg['Subject']}")
            else:
                detail = response[0].decode(errors="replace") if response and response[0] else status
                print(f"  Failed {path.name}: {detail}")
        print(f"\n{added} of {len(files)} messages placed in {MAILBOX} as unread. Nothing was sent.")
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    sys.exit(0 if added == len(files) else 1)


if __name__ == "__main__":
    main()
