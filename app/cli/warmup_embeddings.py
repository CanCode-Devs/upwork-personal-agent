from __future__ import annotations

import logging
import os
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def cache_root() -> Path:
    return Path(os.environ.get("HF_HOME", "/models/huggingface"))


def marker_path() -> Path:
    return cache_root() / ".all-minilm-l6-v2.ready"


def _quiet_hub_logs() -> None:
    for name in ("httpx", "httpcore", "huggingface_hub", "transformers", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _embedding_dim(model: object) -> int:
    getter = getattr(model, "get_embedding_dimension", None) or getattr(
        model, "get_sentence_embedding_dimension"
    )
    return int(getter())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _quiet_hub_logs()
    settings = get_settings()
    cache_root().mkdir(parents=True, exist_ok=True)
    if marker_path().exists():
        logger.info("Hugging Face cache already has %s", settings.embedding_model)
        return
    from sentence_transformers import SentenceTransformer

    logger.info("Downloading %s into %s (once)", settings.embedding_model, cache_root())
    model = SentenceTransformer(settings.embedding_model, cache_folder=str(cache_root()))
    dim = _embedding_dim(model)
    marker_path().write_text(str(dim), encoding="utf-8")
    logger.info("Model ready (dim=%s)", dim)


if __name__ == "__main__":
    main()
