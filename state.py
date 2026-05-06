from .shared import *

class AuthState:
    IDLE = "IDLE"
    WAIT_PHONE = "WAIT_PHONE"
    CODE_SENT = "CODE_SENT"
    WAIT_2FA = "WAIT_2FA"


async def set_state(db: Database, user_id: int, state: str, **kwargs) -> None:
    """
    Upsert auth_state row for user_id. Additional kwargs are saved as columns.
    Uses INSERT ... ON CONFLICT DO UPDATE pattern.
    """
    cols = ["user_id", "state", "updated_at"] + list(kwargs.keys())
    vals = [user_id, state, datetime.now(timezone.utc).isoformat()] + list(kwargs.values())
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    update_parts = ["state=excluded.state", "updated_at=excluded.updated_at"]
    for k in kwargs.keys():
        update_parts.append(f"{k}=excluded.{k}")
    update_clause = ", ".join(update_parts)
    q = f"""
        INSERT INTO auth_state ({col_names})
        VALUES ({placeholders})
        ON CONFLICT(user_id) DO UPDATE SET
        {update_clause}
    """
    await db.execute(q, tuple(vals))


async def get_state(db: Database, user_id: int) -> Dict[str, Any]:
    row = await db.fetchone("SELECT * FROM auth_state WHERE user_id=?", (user_id,))
    if not row:
        return {"user_id": user_id, "state": AuthState.IDLE}

    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}

    try:
        return dict(row)
    except Exception:
        return {"user_id": user_id, "state": row[1] if len(row) > 1 else AuthState.IDLE}
