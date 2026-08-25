from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import AppRuntimeSettings
from app.db.session import SessionLocal
from app.models import AutonomyMode, RuntimeSettings


def get_or_create_runtime(db: Session, settings: Settings | None = None) -> AppRuntimeSettings:
    settings = settings or get_settings()
    row = db.query(AppRuntimeSettings).order_by(AppRuntimeSettings.id.asc()).first()
    if row is None:
        row = AppRuntimeSettings(
            autonomy_mode=settings.autonomy_mode,
            auto_submit_threshold=settings.auto_submit_threshold,
            min_score=settings.min_score,
            min_hourly=settings.min_hourly,
            min_fixed=settings.min_fixed,
        )
        db.add(row)
        db.flush()
    return row


def load_runtime(db: Session | None = None, settings: Settings | None = None) -> RuntimeSettings:
    settings = settings or get_settings()
    own = db is None
    session = db or SessionLocal()
    try:
        row = get_or_create_runtime(session, settings)
        if own:
            session.commit()
        mode = row.autonomy_mode
        try:
            autonomy = AutonomyMode(mode)
        except ValueError:
            autonomy = AutonomyMode.manual
        return RuntimeSettings(
            autonomy_mode=autonomy,
            auto_submit_threshold=row.auto_submit_threshold,
            min_score=row.min_score,
            min_hourly=row.min_hourly,
            min_fixed=row.min_fixed,
            require_verified_payment=bool(getattr(row, "require_verified_payment", False)),
            min_client_rating=getattr(row, "min_client_rating", None),
            min_client_hires=getattr(row, "min_client_hires", None),
            max_proposal_count=getattr(row, "max_proposal_count", None),
            prefer_timezones=getattr(row, "prefer_timezones", "") or "",
        )
    finally:
        if own:
            session.close()


def should_auto_submit(score: int, runtime: RuntimeSettings) -> bool:
    if runtime.autonomy_mode == AutonomyMode.manual:
        return False
    if runtime.autonomy_mode == AutonomyMode.fully_auto:
        return score >= runtime.min_score
    return score >= runtime.auto_submit_threshold
