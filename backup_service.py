from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from config import BUILD

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "competency_dss.db"
EVIDENCE_DIR = ROOT / "evidence"
BACKUP_DIR = ROOT / "backups" / "runtime"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _safe_label(label: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in (label or "manual"))
    return cleaned[:40] or "manual"


def _db_copy(target: Path) -> None:
    # sqlite3.Connection context manager commits/rolls back but does NOT
    # close the connection. On Windows that can leave the temporary DB
    # file locked and make TemporaryDirectory cleanup fail with WinError 32.
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def create_backup(label: str = "manual", created_by: str = "system") -> dict[str, Any]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _safe_label(label)
    filename = f"DSS_Backup_{ts}_{safe}.zip"
    out = BACKUP_DIR / filename
    with tempfile.TemporaryDirectory(prefix="dss_backup_") as td:
        temp_db = Path(td) / "competency_dss.db"
        _db_copy(temp_db)
        con = sqlite3.connect(temp_db)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup database integrity check gagal: {integrity}")
        finally:
            con.close()
        evidence_files = [p for p in EVIDENCE_DIR.rglob("*") if p.is_file()] if EVIDENCE_DIR.exists() else []
        manifest = {
            "build": BUILD,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "created_by": created_by,
            "label": safe,
            "database": "database/competency_dss.db",
            "evidence_file_count": len(evidence_files),
        }
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.write(temp_db, "database/competency_dss.db")
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for p in evidence_files:
                rel = p.relative_to(EVIDENCE_DIR).as_posix()
                z.write(p, f"evidence/{rel}")
    return {"filename": filename, "path": str(out), **manifest, "size": out.stat().st_size}


def list_backups(limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(BACKUP_DIR.glob("DSS_Backup_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[: max(1, min(limit, 200))]:
        item: dict[str, Any] = {"filename": p.name, "size": p.stat().st_size, "modified_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
        try:
            with zipfile.ZipFile(p) as z:
                item.update(json.loads(z.read("manifest.json")))
        except Exception as exc:
            item["error"] = str(exc)
        rows.append(item)
    return rows


def _backup_path(filename: str) -> Path:
    if Path(filename).name != filename:
        raise ValueError("Nama backup tidak valid.")
    p = (BACKUP_DIR / filename).resolve()
    if BACKUP_DIR.resolve() not in p.parents or not p.exists():
        raise FileNotFoundError(filename)
    return p


def validate_backup(filename: str) -> dict[str, Any]:
    p = _backup_path(filename)
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        if "manifest.json" not in names or "database/competency_dss.db" not in names:
            raise ValueError("Backup tidak memiliki manifest/database yang diperlukan.")
        manifest = json.loads(z.read("manifest.json"))
        for n in names:
            pp = Path(n)
            if pp.is_absolute() or ".." in pp.parts:
                raise ValueError("Backup mengandung path yang tidak aman.")
        raw = z.read("database/competency_dss.db")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(raw)
        temp_name = f.name
    try:
        con = sqlite3.connect(temp_name)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            user_cols = {r[1] for r in con.execute("PRAGMA table_info(app_users)")}
            session_cols = {r[1] for r in con.execute("PRAGMA table_info(auth_sessions)")}
        finally:
            con.close()
        required = {"employees", "training_requirements", "certifications", "app_users", "auth_sessions"}
        security_ok = {"must_change_password","last_login_at","password_changed_at"}.issubset(user_cols) and {"last_seen_at","revoked_at"}.issubset(session_cols)
        build_value = str(manifest.get("build", ""))
        if integrity != "ok" or not required.issubset(tables) or not security_ok or not build_value.startswith("v22."):
            raise ValueError("Backup tidak kompatibel dengan schema/security Build v22.")
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return {"valid": True, "filename": filename, **manifest}


def restore_backup(filename: str, restored_by: str) -> dict[str, Any]:
    validation = validate_backup(filename)
    safety = create_backup("pre_restore", restored_by)
    p = _backup_path(filename)
    with tempfile.TemporaryDirectory(prefix="dss_restore_") as td:
        td_path = Path(td)
        with zipfile.ZipFile(p) as z:
            db_target = td_path / "competency_dss.db"
            db_target.write_bytes(z.read("database/competency_dss.db"))
            extracted_evidence = td_path / "evidence"
            for n in z.namelist():
                if not n.startswith("evidence/") or n.endswith("/"):
                    continue
                rel = Path(n).relative_to("evidence")
                dest = extracted_evidence / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(n))
        src = sqlite3.connect(db_target)
        dst = sqlite3.connect(DB_PATH)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        for child in EVIDENCE_DIR.iterdir():
            if child.is_dir(): shutil.rmtree(child)
            else: child.unlink(missing_ok=True)
        if extracted_evidence.exists():
            for child in extracted_evidence.iterdir():
                target = EVIDENCE_DIR / child.name
                if child.is_dir(): shutil.copytree(child, target)
                else: shutil.copy2(child, target)
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("UPDATE auth_sessions SET active_flag=0, revoked_at=CURRENT_TIMESTAMP WHERE active_flag=1")
            con.commit()
        finally:
            con.close()
    return {"status": "success", "restored": filename, "safety_backup": safety["filename"], "manifest": validation}


def ensure_daily_backup(created_by: str = "system") -> dict[str, Any] | None:
    today = datetime.now().strftime("%Y%m%d")
    for p in BACKUP_DIR.glob(f"DSS_Backup_{today}_*_daily.zip"):
        if p.exists():
            return None
    return create_backup("daily", created_by)


def prune_backups(max_files: int = 30) -> int:
    files = sorted(BACKUP_DIR.glob("DSS_Backup_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    removed = 0
    for p in files[max_files:]:
        p.unlink(missing_ok=True)
        removed += 1
    return removed
