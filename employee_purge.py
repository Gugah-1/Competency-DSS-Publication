from __future__ import annotations

import sqlite3


EMPLOYEE_PURGE_TABLES = (
    "employee_position_history", "training_history", "certifications",
    "training_certification_mapping", "competency_assessment", "recommendations",
    "training_status_current", "decision_assessment_current", "recommendation_current",
    "assessment_runs", "assessment_history", "assessment_validations",
    "assessment_overrides", "actions", "evidence_documents", "notifications",
)


def is_test_employee_code(employee_code: str) -> bool:
    code = (employee_code or "").strip().upper()
    return code.startswith(("UAT-", "TEST-", "DUMMY-"))


def employee_purge_impact(con: sqlite3.Connection, employee_pk: int) -> dict[str, int]:
    impact: dict[str, int] = {}
    for table in EMPLOYEE_PURGE_TABLES:
        columns = {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if "employee_pk" in columns:
            impact[table] = int(con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE employee_pk=?", (employee_pk,)
            ).fetchone()[0])
    impact["action_history"] = int(con.execute(
        "SELECT COUNT(*) FROM action_history WHERE action_id IN "
        "(SELECT action_id FROM actions WHERE employee_pk=?)", (employee_pk,)
    ).fetchone()[0])
    return impact


def purge_employee_records(con: sqlite3.Connection, employee_pk: int) -> dict[str, int]:
    """Delete one already-authorized test employee and every FK-dependent row.

    The caller owns backup creation, authorization, confirmation, notification
    refresh, commit/rollback, audit logging, and evidence-file quarantine.
    """
    deleted: dict[str, int] = {}
    deleted["evidence_documents"] = con.execute(
        "DELETE FROM evidence_documents WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["notifications"] = con.execute(
        "DELETE FROM notifications WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["action_history"] = con.execute(
        "DELETE FROM action_history WHERE action_id IN "
        "(SELECT action_id FROM actions WHERE employee_pk=?)", (employee_pk,)
    ).rowcount
    deleted["actions"] = con.execute(
        "DELETE FROM actions WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["recommendations"] = con.execute(
        "DELETE FROM recommendations WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["recommendation_current"] = con.execute(
        "DELETE FROM recommendation_current WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["assessment_history"] = con.execute(
        "DELETE FROM assessment_history WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["assessment_validations"] = con.execute(
        "DELETE FROM assessment_validations WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["assessment_overrides"] = con.execute(
        "DELETE FROM assessment_overrides WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["assessment_runs"] = con.execute(
        "DELETE FROM assessment_runs WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["training_certification_mapping"] = con.execute(
        "DELETE FROM training_certification_mapping WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["competency_assessment"] = con.execute(
        "DELETE FROM competency_assessment WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["decision_assessment_current"] = con.execute(
        "DELETE FROM decision_assessment_current WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["training_status_current"] = con.execute(
        "DELETE FROM training_status_current WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["certifications"] = con.execute(
        "DELETE FROM certifications WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["training_history"] = con.execute(
        "DELETE FROM training_history WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["employee_position_history"] = con.execute(
        "DELETE FROM employee_position_history WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    deleted["employees"] = con.execute(
        "DELETE FROM employees WHERE employee_pk=?", (employee_pk,)
    ).rowcount
    if deleted["employees"] != 1:
        raise RuntimeError("Employee tidak berhasil dihapus.")
    return deleted
