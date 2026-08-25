from pathlib import Path

import yaml

from app.config import Settings, get_settings
from app.models import ScoringConfig


def load_scoring_config(settings: Settings | None = None) -> ScoringConfig:
    settings = settings or get_settings()
    path: Path = settings.scoring_path
    if not path.exists():
        return ScoringConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ScoringConfig()
    return ScoringConfig.model_validate(data)
