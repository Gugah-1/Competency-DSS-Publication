from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

ACTION_STATUSES = [
    "Open", "Planned", "Scheduled", "In Progress", "Waiting Evidence",
    "Waiting External Party", "Completed", "Closed", "Cancelled", "No Action Required",
]
ACTION_TYPES = [
    "Training", "Certification Renewal", "Evidence Validation", "Data Completion",
    "Requirement Validation", "Position Review", "Mapping Validation", "Reassessment",
    "Document Update", "Other",
]
ACTION_PRIORITIES = ["High", "Validation", "Medium", "Low", "Not Assessed"]
TERMINAL_ACTION_STATUSES = {"Closed", "Cancelled", "No Action Required"}
EVIDENCE_ENTITY_TABLES = {
    "action": ("actions", "action_id"),
    "validation": ("assessment_validations", "validation_id"),
    "training": ("training_history", "history_id"),
    "certification": ("certifications", "certification_id"),
    "override": ("assessment_overrides", "override_id"),
}
ALLOWED_EVIDENCE_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".xls", ".csv", ".docx"}
MAX_EVIDENCE_BYTES = 15 * 1024 * 1024


def _row(con: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    con.row_factory = sqlite3.Row
    return con.execute(sql, params).fetchone()


def _safe_filename(name: str) -> str:
    base = Path(name or "evidence").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(base).stem).strip("._") or "evidence"
    ext = Path(base).suffix.lower()
    return f"{stem[:80]}_{uuid.uuid4().hex[:12]}{ext}"


def assert_entity(con: sqlite3.Connection, entity_type: str, entity_id: int) -> None:
    entity_type = entity_type.strip().lower()
    if entity_type not in EVIDENCE_ENTITY_TABLES:
        raise ValueError("entity_type tidak didukung.")
    table, key = EVIDENCE_ENTITY_TABLES[entity_type]
    if entity_type == "action":
        exists = con.execute(
            "SELECT 1 FROM actions WHERE action_id=? AND COALESCE(is_deleted,0)=0",
            (entity_id,),
        ).fetchone()
    else:
        exists = con.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (entity_id,)).fetchone()
    if not exists:
        raise ValueError(f"Target evidence {entity_type} #{entity_id} tidak ditemukan.")


def infer_entity_context(con: sqlite3.Connection, entity_type: str, entity_id: int) -> tuple[int | None, int | None]:
    entity_type = entity_type.lower()
    if entity_type == "action":
        r = _row(con, "SELECT employee_pk,requirement_id FROM actions WHERE action_id=?", (entity_id,))
    elif entity_type == "validation":
        r = _row(con, "SELECT employee_pk,requirement_id FROM assessment_validations WHERE validation_id=?", (entity_id,))
    elif entity_type == "training":
        r = _row(con, "SELECT employee_pk,NULL requirement_id FROM training_history WHERE history_id=?", (entity_id,))
    elif entity_type == "certification":
        r = _row(con, "SELECT employee_pk,NULL requirement_id FROM certifications WHERE certification_id=?", (entity_id,))
    elif entity_type == "override":
        r = _row(con, "SELECT employee_pk,requirement_id FROM assessment_overrides WHERE override_id=?", (entity_id,))
    else:
        r = None
    return (int(r["employee_pk"]) if r and r["employee_pk"] is not None else None,
            int(r["requirement_id"]) if r and r["requirement_id"] is not None else None)


def evidence_count(con: sqlite3.Connection, entity_type: str, entity_id: int) -> int:
    return int(con.execute(
        "SELECT COUNT(*) FROM evidence_documents WHERE entity_type=? AND entity_id=? AND active_flag=1",
        (entity_type.lower(), entity_id),
    ).fetchone()[0])


def action_completion_ready(con: sqlite3.Connection, action: sqlite3.Row) -> dict[str, Any]:
    docs = evidence_count(con, "action", int(action["action_id"]))
    linked = False
    reason = ""
    action_type = str(action["action_type"] or "")
    if docs:
        linked = True
        reason = f"{docs} action evidence document(s) tersedia."
    elif action_type in {"Data Completion", "Position Review", "Reassessment", "Document Update", "Other"}:
        linked = True
        reason = "Action type tidak mensyaratkan dokumen khusus untuk closure."
    elif action_type in {"Evidence Validation", "Mapping Validation", "Requirement Validation"} and action["requirement_id"]:
        exists = con.execute(
            "SELECT 1 FROM assessment_validations WHERE employee_pk=? AND requirement_id=? AND active_flag=1 LIMIT 1",
            (action["employee_pk"], action["requirement_id"]),
        ).fetchone()
        if exists:
            linked = True
            reason = "Validasi aktif tersedia."
    elif action_type == "Training" and action["requirement_id"]:
        r = con.execute("SELECT training_id FROM training_requirements WHERE requirement_id=?", (action["requirement_id"],)).fetchone()
        if r:
            exists = con.execute(
                """SELECT 1 FROM training_history WHERE employee_pk=? AND COALESCE(training_id,training_pk)=?
                   AND lower(COALESCE(result_status,completion_status,'')) IN ('completed','passed','pass','competent','success','successful')
                   AND COALESCE(record_status,'Active')<>'Archived' LIMIT 1""",
                (action["employee_pk"], r[0]),
            ).fetchone()
            if exists:
                linked = True
                reason = "Completed Training History tersedia."
    elif action_type == "Certification Renewal":
        exists = con.execute(
            "SELECT 1 FROM certifications WHERE employee_pk=? AND previous_record_id IS NOT NULL AND COALESCE(record_status,'Active')<>'Archived' LIMIT 1",
            (action["employee_pk"],),
        ).fetchone()
        if exists:
            linked = True
            reason = "Renewed certification record tersedia."
    if not linked:
        reason = "Belum ada evidence/record penyelesaian yang dapat diverifikasi."
    return {"ready": linked, "evidence_documents": docs, "reason": reason}


def create_action(
    con: sqlite3.Connection,
    *, employee_pk: int, requirement_id: int | None, action_type: str,
    title: str | None, priority: str, recommendation: str | None,
    pic_user_id: str | None, due_date: str | None, remarks: str | None,
    source_decision_id: int | None, created_by: str,
) -> int:
    con.row_factory = sqlite3.Row
    employee = _row(con, "SELECT employee_name FROM employees WHERE employee_pk=?", (employee_pk,))
    if not employee:
        raise ValueError("Employee tidak ditemukan.")
    if action_type not in ACTION_TYPES:
        raise ValueError("Action Type tidak valid.")
    if priority not in ACTION_PRIORITIES:
        raise ValueError("Priority tidak valid.")
    assessment_id = None
    training_name = None
    if requirement_id is not None:
        req = _row(con, """SELECT tr.requirement_id,tc.training_name FROM training_requirements tr
                            JOIN training_catalog tc ON tc.training_id=tr.training_id WHERE tr.requirement_id=?""", (requirement_id,))
        if not req:
            raise ValueError("Training Requirement tidak ditemukan.")
        training_name = req["training_name"]
        a = _row(con, "SELECT assessment_id FROM competency_assessment WHERE employee_pk=? AND requirement_id=? ORDER BY assessment_id DESC LIMIT 1", (employee_pk, requirement_id))
        assessment_id = int(a["assessment_id"]) if a else None
        if source_decision_id is None:
            d = _row(con, "SELECT decision_id FROM decision_assessment_current WHERE employee_pk=? AND requirement_id=? ORDER BY decision_id DESC LIMIT 1", (employee_pk, requirement_id))
            source_decision_id = int(d["decision_id"]) if d else None
    if pic_user_id:
        if not con.execute("SELECT 1 FROM app_users WHERE user_id=? AND active_flag=1", (pic_user_id,)).fetchone():
            raise ValueError("PIC user tidak ditemukan/aktif.")
    if not title:
        title = f"{action_type} — {training_name or employee['employee_name']}"
    cur = con.execute(
        """INSERT INTO actions(employee_pk,requirement_id,assessment_id,source_decision_id,action_type,title,priority,
              recommendation,pic_user_id,due_date,status,remarks,created_by,updated_by)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'Open', ?,?,?)""",
        (employee_pk, requirement_id, assessment_id, source_decision_id, action_type, title.strip(), priority,
         recommendation, pic_user_id, due_date, remarks, created_by, created_by),
    )
    action_id = int(cur.lastrowid)
    con.execute(
        """INSERT INTO action_history(action_id,event_type,new_status,new_pic_user_id,remarks,changed_by)
           VALUES (?,?,?,?,?,?)""",
        (action_id, "Created", "Open", pic_user_id, remarks, created_by),
    )
    return action_id


def update_action(
    con: sqlite3.Connection, action_id: int, *, status: str | None = None,
    pic_user_id: str | None | object = ..., due_date: str | None | object = ...,
    priority: str | None = None, remarks: str | None = None, no_action_reason: str | None = None,
    changed_by: str,
) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    action = _row(con, "SELECT * FROM actions WHERE action_id=?", (action_id,))
    if not action:
        raise ValueError("Action tidak ditemukan.")
    if "is_deleted" in action.keys() and int(action["is_deleted"] or 0):
        raise ValueError("Action sudah dihapus. Restore Action terlebih dahulu sebelum memperbaruinya.")
    old_status = action["status"]
    old_pic = action["pic_user_id"]
    new_status = status or old_status
    new_pic = old_pic if pic_user_id is ... else pic_user_id
    new_due = action["due_date"] if due_date is ... else due_date
    new_priority = priority or action["priority"]
    if new_status not in ACTION_STATUSES:
        raise ValueError("Action Status tidak valid.")
    if new_priority not in ACTION_PRIORITIES:
        raise ValueError("Priority tidak valid.")
    if new_pic and not con.execute("SELECT 1 FROM app_users WHERE user_id=? AND active_flag=1", (new_pic,)).fetchone():
        raise ValueError("PIC user tidak ditemukan/aktif.")
    if new_status == "No Action Required" and not (no_action_reason or remarks or action["no_action_reason"]):
        raise ValueError("Alasan wajib diisi untuk No Action Required.")
    # Closed is a terminal/read-only state. Reopening is an explicit transition
    # back to Open and always requires a reason for traceability.
    if old_status == "Closed" and new_status != "Closed":
        if new_status != "Open":
            raise ValueError("Action Closed hanya dapat dibuka kembali ke status Open.")
        if not (remarks and str(remarks).strip()):
            raise ValueError("Alasan Reopen wajib diisi untuk Action yang sudah Closed.")
    if new_status == "Closed":
        readiness = action_completion_ready(con, action)
        if old_status not in {"Completed", "No Action Required"}:
            raise ValueError("Action harus Completed sebelum Closed.")
        if old_status == "Completed" and not readiness["ready"]:
            raise ValueError(f"Action belum dapat Closed: {readiness['reason']}")
    completed_at = action["completed_at"]
    closed_at = action["closed_at"]
    if new_status == "Completed" and old_status != "Completed":
        completed_at = datetime.now().isoformat(timespec="seconds")
    if new_status == "Closed" and old_status != "Closed":
        closed_at = datetime.now().isoformat(timespec="seconds")
    # Reopen starts a new active lifecycle. Historical closure remains preserved
    # in action_history, while current-row terminal timestamps are cleared.
    if old_status == "Closed" and new_status == "Open":
        completed_at = None
        closed_at = None
    elif old_status == "Completed" and new_status not in {"Completed", "Closed"}:
        completed_at = None
    con.execute(
        """UPDATE actions SET status=?,pic_user_id=?,due_date=?,priority=?,remarks=COALESCE(?,remarks),
             no_action_reason=COALESCE(?,no_action_reason),updated_by=?,updated_at=CURRENT_TIMESTAMP,
             completed_at=?,closed_at=? WHERE action_id=?""",
        (new_status, new_pic, new_due, new_priority, remarks, no_action_reason, changed_by, completed_at, closed_at, action_id),
    )
    event = "Updated"
    if new_status != old_status:
        event = "Status Changed"
    elif new_pic != old_pic:
        event = "PIC Changed"
    con.execute(
        """INSERT INTO action_history(action_id,event_type,old_status,new_status,old_pic_user_id,new_pic_user_id,remarks,changed_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (action_id, event, old_status, new_status, old_pic, new_pic, remarks or no_action_reason, changed_by),
    )
    return {"action_id": action_id, "old_status": old_status, "new_status": new_status, "pic_user_id": new_pic}


def refresh_notifications(con: sqlite3.Connection) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    today = date.today()
    generated: list[dict[str, Any]] = []

    # Certification reminders. Historical/no-action records are intentionally excluded.
    certs = con.execute(
        """SELECT c.certification_id,c.employee_pk,e.employee_name,
                  COALESCE(cc.certification_name,c.certification_name_raw) certification_name,
                  COALESCE(c.expiry_date,c.expired_date) expiry_date,
                  COALESCE(c.renewal_status,'') renewal_status
           FROM certifications c JOIN employees e ON e.employee_pk=c.employee_pk
           LEFT JOIN certification_catalog cc ON cc.certification_catalog_id=c.certification_catalog_id
           WHERE e.active_flag=1 AND COALESCE(e.employment_status,'Active')='Active'
             AND COALESCE(c.record_status,'Active')<>'Archived'
             AND COALESCE(c.certification_status,c.status,'')<>'Superseded'
             AND lower(COALESCE(c.renewal_status,'')) NOT IN ('completed','not renewed','no action required')
             AND COALESCE(c.expiry_date,c.expired_date) IS NOT NULL"""
    ).fetchall()
    for r in certs:
        try:
            exp = date.fromisoformat(str(r["expiry_date"])[:10])
        except Exception:
            continue
        days = (exp - today).days
        if days > 180:
            continue
        if days < 0:
            severity, bucket, title = "Critical", "expired", "Certification Expired"
            msg = f"{r['employee_name']} — {r['certification_name']} expired {abs(days)} hari lalu."
        elif days <= 30:
            severity, bucket, title = "High", "30d", "Certification Urgent"
            msg = f"{r['employee_name']} — {r['certification_name']} expires in {days} day(s)."
        elif days <= 90:
            severity, bucket, title = "Attention", "90d", "Certification Near Expiry"
            msg = f"{r['employee_name']} — {r['certification_name']} expires in {days} day(s)."
        else:
            severity, bucket, title = "Info", "180d", "Certification Renewal Planning"
            msg = f"{r['employee_name']} — {r['certification_name']} expires in {days} day(s)."
        generated.append(dict(key=f"cert:{r['certification_id']}:{bucket}", notification_type="Certification", source_type="certification", source_id=r["certification_id"], employee_pk=r["employee_pk"], severity=severity, title=title, message=msg, due_date=str(exp)))

    # Action due-date reminders.
    actions = con.execute(
        """SELECT a.*,e.employee_name FROM actions a JOIN employees e ON e.employee_pk=a.employee_pk
           WHERE COALESCE(a.is_deleted,0)=0
             AND a.status NOT IN ('Completed','Closed','Cancelled','No Action Required') AND a.due_date IS NOT NULL"""
    ).fetchall()
    for a in actions:
        try:
            due = date.fromisoformat(str(a["due_date"])[:10])
        except Exception:
            continue
        days = (due - today).days
        if days > 14:
            continue
        if days < 0:
            severity, bucket, title = "Critical", "overdue", "Action Overdue"
            msg = f"{a['employee_name']} — {a['title']} overdue {abs(days)} day(s)."
        elif days <= 3:
            severity, bucket, title = "High", "3d", "Action Due Soon"
            msg = f"{a['employee_name']} — {a['title']} due in {days} day(s)."
        elif days <= 7:
            severity, bucket, title = "Attention", "7d", "Action Due This Week"
            msg = f"{a['employee_name']} — {a['title']} due in {days} day(s)."
        else:
            severity, bucket, title = "Info", "14d", "Upcoming Action"
            msg = f"{a['employee_name']} — {a['title']} due in {days} day(s)."
        generated.append(dict(key=f"action:{a['action_id']}:{bucket}", notification_type="Action", source_type="action", source_id=a["action_id"], employee_pk=a["employee_pk"], severity=severity, title=title, message=msg, due_date=str(due)))

    # Pending assessment states are operational reminders.
    pending = con.execute(
        """SELECT employee_pk,employee_name,assessment_state,updated_at FROM employees
           WHERE active_flag=1 AND assessment_state IN ('Pending Initial Assessment','Pending Reassessment')"""
    ).fetchall()
    for e in pending:
        sev = "Attention" if e["assessment_state"] == "Pending Reassessment" else "Info"
        generated.append(dict(key=f"assessment:{e['employee_pk']}:{e['assessment_state']}", notification_type="Assessment", source_type="employee", source_id=e["employee_pk"], employee_pk=e["employee_pk"], severity=sev, title=e["assessment_state"], message=f"{e['employee_name']} memerlukan tindak lanjut assessment.", due_date=None))

    # Current validation queue.
    vals = con.execute(
        """SELECT ca.assessment_id,ca.employee_pk,e.employee_name,ca.training_name
           FROM competency_assessment ca JOIN employees e ON e.employee_pk=ca.employee_pk
           WHERE ca.coverage_status='Validation Required' AND e.active_flag=1"""
    ).fetchall()
    for v in vals:
        generated.append(dict(key=f"validation:{v['assessment_id']}", notification_type="Validation", source_type="assessment", source_id=v["assessment_id"], employee_pk=v["employee_pk"], severity="Attention", title="Validation Required", message=f"{v['employee_name']} — {v['training_name']} menunggu validasi.", due_date=None))

    current_keys = {x["key"] for x in generated}
    existing_keys = {r[0] for r in con.execute("SELECT notification_key FROM notifications WHERE active_flag=1").fetchall()}
    stale = existing_keys - current_keys
    if stale:
        con.executemany("UPDATE notifications SET active_flag=0,updated_at=CURRENT_TIMESTAMP WHERE notification_key=?", [(k,) for k in stale])
    for n in generated:
        con.execute(
            """INSERT INTO notifications(notification_key,notification_type,source_type,source_id,employee_pk,severity,title,message,due_date,active_flag)
               VALUES (?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(notification_key) DO UPDATE SET notification_type=excluded.notification_type,
                 source_type=excluded.source_type,source_id=excluded.source_id,employee_pk=excluded.employee_pk,
                 severity=excluded.severity,title=excluded.title,message=excluded.message,due_date=excluded.due_date,
                 active_flag=1,updated_at=CURRENT_TIMESTAMP""",
            (n["key"], n["notification_type"], n["source_type"], n["source_id"], n["employee_pk"], n["severity"], n["title"], n["message"], n["due_date"]),
        )
    return {
        "active": len(generated),
        "critical": sum(1 for x in generated if x["severity"] == "Critical"),
        "high": sum(1 for x in generated if x["severity"] == "High"),
        "attention": sum(1 for x in generated if x["severity"] == "Attention"),
        "info": sum(1 for x in generated if x["severity"] == "Info"),
    }
