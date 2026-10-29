from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.redact import redact
from app.database.models import AuditLog


def audit(
    s: Session,
    actor: str,
    action: str,
    target: str = "",
    outcome: str = "ok",
    **detail: Any,
) -> None:
    """Record a security-relevant event. Details are redacted before storage."""
    safe = json.loads(redact(json.dumps(detail, default=str)))
    s.add(AuditLog(actor=actor, action=action, target=redact(target), outcome=outcome, detail=safe))
