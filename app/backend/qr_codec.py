from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import qrcode
from qrcode.constants import ERROR_CORRECT_H


PAYLOAD_RE = re.compile(r"^SITE=([A-Z0-9][A-Z0-9_-]{0,31})\|COW=([0-9]{1,6})$")


def build_payload(site_code: str, cow_number: int) -> str:
    return f"SITE={site_code}|COW={cow_number:03d}"


def parse_payload(raw_value: str) -> dict[str, Any] | None:
    normalized = str(raw_value or "").strip().upper()
    match = PAYLOAD_RE.fullmatch(normalized)
    if match is None:
        return None
    return {"siteCode": match.group(1), "cowNumber": int(match.group(2)), "payload": normalized}


def write_qr_image(payload: str, path: Path) -> None:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    image.save(path, format="PNG", optimize=True)
