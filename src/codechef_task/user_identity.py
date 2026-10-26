"""Header-based caller identification (not authentication)."""

import re
from typing import Annotated

from fastapi import Header, HTTPException

_USER_REF_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z", re.ASCII)


def require_user_ref(
    user_ref: Annotated[str, Header(alias="X-User-Ref", min_length=1, max_length=64)],
) -> str:
    """Require a syntactically valid caller reference on protected endpoints."""
    if _USER_REF_PATTERN.fullmatch(user_ref) is None:
        raise HTTPException(
            status_code=422,
            detail="X-User-Ref must be 1-64 letters, digits, '.', '_', ':', or '-'.",
        )
    return user_ref


def ensure_owner(stored_user_ref: str | None, requested_user_ref: str) -> None:
    """Allow legacy NULL-owner rows; reject a different identified owner."""
    if stored_user_ref is not None and stored_user_ref != requested_user_ref:
        raise HTTPException(status_code=403, detail="Resource belongs to another user")
