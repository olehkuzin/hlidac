"""Emails rendered from approved, versioned templates in `data/content/email/` (no clinical content).

A template is a subject and plain-text paragraphs with `{placeholders}`; each value is checked
against a pattern, so only what the template expects can get into an email: a login code's digits,
the link to the nurse queue (an http(s) base URL plus the fixed path `/sestra`: no query string,
no case or patient id), or a patient's link to the app home (the fixed path `/pacient`).
"""

import html
import re
from pathlib import Path

from hlidac.schemas import EmailJob, Strict
from hlidac.schemas._base import Id, NonEmpty, Version

CONTENT_DIR = Path(__file__).resolve().parents[2] / "data" / "content" / "email"

# The link in a care-team notice (WP16) only ever opens the nurse queue, never a case.
NURSE_QUEUE_PATH = "/sestra"
PATIENT_HOME_PATH = "/pacient"
# Reserved top-level names that never receive mail (RFC 2606, RFC 6762), e.g. the invented
# `@demo.local` accounts: a real SMTP server never gets them (outbox_runner, invitations).
UNDELIVERABLE_TLDS = frozenset({"local", "test", "example", "invalid", "localhost"})


def deliverable(address: str) -> bool:
    tld = address.rsplit("@", 1)[-1].rstrip(".").rsplit(".", 1)[-1].lower()
    return tld not in UNDELIVERABLE_TLDS
_ALLOWED_VALUES = {
    "code": re.compile(r"^\d{6}$"),
    "link": re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?/sestra$"),
    "patient_link": re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?/pacient$"),
}


class EmailTemplate(Strict):
    template_id: Id
    version: Version
    approval: NonEmpty  # "DEMO — čeká na schválení FN Motol" until approved
    source_ref: NonEmpty
    subject: NonEmpty
    paragraphs: list[NonEmpty]


def render(template_name: str, to: str, **values: str) -> EmailJob:
    raw = (CONTENT_DIR / f"{template_name}.json").read_text(encoding="utf-8")
    template = EmailTemplate.model_validate_json(raw)
    for name, value in values.items():
        if name not in _ALLOWED_VALUES or not _ALLOWED_VALUES[name].fullmatch(value):
            raise ValueError(f"value {name!r} is not allowed in an email")
    paragraphs = [p.format(**values) for p in template.paragraphs]
    html_paragraphs = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return EmailJob(
        kind="email",
        to=to,
        template_id=template.template_id,
        template_version=template.version,
        subject=template.subject,
        text_body="\n\n".join(paragraphs) + "\n",
        html_body=(
            '<!doctype html>\n<html lang="cs">\n<head><meta charset="utf-8">'
            f"<title>{html.escape(template.subject)}</title></head>\n"
            f"<body>\n{html_paragraphs}\n</body>\n</html>\n"
        ),
    )
