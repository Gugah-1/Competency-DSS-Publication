from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from config import ROLE_PERMISSIONS, SESSION_IDLE_MINUTES, SESSION_ABSOLUTE_HOURS

HASH_SCHEME = "pbkdf2_sha256"
DEFAULT_ROUNDS = 210_000


def utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def password_policy(password: str) -> tuple[bool, str]:
    if len(password) < 10:
        return False, "Password minimal 10 karakter."
    if not any(c.islower() for c in password):
        return False, "Password harus memiliki huruf kecil."
    if not any(c.isupper() for c in password):
        return False, "Password harus memiliki huruf besar."
    if not any(c.isdigit() for c in password):
        return False, "Password harus memiliki angka."
    if not any(not c.isalnum() for c in password):
        return False, "Password harus memiliki simbol."
    return True, "OK"


def hash_password(password: str, rounds: int = DEFAULT_ROUNDS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), rounds)
    return f"{HASH_SCHEME}${rounds}${salt}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or stored == "disabled":
        return False
    try:
        scheme, rounds_s, salt, digest_hex = stored.split("$", 3)
        if scheme != HASH_SCHEME:
            return False
        rounds = int(rounds_s)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), rounds).hex()
        return hmac.compare_digest(digest, digest_hex)
    except Exception:
        return False


def role_permissions(role: str) -> set[str]:
    return set(ROLE_PERMISSIONS.get(role, set()))


def user_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    role = d.get("role") or ""
    return {
        "user_id": d.get("user_id"),
        "username": d.get("username"),
        "display_name": d.get("display_name"),
        "role": role,
        "active_flag": int(d.get("active_flag") or 0),
        "must_change_password": int(d.get("must_change_password") or 0),
        "last_login_at": d.get("last_login_at"),
        "permissions": sorted(role_permissions(role)),
    }


def authenticate(con: sqlite3.Connection, username: str, password: str) -> dict[str, Any] | None:
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM app_users WHERE lower(username)=lower(?) AND active_flag=1",
        (username.strip(),),
    ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return user_payload(row)


def create_session(
    con: sqlite3.Connection,
    user_id: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str:
    token = secrets.token_urlsafe(40)
    now = utcnow()
    expires = now + timedelta(hours=SESSION_ABSOLUTE_HOURS)
    con.execute(
        """INSERT INTO auth_sessions(token,user_id,created_at,expires_at,active_flag,last_seen_at,ip_address,user_agent)
           VALUES (?,?,?,?,1,?,?,?)""",
        (token, user_id, now.isoformat(sep=" "), expires.isoformat(sep=" "), now.isoformat(sep=" "), ip_address, (user_agent or "")[:500]),
    )
    con.execute("UPDATE app_users SET last_login_at=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (now.isoformat(sep=" "), user_id))
    return token


def session_user(con: sqlite3.Connection, token: str | None, touch: bool = True) -> dict[str, Any] | None:
    if not token:
        return None
    con.row_factory = sqlite3.Row
    row = con.execute(
        """SELECT s.*,u.username,u.display_name,u.role,u.active_flag user_active,u.must_change_password,u.last_login_at
           FROM auth_sessions s JOIN app_users u ON u.user_id=s.user_id
           WHERE s.token=? AND s.active_flag=1""",
        (token,),
    ).fetchone()
    if not row or int(row["user_active"] or 0) != 1:
        return None
    now = utcnow()
    expires = _dt(row["expires_at"])
    last_seen = _dt(row["last_seen_at"]) or _dt(row["created_at"])
    if not expires or now >= expires or not last_seen or now - last_seen > timedelta(minutes=SESSION_IDLE_MINUTES):
        con.execute("UPDATE auth_sessions SET active_flag=0, revoked_at=? WHERE token=?", (now.isoformat(sep=" "), token))
        return None
    if touch and now - last_seen > timedelta(minutes=5):
        con.execute("UPDATE auth_sessions SET last_seen_at=? WHERE token=?", (now.isoformat(sep=" "), token))
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "active_flag": 1,
        "must_change_password": int(row["must_change_password"] or 0),
        "last_login_at": row["last_login_at"],
        "permissions": sorted(role_permissions(row["role"])),
    }


def revoke_session(con: sqlite3.Connection, token: str | None) -> None:
    if token:
        con.execute("UPDATE auth_sessions SET active_flag=0, revoked_at=? WHERE token=?", (utcnow().isoformat(sep=" "), token))


def revoke_user_sessions(con: sqlite3.Connection, user_id: str) -> None:
    con.execute(
        "UPDATE auth_sessions SET active_flag=0, revoked_at=? WHERE user_id=? AND active_flag=1",
        (utcnow().isoformat(sep=" "), user_id),
    )


def change_password(con: sqlite3.Connection, user_id: str, current_password: str, new_password: str) -> None:
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT password_hash FROM app_users WHERE user_id=? AND active_flag=1", (user_id,)).fetchone()
    if not row or not verify_password(current_password, row["password_hash"]):
        raise ValueError("Password saat ini tidak sesuai.")
    ok, msg = password_policy(new_password)
    if not ok:
        raise ValueError(msg)
    con.execute(
        "UPDATE app_users SET password_hash=?, must_change_password=0, password_changed_at=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (hash_password(new_password), utcnow().isoformat(sep=" "), user_id),
    )
    revoke_user_sessions(con, user_id)


def set_temporary_password(con: sqlite3.Connection, user_id: str, temporary_password: str) -> None:
    ok, msg = password_policy(temporary_password)
    if not ok:
        raise ValueError(msg)
    con.execute(
        "UPDATE app_users SET password_hash=?, must_change_password=1, password_changed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (hash_password(temporary_password), user_id),
    )
    revoke_user_sessions(con, user_id)
