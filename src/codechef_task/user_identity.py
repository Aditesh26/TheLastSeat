"""Header-based caller identification (not authentication)."""

import re
from typing import Annotated

from fastapi import Header, HTTPException

_USER_REF_PATTERN = re.compile(r"[A-Z0-9]{6,20}\Z", re.ASCII)


def require_user_ref(
    user_ref: Annotated[str, Header(alias="X-User-Ref", min_length=6, max_length=20)],
) -> str:
    """Require and canonicalize a college registration number.

    This is a caller-supplied identifier for the demo, not proof of identity.
    """
    normalized_user_ref = user_ref.strip().upper()
    if _USER_REF_PATTERN.fullmatch(normalized_user_ref) is None:
        raise HTTPException(
            status_code=422,
            detail="X-User-Ref must be a 6-20 character college registration number containing only letters and digits.",
        )
    return normalized_user_ref


def ensure_owner(stored_user_ref: str | None, requested_user_ref: str) -> None:
    """Require an assigned matching owner; never let callers claim legacy rows."""
    if stored_user_ref is None or stored_user_ref != requested_user_ref:
        raise HTTPException(status_code=403, detail="Resource belongs to another user")
