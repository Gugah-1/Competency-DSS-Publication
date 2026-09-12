from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

ENGINE_VERSION = "v22.4-decision-engine-v2"
FINAL_SUCCESS_STATUSES = {"completed", "passed", "pass", "competent", "success", "successful"}
IN_PROGRESS_STATUSES = {"scheduled", "in progress", "in_progress", "ongoing", "planned"}
FAILED_STATUSES = {"failed", "fail", "not passed", "not competent", "cancelled", "canceled"}
ALLOWED_OVERRIDE_STATUSES = {"Covered", "Potential Gap", "Validation Required", "Not Assessed", "Expired"}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _is_mandatory(requirement_type: Any, requirement_code: Any) -> bool:
    return _norm(requirement_type) == "mandatory" or str(requirement_code or "").strip() == "★"


def _training_signal(con: sqlite3.Connection, employee_pk: int, training_id: int) -> dict[str, Any] | None:
    """Return the strongest current training signal for an exact requirement training_id.

    A completed/passed record is fulfillment evidence. Scheduled/In Progress and Failed are
    operational signals, not fulfillment evidence, but they influence the recommendation.
    """
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT history_id, COALESCE(training_id,training_pk) training_id,
               COALESCE(result_status,completion_status) result_status,
               completion_date, training_date, valid_until, certificate_reference,
               evidence_status, evidence_note, evidence_path, provider, remarks
        FROM training_history
        WHERE employee_pk=?
          AND COALESCE(training_id,training_pk)=?
          AND COALESCE(record_status,'Active') <> 'Archived'
        ORDER BY COALESCE(completion_date,training_date,'') DESC, history_id DESC
        """,
        (employee_pk, training_id),
    ).fetchall()
    if not rows:
        return None

    today = date.today()
    success: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []

    for row in rows:
        status_norm = _norm(row["result_status"])
        valid_until = _parse_date(row["valid_until"])
        item = {
            "history_id": int(row["history_id"]),
            "result_status": row["result_status"] or "",
            "completion_date": row["completion_date"] or row["training_date"],
            "valid_until": row["valid_until"],
            "expired": bool(valid_until and valid_until < today),
            "certificate_reference": row["certificate_reference"],
            "evidence_status": row["evidence_status"],
            "evidence_path": row["evidence_path"],
            "provider": row["provider"],
            "remarks": row["remarks"],
        }
        if status_norm in FINAL_SUCCESS_STATUSES:
            success.append(item)
        elif status_norm in IN_PROGRESS_STATUSES:
            progress.append(item)
        elif status_norm in FAILED_STATUSES:
            failed.append(item)
        else:
            other.append(item)

    # Prefer valid success over expired success; rows were already ordered newest first.
    valid_success = [x for x in success if not x["expired"]]
    if valid_success:
        return {**valid_success[0], "signal_type": "Fulfilled"}
    if success:
        return {**success[0], "signal_type": "Expired Fulfillment"}
    if progress:
        return {**progress[0], "signal_type": "In Progress"}
    if failed:
        return {**failed[0], "signal_type": "Failed"}
    if other:
        return {**other[0], "signal_type": "Recorded - Unresolved"}
    return None


def _catalog_id_for_candidate(con: sqlite3.Connection, candidate: str | None) -> int | None:
    if not candidate:
        return None
    row = con.execute(
        "SELECT certification_catalog_id FROM certification_catalog WHERE lower(trim(certification_name))=lower(trim(?)) LIMIT 1",
        (candidate,),
    ).fetchone()
    return int(row[0]) if row else None


def _latest_validation(
    con: sqlite3.Connection,
    employee_pk: int,
    requirement_id: int,
) -> sqlite3.Row | None:
    con.row_factory = sqlite3.Row
    # Employee-specific validation takes precedence over a reusable global validation.
    return con.execute(
        """
        SELECT * FROM assessment_validations
        WHERE requirement_id=? AND active_flag=1
          AND ((scope='Employee' AND employee_pk=?) OR scope='Global')
        ORDER BY CASE WHEN scope='Employee' AND employee_pk=? THEN 0 ELSE 1 END,
                 validated_at DESC, validation_id DESC
        LIMIT 1
        """,
        (requirement_id, employee_pk, employee_pk),
    ).fetchone()


def _mapping_for(con: sqlite3.Connection, employee_pk: int, req: sqlite3.Row) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    existing = con.execute(
        """
        SELECT mapping_id, certification_catalog_id, certification_candidate, mapping_category, validation_status
        FROM training_certification_mapping
        WHERE employee_pk=? AND requirement_id=?
        ORDER BY mapping_id DESC LIMIT 1
        """,
        (employee_pk, req["requirement_id"]),
    ).fetchone()

    if existing:
        mapping_id = int(existing["mapping_id"])
        base_category = existing["mapping_category"] or req["mapping_category"] or "Unmapped Candidate"
        candidate = existing["certification_candidate"] or req["cert_candidate"]
        catalog_id = existing["certification_catalog_id"] or _catalog_id_for_candidate(con, candidate)
        base_validation_status = existing["validation_status"] or req["validation_status"]
    else:
        base_category = req["mapping_category"] or "Unmapped Candidate"
        candidate = req["cert_candidate"]
        catalog_id = _catalog_id_for_candidate(con, candidate)
        cur = con.execute(
            """
            INSERT INTO training_certification_mapping(
                employee_pk,requirement_id,certification_catalog_id,training_name,
                certification_candidate,mapping_category,validation_status,source_file
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                employee_pk,
                req["requirement_id"],
                catalog_id,
                req["training_name"],
                candidate,
                base_category,
                req["validation_status"],
                "v22.4 Decision Engine v2",
            ),
        )
        mapping_id = int(cur.lastrowid)
        base_validation_status = req["validation_status"]

    validation = _latest_validation(con, employee_pk, int(req["requirement_id"]))
    effective_category = str(base_category)
    validation_id = None
    validation_decision = None
    validation_scope = None
    if validation:
        validation_id = int(validation["validation_id"])
        validation_decision = validation["decision"]
        validation_scope = validation["scope"]
        d = _norm(validation_decision)
        if d in {"confirm equivalent", "confirmed equivalent", "equivalent", "validated", "approve", "approved"}:
            effective_category = "Validated Direct Match"
        elif d in {"not equivalent", "rejected", "reject", "not match", "no match"}:
            effective_category = "Rejected Match"
        elif d in {"need more evidence", "needs more evidence", "pending", "defer"}:
            effective_category = "Potential Match"

    return {
        "mapping_id": mapping_id,
        "base_category": base_category,
        "category": effective_category,
        "candidate": candidate,
        "catalog_id": int(catalog_id) if catalog_id is not None else None,
        "validation_status": base_validation_status,
        "validation_id": validation_id,
        "validation_decision": validation_decision,
        "validation_scope": validation_scope,
    }


def _certification_evidence(
    con: sqlite3.Connection,
    employee_pk: int,
    catalog_id: int | None,
    candidate: str | None,
) -> dict[str, Any] | None:
    con.row_factory = sqlite3.Row
    clauses: list[str] = []
    params: list[Any] = [employee_pk]
    if catalog_id is not None:
        clauses.append("c.certification_catalog_id=?")
        params.append(catalog_id)
    if candidate:
        clauses.append("lower(trim(COALESCE(cc.certification_name,c.certification_name_raw,'')))=lower(trim(?))")
        params.append(candidate)
    if not clauses:
        return None

    rows = con.execute(
        f"""
        SELECT c.certification_id, c.certification_catalog_id,
               COALESCE(cc.certification_name,c.certification_name_raw) certification_name,
               COALESCE(c.issue_date,c.certification_date) issue_date,
               COALESCE(c.expiry_date,c.expired_date) expiry_date,
               COALESCE(c.certification_status,c.status) stored_status,
               c.renewal_status, c.reason_code, c.remarks, c.evidence_path, c.record_status
        FROM certifications c
        LEFT JOIN certification_catalog cc ON cc.certification_catalog_id=c.certification_catalog_id
        WHERE c.employee_pk=?
          AND COALESCE(c.record_status,'Active') <> 'Archived'
          AND lower(COALESCE(c.certification_status,c.status,'')) NOT IN ('superseded','archived')
          AND ({' OR '.join(clauses)})
        ORDER BY COALESCE(c.expiry_date,c.expired_date,'9999-12-31') DESC,
                 COALESCE(c.issue_date,c.certification_date,'') DESC,
                 c.certification_id DESC
        """,
        tuple(params),
    ).fetchall()
    if not rows:
        return None

    today = date.today()
    row = rows[0]
    expiry = _parse_date(row["expiry_date"])
    expired = bool(expiry and expiry < today)
    days_to_expiry = (expiry - today).days if expiry else None
    near_expiry = bool(days_to_expiry is not None and 0 <= days_to_expiry <= 90)
    return {
        "certification_id": int(row["certification_id"]),
        "certification_name": row["certification_name"],
        "issue_date": row["issue_date"],
        "expiry_date": row["expiry_date"],
        "expired": expired,
        "near_expiry": near_expiry,
        "days_to_expiry": days_to_expiry,
        "renewal_status": row["renewal_status"],
        "stored_status": row["stored_status"],
        "reason_code": row["reason_code"],
        "remarks": row["remarks"],
        "evidence_path": row["evidence_path"],
    }


def _latest_override(con: sqlite3.Connection, employee_pk: int, requirement_id: int) -> sqlite3.Row | None:
    con.row_factory = sqlite3.Row
    return con.execute(
        """
        SELECT * FROM assessment_overrides
        WHERE employee_pk=? AND requirement_id=? AND active_flag=1
        ORDER BY approved_at DESC, override_id DESC LIMIT 1
        """,
        (employee_pk, requirement_id),
    ).fetchone()


def _priority_for(status: str, mandatory: bool) -> str:
    if status == "Covered":
        return "Low"
    if status in {"Potential Gap", "Expired"}:
        return "High" if mandatory else "Medium"
    if status == "Validation Required":
        return "Validation"
    return "Not Assessed"


def _default_action(status: str) -> str:
    return {
        "Covered": "Maintain evidence and monitor validity",
        "Potential Gap": "Validate fulfillment and plan training/certification if not fulfilled",
        "Validation Required": "Validate requirement-evidence relationship",
        "Not Assessed": "Complete mapping or supporting evidence",
        "Expired": "Renew/refresh evidence or validate continued applicability",
    }.get(status, "Review assessment")


def _decision(
    req: sqlite3.Row,
    training: dict[str, Any] | None,
    mapping: dict[str, Any],
    cert: dict[str, Any] | None,
    override: sqlite3.Row | None,
) -> dict[str, Any]:
    mandatory = _is_mandatory(req["requirement_type"], req["requirement_code"])
    mapping_category = str(mapping.get("category") or "Unmapped Candidate").strip()
    mapping_norm = _norm(mapping_category)

    if override:
        override_status = str(override["override_status"])
        if override_status not in ALLOWED_OVERRIDE_STATUSES:
            override_status = "Validation Required"
        return {
            "coverage": override_status,
            "priority": _priority_for(override_status, mandatory),
            "action": _default_action(override_status),
            "rationale": f"Authorized assessment override applied. Reason: {override['reason']}",
            "confidence": "Authorized Override",
            "training_status": training["result_status"] if training else "No matching Training History",
            "assessment_source": "Override",
        }

    # Exact Training History is direct evidence for the requirement, independent of certificate-name mapping.
    if training:
        signal_type = training.get("signal_type")
        if signal_type == "Fulfilled":
            return {
                "coverage": "Covered",
                "priority": "Low",
                "action": "Maintain training evidence and monitor validity",
                "rationale": "A matching completed/passed Training History record provides direct fulfillment evidence for this requirement.",
                "confidence": "High",
                "training_status": training["result_status"] or "Completed",
                "assessment_source": "System",
            }
        if signal_type == "Expired Fulfillment":
            return {
                "coverage": "Expired",
                "priority": "High" if mandatory else "Medium",
                "action": "Plan refresher/renewal training and validate continued applicability",
                "rationale": "A matching completed Training History record exists, but its validity period has expired.",
                "confidence": "High",
                "training_status": f"{training['result_status']} - Expired",
                "assessment_source": "System",
            }
        if signal_type == "In Progress":
            return {
                "coverage": "Potential Gap",
                "priority": "High" if mandatory else "Medium",
                "action": "Monitor scheduled/in-progress training until successful completion",
                "rationale": "The exact required training is scheduled or in progress, but successful completion evidence is not yet available.",
                "confidence": "Medium",
                "training_status": training["result_status"],
                "assessment_source": "System",
            }
        if signal_type == "Failed":
            return {
                "coverage": "Potential Gap",
                "priority": "High" if mandatory else "Medium",
                "action": "Plan retake/retraining and monitor successful completion",
                "rationale": "A matching Training History record exists but the latest usable result is failed/cancelled, so fulfillment is not established.",
                "confidence": "High",
                "training_status": training["result_status"],
                "assessment_source": "System",
            }

    is_direct = "direct" in mapping_norm and "potential" not in mapping_norm
    is_validated_direct = "validated direct" in mapping_norm
    is_potential = "potential" in mapping_norm
    is_rejected = "rejected" in mapping_norm
    assessment_source = "Validated" if mapping.get("validation_id") else "System"

    if is_direct or is_validated_direct:
        if cert:
            if cert["expired"]:
                renewal = _norm(cert.get("renewal_status"))
                if renewal in {"renewal planned", "planned", "renewal pending", "pending", "in progress", "renewal in progress"}:
                    action = "Monitor renewal in progress; keep status expired until valid replacement evidence is recorded"
                else:
                    action = "Initiate certification renewal or validate continued applicability"
                return {
                    "coverage": "Expired",
                    "priority": "High" if mandatory else "Medium",
                    "action": action,
                    "rationale": "Mapped certification evidence exists, but the evidence has passed its expiry date.",
                    "confidence": "High" if is_direct else "Validated",
                    "training_status": "No fulfilled matching Training History",
                    "assessment_source": assessment_source,
                }
            if cert["near_expiry"]:
                return {
                    "coverage": "Covered",
                    "priority": "Medium",
                    "action": "Plan certification renewal and monitor expiry",
                    "rationale": f"Mapped certification evidence is currently valid but will expire in {cert['days_to_expiry']} day(s).",
                    "confidence": "High" if is_direct else "Validated",
                    "training_status": "No fulfilled matching Training History",
                    "assessment_source": assessment_source,
                }
            return {
                "coverage": "Covered",
                "priority": "Low",
                "action": "Maintain certification evidence and monitor validity",
                "rationale": "Valid mapped certification evidence is available for the applicable requirement.",
                "confidence": "High" if is_direct else "Validated",
                "training_status": "No fulfilled matching Training History",
                "assessment_source": assessment_source,
            }
        return {
            "coverage": "Potential Gap",
            "priority": "High" if mandatory else "Medium",
            "action": "Validate fulfillment and plan training/certification if not fulfilled",
            "rationale": "The requirement is applicable and has an established evidence mapping, but no valid fulfillment evidence was found in the confirmed data.",
            "confidence": "Medium" if is_direct else "Validated Mapping / Missing Evidence",
            "training_status": "No matching Training History",
            "assessment_source": assessment_source,
        }

    if is_potential:
        cert_note = " A candidate certification record is present." if cert else ""
        return {
            "coverage": "Validation Required",
            "priority": "Validation",
            "action": "Validate requirement-evidence equivalence before concluding fulfillment",
            "rationale": "The requirement has a potential evidence relationship that has not been confirmed by TCD." + cert_note,
            "confidence": "Medium",
            "training_status": "No fulfilled matching Training History",
            "assessment_source": assessment_source,
        }

    if is_rejected:
        return {
            "coverage": "Not Assessed",
            "priority": "Not Assessed",
            "action": "Establish an alternative valid mapping or provide direct Training History evidence",
            "rationale": "The previous candidate relationship has been explicitly validated as not equivalent, and no alternative fulfillment evidence is established.",
            "confidence": "Validated Rejection",
            "training_status": "No fulfilled matching Training History",
            "assessment_source": "Validated",
        }

    return {
        "coverage": "Not Assessed",
        "priority": "Not Assessed",
        "action": "Complete mapping or supporting evidence",
        "rationale": "The requirement does not yet have a sufficiently established evidence mapping for assessment.",
        "confidence": "Low",
        "training_status": "No fulfilled matching Training History",
        "assessment_source": assessment_source,
    }


def assessment_readiness(con: sqlite3.Connection, employee_pk: int) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    emp = con.execute(
        """
        SELECT e.employee_pk,e.employee_code,e.employee_name,e.current_position_id,e.active_flag,
               e.assessment_state,e.initial_data_confirmed,e.initial_data_confirmed_at,
               p.position_name
        FROM employees e LEFT JOIN positions p ON p.position_id=e.current_position_id
        WHERE e.employee_pk=?
        """,
        (employee_pk,),
    ).fetchone()
    if not emp:
        raise ValueError("Employee tidak ditemukan.")
    position_id = emp["current_position_id"]
    req_count = 0
    if position_id:
        req_count = con.execute(
            """
            SELECT COUNT(*) FROM training_requirements
            WHERE active_flag=1
              AND COALESCE(requirement_status,'Active')='Active'
              AND COALESCE(position_standard_id,position_id)=?
              AND (effective_from IS NULL OR date(effective_from) <= date('now'))
              AND (effective_to IS NULL OR date(effective_to) >= date('now'))
            """,
            (position_id,),
        ).fetchone()[0]
    training_count = con.execute(
        "SELECT COUNT(*) FROM training_history WHERE employee_pk=? AND COALESCE(record_status,'Active') <> 'Archived'",
        (employee_pk,),
    ).fetchone()[0]
    cert_count = con.execute(
        "SELECT COUNT(*) FROM certifications WHERE employee_pk=? AND COALESCE(record_status,'Active') <> 'Archived'",
        (employee_pk,),
    ).fetchone()[0]
    return {
        "employee_pk": emp["employee_pk"],
        "employee_code": emp["employee_code"],
        "employee_name": emp["employee_name"],
        "position_id": position_id,
        "position_name": emp["position_name"],
        "active": bool(emp["active_flag"]),
        "assessment_state": emp["assessment_state"] or "Pending Initial Assessment",
        "initial_data_confirmed": bool(emp["initial_data_confirmed"]),
        "initial_data_confirmed_at": emp["initial_data_confirmed_at"],
        "applicable_requirements": int(req_count),
        "training_history_records": int(training_count),
        "certification_records": int(cert_count),
        "can_confirm_initial": bool(emp["active_flag"] and position_id and req_count > 0),
        "engine_version": ENGINE_VERSION,
    }


def run_employee_assessment(
    con: sqlite3.Connection,
    employee_pk: int,
    run_type: str,
    user_id: str,
    notes: str | None = None,
) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    readiness = assessment_readiness(con, employee_pk)
    emp = con.execute(
        """
        SELECT e.*, p.position_name
        FROM employees e LEFT JOIN positions p ON p.position_id=e.current_position_id
        WHERE e.employee_pk=?
        """,
        (employee_pk,),
    ).fetchone()
    if not emp or not emp["active_flag"]:
        raise ValueError("Employee tidak aktif atau tidak ditemukan.")
    if not emp["current_position_id"]:
        raise ValueError("Employee belum memiliki current position.")
    if not readiness["initial_data_confirmed"]:
        raise ValueError("Initial data belum dikonfirmasi.")

    state_before = emp["assessment_state"] or "Pending Initial Assessment"
    run_type_norm = _norm(run_type)
    if run_type_norm.startswith("initial") and state_before != "Assessment Ready":
        raise ValueError(f"Initial Assessment hanya dapat dijalankan dari state Assessment Ready. State saat ini: {state_before}.")
    if "reassess" in run_type_norm and state_before != "Pending Reassessment":
        raise ValueError(f"Reassessment hanya dapat dijalankan dari state Pending Reassessment. State saat ini: {state_before}.")

    requirements = con.execute(
        """
        SELECT tr.*, tc.training_name
        FROM training_requirements tr
        JOIN training_catalog tc ON tc.training_id=tr.training_id
        WHERE tr.active_flag=1
          AND COALESCE(tr.requirement_status,'Active')='Active'
          AND COALESCE(tr.position_standard_id,tr.position_id)=?
          AND (tr.effective_from IS NULL OR date(tr.effective_from) <= date('now'))
          AND (tr.effective_to IS NULL OR date(tr.effective_to) >= date('now'))
        ORDER BY tr.requirement_id
        """,
        (emp["current_position_id"],),
    ).fetchall()
    if not requirements:
        raise ValueError("Tidak ada Training Requirement aktif untuk current position employee.")

    cur = con.execute(
        """
        INSERT INTO assessment_runs(
            employee_pk,run_type,state_before,state_after,initiated_by,initiated_at,notes,engine_version
        ) VALUES (?,?,?,'Running',?,CURRENT_TIMESTAMP,?,?)
        """,
        (employee_pk, run_type, state_before, user_id, notes, ENGINE_VERSION),
    )
    run_id = int(cur.lastrowid)

    # Current assessment rows are rebuilt for this employee. Before deleting them, detach
    # operational/history rows that intentionally survive reassessment. This avoids FK failures
    # when Actions still point to the previous current assessment/decision IDs.
    con.execute(
        "UPDATE actions SET assessment_id=NULL, source_decision_id=NULL, updated_at=CURRENT_TIMESTAMP "
        "WHERE employee_pk=?",
        (employee_pk,),
    )
    # Legacy recommendation rows are retained as historical artifacts, but their assessment_id
    # may refer to competency_assessment rows that are about to be replaced.
    con.execute("UPDATE recommendations SET assessment_id=NULL WHERE employee_pk=?", (employee_pk,))
    con.execute("DELETE FROM recommendation_current WHERE employee_pk=?", (employee_pk,))
    con.execute("DELETE FROM decision_assessment_current WHERE employee_pk=?", (employee_pk,))
    con.execute("DELETE FROM competency_assessment WHERE employee_pk=?", (employee_pk,))

    counts = {"Covered": 0, "Potential Gap": 0, "Validation Required": 0, "Not Assessed": 0, "Expired": 0}
    validated_count = 0
    override_count = 0
    evaluated_at = datetime.now().isoformat(timespec="seconds")

    for req in requirements:
        training = _training_signal(con, employee_pk, int(req["training_id"]))
        mapping = _mapping_for(con, employee_pk, req)
        cert = _certification_evidence(con, employee_pk, mapping.get("catalog_id"), mapping.get("candidate"))
        override = _latest_override(con, employee_pk, int(req["requirement_id"]))
        result = _decision(req, training, mapping, cert, override)
        coverage = result["coverage"]
        counts[coverage] = counts.get(coverage, 0) + 1
        if mapping.get("validation_id"):
            validated_count += 1
        if override:
            override_count += 1

        expiry_status = None
        cert_name = None
        cert_status = None
        cert_expiry = None
        cert_expiry_condition = None
        if cert:
            cert_name = cert["certification_name"]
            cert_expiry = cert["expiry_date"]
            if cert["expired"]:
                cert_status = "Expired"
                cert_expiry_condition = "Expired"
                expiry_status = "Expired"
            elif cert["near_expiry"]:
                cert_status = "Near Expiry"
                cert_expiry_condition = "≤90 days"
                expiry_status = "Near Expiry"
            else:
                cert_status = "Active"
                cert_expiry_condition = "Valid"
                expiry_status = "Active"
        elif mapping.get("candidate"):
            expiry_status = "No evidence"

        evidence_payload = {
            "training": {
                "history_id": training.get("history_id") if training else None,
                "status": training.get("result_status") if training else None,
                "signal": training.get("signal_type") if training else None,
                "valid_until": training.get("valid_until") if training else None,
            },
            "mapping": {
                "mapping_id": mapping.get("mapping_id"),
                "base_category": mapping.get("base_category"),
                "effective_category": mapping.get("category"),
                "validation_id": mapping.get("validation_id"),
                "validation_decision": mapping.get("validation_decision"),
            },
            "certification": {
                "certification_id": cert.get("certification_id") if cert else None,
                "name": cert_name,
                "status": cert_status,
                "expiry_date": cert_expiry,
                "renewal_status": cert.get("renewal_status") if cert else None,
            },
            "override": {
                "override_id": int(override["override_id"]) if override else None,
                "status": override["override_status"] if override else None,
            },
        }
        evidence_summary = json.dumps(evidence_payload, ensure_ascii=False, default=str)

        acur = con.execute(
            """
            INSERT INTO competency_assessment(
                employee_pk,requirement_id,training_name,position_tna,position_cert_db,
                requirement_type,requirement_code,mapping_category,certification_candidate,
                coverage_status,expiry_status,priority,recommendation,source_file,
                assessment_source,rationale,evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                employee_pk,
                req["requirement_id"],
                req["training_name"],
                emp["position_name"],
                emp["position_name"],
                req["requirement_type"],
                req["requirement_code"],
                mapping.get("category"),
                mapping.get("candidate"),
                coverage,
                expiry_status,
                result["priority"],
                result["action"],
                f"assessment_run:{run_id}",
                result["assessment_source"],
                result["rationale"],
                evaluated_at,
            ),
        )
        assessment_id = int(acur.lastrowid)
        dcur = con.execute(
            """
            INSERT INTO decision_assessment_current(
                employee_pk,employee_name,position_name,training_name,requirement_type,requirement_code,
                mapping_category,certification_name,certification_status,certification_expired_date,
                certification_expiry_condition,training_history_status,coverage_status,final_status,
                priority,decision_action,rationale,evidence_confidence,evaluated_at,
                requirement_id,training_evidence_id,certification_evidence_id,mapping_validation_id,
                override_id,assessment_source,evidence_summary,engine_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                employee_pk,
                emp["employee_name"],
                emp["position_name"],
                req["training_name"],
                req["requirement_type"],
                req["requirement_code"],
                mapping.get("category"),
                cert_name,
                cert_status,
                cert_expiry,
                cert_expiry_condition,
                result["training_status"],
                coverage,
                coverage,
                result["priority"],
                result["action"],
                result["rationale"],
                result["confidence"],
                evaluated_at,
                req["requirement_id"],
                training.get("history_id") if training else None,
                cert.get("certification_id") if cert else None,
                mapping.get("validation_id"),
                int(override["override_id"]) if override else None,
                result["assessment_source"],
                evidence_summary,
                ENGINE_VERSION,
            ),
        )
        decision_id = int(dcur.lastrowid)
        # Re-link surviving operational actions to the freshly generated current rows.
        con.execute(
            """UPDATE actions
               SET assessment_id=?, source_decision_id=?, updated_at=CURRENT_TIMESTAMP
               WHERE employee_pk=? AND requirement_id=?""",
            (assessment_id, decision_id, employee_pk, req["requirement_id"]),
        )
        con.execute(
            """
            INSERT INTO recommendation_current(
                decision_id,employee_pk,employee_name,position_name,issue_type,priority,
                recommendation,rationale,trigger_status,evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id,
                employee_pk,
                emp["employee_name"],
                emp["position_name"],
                coverage,
                result["priority"],
                result["action"],
                result["rationale"],
                coverage,
                evaluated_at,
            ),
        )
        con.execute(
            """
            INSERT INTO assessment_history(
                run_id,employee_pk,requirement_id,training_name,position_name,mapping_category,
                coverage_status,priority,recommendation,rationale,evidence_summary,evaluated_at,
                assessment_source,training_evidence_id,certification_evidence_id,mapping_validation_id,
                override_id,engine_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                employee_pk,
                req["requirement_id"],
                req["training_name"],
                emp["position_name"],
                mapping.get("category"),
                coverage,
                result["priority"],
                result["action"],
                result["rationale"],
                evidence_summary,
                evaluated_at,
                result["assessment_source"],
                training.get("history_id") if training else None,
                cert.get("certification_id") if cert else None,
                mapping.get("validation_id"),
                int(override["override_id"]) if override else None,
                ENGINE_VERSION,
            ),
        )

    con.execute(
        """
        UPDATE employees
        SET assessment_state='Assessed', initial_data_confirmed=1,
            last_assessment_at=CURRENT_TIMESTAMP, last_assessment_run_id=?, assessment_version='v22.4',
            updated_by=?, updated_at=CURRENT_TIMESTAMP
        WHERE employee_pk=?
        """,
        (run_id, user_id, employee_pk),
    )
    con.execute(
        """
        UPDATE assessment_runs
        SET state_after='Assessed',completed_at=CURRENT_TIMESTAMP,applicable_requirement_count=?,
            covered_count=?,potential_gap_count=?,validation_required_count=?,not_assessed_count=?,expired_count=?,
            validated_count=?,override_count=?,engine_version=?
        WHERE run_id=?
        """,
        (
            len(requirements),
            counts.get("Covered", 0),
            counts.get("Potential Gap", 0),
            counts.get("Validation Required", 0),
            counts.get("Not Assessed", 0),
            counts.get("Expired", 0),
            validated_count,
            override_count,
            ENGINE_VERSION,
            run_id,
        ),
    )
    return {
        "status": "success",
        "run_id": run_id,
        "employee_pk": employee_pk,
        "assessment_state": "Assessed",
        "engine_version": ENGINE_VERSION,
        "applicable_requirements": len(requirements),
        "distribution": counts,
        "validated_requirements": validated_count,
        "override_requirements": override_count,
    }
