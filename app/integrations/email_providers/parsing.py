"""Safe parsing helpers: HTML to text, address parsing, automated-mail detection, reply building."""

from __future__ import annotations

import base64
import re
import warnings
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from app.integrations.email_providers.base import Address

_WS = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_NOREPLY = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|notifications?)@", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEADER_INJECTION = re.compile(r"[\r\n]")


def html_to_text(html: str) -> str:
    """Convert HTML to readable text. Scripts, styles and hidden content are dropped; nothing is executed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "noscript", "template", "svg", "iframe", "object"]):
        tag.decompose()
    # Hidden elements are a common vector for prompt injection and tracking text.
    for tag in soup.select('[style*="display:none"], [style*="display: none"], [hidden]'):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"]):
        block.insert_after("\n")
    for link in soup.find_all("a"):
        href = link.get("href")
        text = link.get_text(" ", strip=True)
        if isinstance(href, str) and href.startswith(("http://", "https://")) and href not in text:
            link.replace_with(f"{text} ({href})" if text else href)
    return normalize_text(soup.get_text())


def normalize_text(text: str) -> str:
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def decode_base64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parse_address(value: str | None) -> Address | None:
    if not value:
        return None
    name, email = parseaddr(value)
    if not email or "@" not in email:
        return None
    return Address(email=email.strip().lower(), name=name.strip() or None)


def parse_address_list(value: str | None) -> list[Address]:
    if not value:
        return []
    out: list[Address] = []
    for name, email in getaddresses([value]):
        if email and "@" in email:
            out.append(Address(email=email.strip().lower(), name=name.strip() or None))
    return out


def is_noreply_address(email: str) -> bool:
    return bool(_NOREPLY.match(email))


def detect_automated(headers: dict[str, str], sender_email: str) -> bool:
    """Heuristic for bulk/automated mail (newsletters, notifications, auto-replies)."""
    h = {k.lower(): v.lower() for k, v in headers.items()}
    if "list-unsubscribe" in h or "list-id" in h:
        return True
    if h.get("precedence") in {"bulk", "list", "junk"}:
        return True
    if h.get("auto-submitted", "no") != "no":
        return True
    return is_noreply_address(sender_email)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"^re:", subject, re.I) else f"Re: {subject}".strip()


def safe_header(value: str) -> str:
    """Reject CR/LF in header values (header injection)."""
    if _HEADER_INJECTION.search(value):
        raise ValueError("Header values must not contain line breaks")
    return value


def build_reply_mime(
    *,
    from_address: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: str | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((None, safe_header(from_address)))
    msg["To"] = safe_header(to)
    msg["Subject"] = safe_header(subject)
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = safe_header(in_reply_to)
        refs = f"{references} {in_reply_to}".strip() if references else in_reply_to
        msg["References"] = safe_header(refs)
    msg.set_content(body)
    return msg
