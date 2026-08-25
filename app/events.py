from sqlalchemy.orm import Session

from app.db.models import Event


def add_event(db: Session, kind: str, message: str, job_id: int | None = None) -> None:
    db.add(Event(job_id=job_id, kind=kind, message=message))
