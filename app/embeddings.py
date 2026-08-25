from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import EmbeddingIndex, Job, PortfolioItem, Proposal
from app.models import ContextMatch, EmbeddingSource, WorkKind

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]{1,}", re.I)
_model: Any = None
_model_failed = False


def _load_model(settings: Settings) -> Any | None:
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed:
        return None
    try:
        from sentence_transformers import SentenceTransformer

        cache = os.environ.get("HF_HOME") or os.environ.get("SENTENCE_TRANSFORMERS_HOME")
        kwargs: dict[str, str] = {}
        if cache:
            kwargs["cache_folder"] = cache
        _model = SentenceTransformer(settings.embedding_model, **kwargs)
        return _model
    except Exception:
        logger.warning("sentence-transformers unavailable; using bag-of-words embeddings")
        _model_failed = True
        return None


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text or "")]


def _bow_vector(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    for tok in _tokenize(text):
        vec[hash(tok) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_texts(texts: list[str], settings: Settings | None = None) -> list[list[float]]:
    settings = settings or get_settings()
    model = _load_model(settings)
    if model is not None:
        vectors = model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in vectors]
    return [_bow_vector(text) for text in texts]


def _npz_path(settings: Settings) -> Path:
    return settings.data_dir / "embeddings.npz"


def _load_matrix(settings: Settings) -> tuple[list[list[float]], list[int]]:
    path = _npz_path(settings)
    json_path = path.with_suffix(".json")
    if path.exists():
        try:
            import numpy as np

            data = np.load(path, allow_pickle=True)
            matrix = data["vectors"].tolist()
            ids = [int(i) for i in data["ids"].tolist()]
            return matrix, ids
        except Exception:
            pass
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            return payload.get("vectors") or [], payload.get("ids") or []
        except json.JSONDecodeError:
            return [], []
    return [], []


def _save_matrix(settings: Settings, matrix: list[list[float]], ids: list[int]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        import numpy as np

        np.savez_compressed(
            _npz_path(settings),
            vectors=np.array(matrix, dtype="float32") if matrix else np.zeros((0, 1)),
            ids=np.array(ids, dtype="int32"),
        )
    except Exception:
        payload = {"vectors": matrix, "ids": ids}
        _npz_path(settings).with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")


def cosine(a: list[float], b: list[float]) -> float:
    length = min(len(a), len(b))
    if length == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(length))
    na = math.sqrt(sum(x * x for x in a[:length])) or 1.0
    nb = math.sqrt(sum(x * x for x in b[:length])) or 1.0
    return dot / (na * nb)


def portfolio_blob(item: PortfolioItem) -> str:
    origin = "Upwork history" if item.origin == "upwork" else "Agent notes"
    kind = {
        "job_history": "completed Upwork contract",
        "employment": "profile employment history, not an Upwork contract",
        "proposal": "submitted proposal, not completed work",
        "project": "project",
    }.get(item.kind or "", "project")
    return "\n".join(
        [
            f"[{origin} · {kind}] {item.project_title}",
            item.description or "",
            item.tech_stack or "",
            item.outcomes_achieved or "",
            item.associated_keywords or "",
        ]
    )


def remove_embedding(
    db: Session,
    source_type: EmbeddingSource | str,
    source_id: int,
) -> None:
    db.query(EmbeddingIndex).filter(
        EmbeddingIndex.source_type == str(source_type),
        EmbeddingIndex.source_id == source_id,
    ).delete()


def rebuild_portfolio_embeddings(db: Session, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    items = (
        db.query(PortfolioItem)
        .filter(PortfolioItem.kind != WorkKind.proposal.value)
        .order_by(PortfolioItem.id.asc())
        .all()
    )
    db.query(EmbeddingIndex).filter(EmbeddingIndex.source_type == EmbeddingSource.portfolio.value).delete()
    if not items:
        _save_matrix(settings, [], [])
        return 0
    texts = [portfolio_blob(item) for item in items]
    vectors = embed_texts(texts, settings)
    matrix: list[list[float]] = []
    ids: list[int] = []
    for item, vector, text in zip(items, vectors, texts, strict=True):
        row = EmbeddingIndex(
            source_type=EmbeddingSource.portfolio.value,
            source_id=item.id,
            vector_offset=len(matrix),
            text_preview=text[:2000],
        )
        db.add(row)
        db.flush()
        matrix.append(vector)
        ids.append(row.id)
    _save_matrix(settings, matrix, ids)
    return len(items)


def add_embedding(
    db: Session,
    source_type: EmbeddingSource | str,
    source_id: int,
    text: str,
    settings: Settings | None = None,
) -> EmbeddingIndex:
    settings = settings or get_settings()
    existing_rows = (
        db.query(EmbeddingIndex)
        .filter(EmbeddingIndex.source_type == str(source_type), EmbeddingIndex.source_id == source_id)
        .order_by(EmbeddingIndex.id.asc())
        .all()
    )
    existing = existing_rows[0] if existing_rows else None
    for extra in existing_rows[1:]:
        db.delete(extra)
    vector = embed_texts([text], settings)[0]
    matrix, ids = _load_matrix(settings)
    if (
        existing is not None
        and 0 <= existing.vector_offset < len(matrix)
        and existing.vector_offset < len(ids)
        and ids[existing.vector_offset] == existing.id
    ):
        matrix[existing.vector_offset] = vector
        existing.text_preview = text[:2000]
        _save_matrix(settings, matrix, ids)
        return existing
    offset = len(matrix)
    matrix.append(vector)
    if existing is None:
        existing = EmbeddingIndex(
            source_type=str(source_type),
            source_id=source_id,
            vector_offset=offset,
            text_preview=text[:2000],
        )
        db.add(existing)
        db.flush()
    else:
        existing.vector_offset = offset
        existing.text_preview = text[:2000]
    ids.append(existing.id)
    _save_matrix(settings, matrix, ids)
    return existing


def query_similar(
    db: Session,
    text: str,
    top_k: int = 3,
    source_type: str | None = None,
    settings: Settings | None = None,
) -> list[ContextMatch]:
    settings = settings or get_settings()
    matrix, ids = _load_matrix(settings)
    if not matrix:
        return []
    query_vec = embed_texts([text], settings)[0]
    scored: list[tuple[float, int]] = []
    for offset, vector in enumerate(matrix):
        if offset >= len(ids):
            continue
        scored.append((cosine(query_vec, vector), ids[offset]))
    scored.sort(key=lambda item: item[0], reverse=True)
    matches: list[ContextMatch] = []
    for score, index_id in scored:
        row = db.query(EmbeddingIndex).filter(EmbeddingIndex.id == index_id).one_or_none()
        if row is None:
            continue
        if source_type and row.source_type != source_type:
            continue
        title, body, origin = _resolve_text(db, row)
        if not title and not body:
            continue
        matches.append(
            ContextMatch(
                source_type=row.source_type,
                source_id=row.source_id,
                score=round(score, 4),
                text=body,
                title=title,
                origin=origin,
            )
        )
        if len(matches) >= top_k:
            break
    return matches


def _resolve_text(db: Session, row: EmbeddingIndex) -> tuple[str, str, str]:
    if row.source_type == EmbeddingSource.portfolio.value:
        item = db.query(PortfolioItem).filter(PortfolioItem.id == row.source_id).one_or_none()
        if item is None:
            return "", "", "agent"
        return item.project_title, portfolio_blob(item), item.origin or "agent"
    if row.source_type == EmbeddingSource.job.value:
        job = db.query(Job).filter(Job.id == row.source_id).one_or_none()
        if job is None:
            return "", row.text_preview, "upwork"
        return job.title, f"{job.title}\n{job.description}", "upwork"
    if row.source_type == EmbeddingSource.proposal.value:
        proposal = db.query(Proposal).filter(Proposal.id == row.source_id).one_or_none()
        if proposal is None:
            return "", row.text_preview, "agent"
        letter = proposal.edited_text or proposal.submitted_text or proposal.draft_text
        title = proposal.job.title if proposal.job else ""
        return title, letter, "agent"
    return "", row.text_preview, "agent"
