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
from app.llm import llm_extract_image_text

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

TEXT_CAP = 8000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
FILE_EXTS = (
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".zip",
    ".xlsx",
    ".pptx",
    ".rtf",
) + IMAGE_EXTS
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


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


def is_image_filename(filename: str) -> bool:
    return filename.lower().endswith(IMAGE_EXTS)


def image_mime(filename: str) -> str:
    lower = filename.lower()
    for ext, mime in _IMAGE_MIME.items():
        if lower.endswith(ext):
            return mime
    return "application/octet-stream"


def _rebuild_attachment_text(items: list[dict[str, Any]]) -> str:
    combined: list[str] = []
    remaining = TEXT_CAP
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or remaining <= 0:
            continue
        name = str(item.get("filename") or "attachment")
        chunk = text[:remaining]
        combined.append(f"{name}: {chunk}")
        remaining -= len(chunk)
    return "\n\n".join(combined)[:TEXT_CAP]


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
        if lower.endswith(IMAGE_EXTS):
            return "", "image-ocr"
        return "", "not text-extracted"
    except Exception as exc:
        return "", str(exc)


def _ocr_image(filename: str, data: bytes, settings: Settings) -> tuple[str, str]:
    try:
        text = llm_extract_image_text(data, filename, settings)
    except Exception as exc:
        return "", str(exc)
    if text.strip():
        return text[:TEXT_CAP], ""
    return "", "no text in image"


def _extract_with_ocr(filename: str, data: bytes, settings: Settings) -> tuple[str, str]:
    text, err = extract_attachment_text(filename, data)
    if text.strip():
        return text[:TEXT_CAP], ""
    if is_image_filename(filename) or err == "image-ocr":
        return _ocr_image(filename, data, settings)
    return text, err


def add_manual_attachment(
    details: dict[str, Any],
    *,
    job_id: str,
    filename: str,
    data: bytes | None = None,
    pasted_text: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    items = details.get("attachments")
    if not isinstance(items, list):
        items = []
    fname = safe_filename(filename or "pasted.txt")
    att = JobAttachment(filename=fname)
    if pasted_text.strip() and data is None:
        payload = pasted_text.encode("utf-8")
        fname = safe_filename(filename or "pasted.txt")
        att.filename = fname
        root = settings.data_dir / "job_files" / safe_filename(job_id)
        root.mkdir(parents=True, exist_ok=True)
        dest = root / fname
        dest.write_bytes(payload)
        att.path = str(dest)
        att.text = pasted_text.strip()[:TEXT_CAP]
    elif data:
        root = settings.data_dir / "job_files" / safe_filename(job_id)
        root.mkdir(parents=True, exist_ok=True)
        dest = root / fname
        dest.write_bytes(data)
        att.path = str(dest)
        text, err = _extract_with_ocr(fname, data, settings)
        att.text = text[:TEXT_CAP]
        att.error = err
    else:
        return details
    items.append(att.model_dump())
    details["attachments"] = items
    details["attachment_text"] = _rebuild_attachment_text(items)
    return details


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
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        for item in items:
            if not isinstance(item, dict):
                continue
            att = JobAttachment.model_validate(item)
            dest = Path(att.path) if att.path else None
            if dest is not None and dest.exists() and att.text:
                updated.append(att.model_dump())
                continue
            payload: bytes | None = None
            if dest is not None and dest.exists():
                payload = dest.read_bytes()
            elif att.url.startswith("http"):
                try:
                    response = await client.get(att.url)
                    response.raise_for_status()
                    payload = response.content
                except Exception as exc:
                    att.error = str(exc)
                    updated.append(att.model_dump())
                    continue
            else:
                updated.append(att.model_dump())
                continue
            fname = safe_filename(att.filename or "attachment")
            dest = dest if dest is not None and dest.exists() else root / fname
            if not dest.exists():
                dest.write_bytes(payload)
            att.path = str(dest)
            text, err = _extract_with_ocr(fname, payload, settings)
            att.text = text[:TEXT_CAP]
            att.error = err
            updated.append(att.model_dump())
    details["attachments"] = updated
    details["attachment_text"] = _rebuild_attachment_text(updated)
    return details
