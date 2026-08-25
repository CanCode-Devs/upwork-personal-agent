from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, TypedDict

import httpx
from pydantic import BaseModel

from app.config import Settings, get_settings

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

TEXT_CAP = 8000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class JobAttachment(BaseModel):
    filename: str = ""
    url: str = ""
    path: str = ""
    text: str = ""
    error: str = ""


class ClientReview(BaseModel):
    title: str = ""
    reviewer: str = ""
    rating: float | None = None
    comment: str = ""


class AttachmentRef(TypedDict, total=False):
    filename: str
    url: str
    path: str
    text: str
    error: str


def safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name).strip("._") or "attachment"
    return cleaned[:120]


def extract_attachment_text(filename: str, data: bytes) -> tuple[str, str]:
    lower = filename.lower()
    try:
        if lower.endswith(".pdf"):
            if PdfReader is None:
                return "", "PDF extract needs pypdf"
            reader = PdfReader(io.BytesIO(data))
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
            text = "\n".join(part for part in pages if part)
            return (text, "") if text else ("", "no text in PDF")
        if lower.endswith(".docx"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                xml = archive.read("word/document.xml")
            root = ET.fromstring(xml)
            bits = [node.text.strip() for node in root.iter() if node.text and node.text.strip()]
            return " ".join(bits), ""
        if lower.endswith((".txt", ".md", ".csv", ".json", ".log")):
            return data.decode("utf-8", errors="replace"), ""
        return "", "not text-extracted"
    except Exception as exc:
        return "", str(exc)


async def store_job_attachments(
    job_id: str,
    details: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    items = details.get("attachments")
    if not isinstance(items, list) or not items:
        return details
    root = settings.data_dir / "job_files" / safe_filename(job_id)
    root.mkdir(parents=True, exist_ok=True)
    updated: list[dict[str, Any]] = []
    combined: list[str] = []
    remaining = TEXT_CAP
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        for item in items:
            if not isinstance(item, dict):
                continue
            att = JobAttachment.model_validate(item)
            dest = Path(att.path) if att.path else None
            if dest is not None and dest.exists() and (att.text or att.error):
                updated.append(att.model_dump())
                if remaining > 0 and att.text:
                    chunk = att.text[:remaining]
                    combined.append(f"{att.filename}: {chunk}")
                    remaining -= len(chunk)
                continue
            if not att.url.startswith("http"):
                updated.append(att.model_dump())
                continue
            try:
                response = await client.get(att.url)
                response.raise_for_status()
                payload = response.content
            except Exception as exc:
                att.error = str(exc)
                updated.append(att.model_dump())
                continue
            fname = safe_filename(att.filename or "attachment")
            dest = root / fname
            dest.write_bytes(payload)
            att.path = str(dest)
            text, err = extract_attachment_text(fname, payload)
            att.text = text[:TEXT_CAP]
            att.error = err
            updated.append(att.model_dump())
            if remaining > 0 and att.text:
                chunk = att.text[:remaining]
                combined.append(f"{att.filename}: {chunk}")
                remaining -= len(chunk)
    details["attachments"] = updated
    details["attachment_text"] = "\n\n".join(combined)[:TEXT_CAP]
    return details
