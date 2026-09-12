
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd


def _log_import(con, source_file, dataset_type, rows_read, rows_imported, rows_rejected, status, message=""):
    # Compatible with the unified schema: data_import_log(import_id, source_name, source_type, record_count, imported_at, notes).
    notes = f"status={status}; imported={int(rows_imported)}; rejected={int(rows_rejected)}; {message}".strip()
    con.execute(
        """
        INSERT INTO data_import_log(source_name, source_type, record_count, imported_at, notes)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        """,
        (source_file, dataset_type, int(rows_read), notes),
    )


def import_certification_dataframe(con, df, source_file):
    df = df.copy()
    required = ["employee_id", "employee_name", "position", "certification_name",
                "held_by", "issuer_type", "certification_date", "expired_date", "note", "status"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Kolom certification belum lengkap: {', '.join(missing)}")

    df["employee_name"] = df["employee_name"].fillna("").astype(str).str.strip()
    df["position"] = df["position"].fillna("").astype(str).str.strip()
    df["certification_name"] = df["certification_name"].fillna("").astype(str).str.strip()
    df = df[df["employee_name"].ne("") & df["certification_name"].ne("")].copy()

    df["certification_date"] = pd.to_datetime(df["certification_date"], errors="coerce")
    df["expired_date"] = pd.to_datetime(df["expired_date"], errors="coerce")

    imported = 0
    rejected = 0

    for _, r in df.iterrows():
        # Position master
        pos = con.execute(
            "SELECT position_id FROM positions WHERE lower(position_name)=lower(?) LIMIT 1",
            (str(r["position"]),)
        ).fetchone()
        if pos:
            position_id = int(pos[0])
        else:
            con.execute(
                "INSERT INTO positions(position_name) VALUES (?)",
                (str(r["position"]),)
            )
            position_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Employee master: employee_id first, name fallback.
        emp = None
        employee_code = str(r["employee_id"]).strip() if pd.notna(r["employee_id"]) else ""
        if employee_code:
            emp = con.execute(
                "SELECT employee_pk FROM employees WHERE employee_code=? LIMIT 1",
                (employee_code,)
            ).fetchone()
        if not emp:
            emp = con.execute(
                """
                SELECT employee_pk FROM employees
                WHERE lower(employee_name)=lower(?) AND current_position_id=?
                LIMIT 1
                """,
                (str(r["employee_name"]), position_id)
            ).fetchone()
        if emp:
            employee_pk = int(emp[0])
            con.execute(
                """
                UPDATE employees
                SET employee_code=COALESCE(NULLIF(?,''), employee_code),
                    current_position_id=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE employee_pk=?
                """,
                (employee_code, position_id, employee_pk)
            )
        else:
            con.execute(
                """
                INSERT INTO employees(employee_code, employee_name, current_position_id, active_flag)
                VALUES (?, ?, ?, 1)
                """,
                (employee_code or None, str(r["employee_name"]), position_id)
            )
            employee_pk = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Certification catalog.
        cat = con.execute(
            "SELECT certification_catalog_id FROM certification_catalog WHERE lower(certification_name)=lower(?) LIMIT 1",
            (str(r["certification_name"]),)
        ).fetchone()
        if cat:
            catalog_id = int(cat[0])
        else:
            con.execute(
                "INSERT INTO certification_catalog(certification_name, active_flag) VALUES (?,1)",
                (str(r["certification_name"]),)
            )
            catalog_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Append certification records; don't silently overwrite historical records.
        con.execute(
            """
            INSERT INTO certifications
            (employee_pk, certification_catalog_id, employee_code_raw, employee_name_raw,
             certification_name_raw, held_by, issuer_type, certification_date,
             expired_date, note, status, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                employee_pk, catalog_id,
                employee_code or None, str(r["employee_name"]),
                str(r["certification_name"]),
                r.get("held_by"), r.get("issuer_type"),
                r["certification_date"].strftime("%Y-%m-%d") if pd.notna(r["certification_date"]) else None,
                r["expired_date"].strftime("%Y-%m-%d") if pd.notna(r["expired_date"]) else None,
                r.get("note"), r.get("status"), source_file
            )
        )
        imported += 1

    _log_import(con, source_file, "Certification", len(df), imported, rejected, "SUCCESS")
    con.commit()
    return imported, rejected


def import_tna_dataframe(con, df, source_file, effective_year=2026):
    df = df.copy()
    required = ["position_raw", "position_standard", "training_name",
                "requirement_type", "delivery_type", "regulatory_flag", "mapping_code"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Kolom TNA belum lengkap: {', '.join(missing)}")

    df = df.dropna(subset=["position_raw", "training_name"]).copy()
    imported = 0

    for _, r in df.iterrows():
        position_name = str(r["position_standard"]).strip()
        training_name = str(r["training_name"]).strip()
        if not position_name or not training_name:
            continue

        pos = con.execute(
            "SELECT position_id FROM positions WHERE lower(position_name)=lower(?) LIMIT 1",
            (position_name,)
        ).fetchone()
        if pos:
            position_id = int(pos[0])
        else:
            con.execute("INSERT INTO positions(position_name) VALUES (?)", (position_name,))
            position_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        tr = con.execute(
            "SELECT training_id FROM training_catalog WHERE lower(training_name)=lower(?) LIMIT 1",
            (training_name,)
        ).fetchone()
        if tr:
            training_id = int(tr[0])
        else:
            con.execute(
                "INSERT INTO training_catalog(training_name, active_flag) VALUES (?,1)",
                (training_name,)
            )
            training_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        exists = con.execute(
            """
            SELECT requirement_id FROM training_requirements
            WHERE training_id=? AND position_id=? AND requirement_code=?
              AND COALESCE(effective_year,0)=?
            LIMIT 1
            """,
            (
                training_id, position_id,
                str(r["mapping_code"]),
                int(effective_year),
            )
        ).fetchone()

        if exists:
            con.execute(
                """
                UPDATE training_requirements
                SET requirement_type=?, delivery_type=?, regulatory_flag=?,
                    position_raw=?, position_key=?, position_raw_tna=?,
                    effective_year=?, source_file=?
                WHERE requirement_id=?
                """,
                (
                    r["requirement_type"], r["delivery_type"], r["regulatory_flag"],
                    r["position_raw"], position_name.lower(), r["position_raw"],
                    int(effective_year), source_file, int(exists[0])
                )
            )
        else:
            con.execute(
                """
                INSERT INTO training_requirements
                (training_id, position_id, position_raw, position_key,
                 requirement_code, requirement_type, delivery_type, regulatory_flag,
                 position_raw_tna, effective_year, frequency_type, frequency_value,
                 source_file, active_flag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    training_id, position_id, str(r["position_raw"]),
                    position_name.lower(), str(r["mapping_code"]),
                    r["requirement_type"], r["delivery_type"], r["regulatory_flag"],
                    r["position_raw"], int(effective_year), None, None, source_file
                )
            )
        imported += 1

    _log_import(con, source_file, "Training/TNA", len(df), imported, len(df)-imported, "SUCCESS")
    con.commit()
    return imported, len(df)-imported


def import_training_history_dataframe(con, df, source_file):
    df = df.copy()
    required = ["employee_name", "training_name", "completion_date"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Kolom Training History belum lengkap: {', '.join(missing)}")

    df["employee_name"] = df["employee_name"].fillna("").astype(str).str.strip()
    df["training_name"] = df["training_name"].fillna("").astype(str).str.strip()
    df["completion_date"] = pd.to_datetime(df["completion_date"], errors="coerce")
    df = df[df["employee_name"].ne("") & df["training_name"].ne("") & df["completion_date"].notna()].copy()

    imported = 0
    rejected = 0

    for _, r in df.iterrows():
        emp = con.execute(
            "SELECT employee_pk FROM employees WHERE lower(employee_name)=lower(?) LIMIT 1",
            (str(r["employee_name"]),)
        ).fetchone()
        if not emp:
            rejected += 1
            continue

        employee_pk = int(emp[0])
        training = con.execute(
            "SELECT training_id FROM training_catalog WHERE lower(training_name)=lower(?) LIMIT 1",
            (str(r["training_name"]),)
        ).fetchone()
        training_id = int(training[0]) if training else None

        con.execute(
            """
            INSERT INTO training_history
            (employee_pk, training_id, training_pk, training_name_raw, training_name,
             completion_date, completion_year, completion_status,
             certificate_reference, provider, evidence_note, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                employee_pk, training_id, training_id,
                str(r["training_name"]), str(r["training_name"]),
                r["completion_date"].strftime("%Y-%m-%d"),
                int(r["completion_date"].year),
                str(r["completion_status"]) if "completion_status" in r and pd.notna(r["completion_status"]) else "Completed",
                str(r["certificate_reference"]) if "certificate_reference" in r and pd.notna(r["certificate_reference"]) else None,
                str(r["provider"]) if "provider" in r and pd.notna(r["provider"]) else None,
                str(r["evidence_note"]) if "evidence_note" in r and pd.notna(r["evidence_note"]) else None,
                source_file,
            )
        )
        imported += 1

    _log_import(con, source_file, "Training History", len(df), imported, rejected, "SUCCESS")
    con.commit()
    return imported, rejected


def import_employee_master_dataframe(con, df, source_file):
    """Upsert the core Employee Master and position/department context."""
    df = df.copy()
    # Accept common aliases while keeping a strict canonical contract.
    aliases = {
        "employee_id": ["employee_id","employee_code","nik","employee_code_raw"],
        "employee_name": ["employee_name","name","nama","nama_karyawan"],
        "position": ["position","position_name","jabatan","job_title"],
        "department": ["department","departemen","dept"],
        "active_flag": ["active_flag","status","active","is_active"],
    }
    rename = {}
    lower_cols = {str(c).strip().lower(): c for c in df.columns}
    for canonical, opts in aliases.items():
        for opt in opts:
            if opt in lower_cols:
                rename[lower_cols[opt]] = canonical
                break
    df = df.rename(columns=rename)
    required = ["employee_name","position"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Kolom Employee Master belum lengkap: {', '.join(missing)}")
    df["employee_name"] = df["employee_name"].fillna("").astype(str).str.strip()
    df["position"] = df["position"].fillna("").astype(str).str.strip()
    if "employee_id" in df.columns:
        df["employee_id"] = df["employee_id"].fillna("").astype(str).str.strip()
    else:
        df["employee_id"] = ""
    if "department" in df.columns:
        df["department"] = df["department"].fillna("").astype(str).str.strip()
    else:
        df["department"] = ""
    df = df[df["employee_name"].ne("") & df["position"].ne("")].copy()
    imported = rejected = 0
    for _, r in df.iterrows():
        pname, dept = str(r["position"]).strip(), str(r["department"]).strip()
        pos = con.execute("SELECT position_id FROM positions WHERE lower(position_name)=lower(?) LIMIT 1", (pname,)).fetchone()
        if pos:
            position_id = int(pos[0])
            if dept:
                con.execute("UPDATE positions SET department=COALESCE(NULLIF(?,''), department) WHERE position_id=?", (dept, position_id))
        else:
            con.execute("INSERT INTO positions(position_name, department) VALUES (?,?)", (pname, dept or None))
            position_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        code = str(r["employee_id"]).strip() or None
        emp = None
        if code:
            emp = con.execute("SELECT employee_pk FROM employees WHERE employee_code=? LIMIT 1", (code,)).fetchone()
        if not emp:
            emp = con.execute("SELECT employee_pk FROM employees WHERE lower(employee_name)=lower(?) LIMIT 1", (str(r["employee_name"]).strip(),)).fetchone()
        active = 1
        if "active_flag" in df.columns and pd.notna(r.get("active_flag")):
            v=str(r.get("active_flag")).strip().lower()
            active = 0 if v in {"0","false","inactive","nonactive","tidak aktif"} else 1
        if emp:
            con.execute("""UPDATE employees SET employee_code=COALESCE(NULLIF(?,''),employee_code), current_position_id=?, active_flag=?, updated_at=CURRENT_TIMESTAMP WHERE employee_pk=?""", (code or "", position_id, active, int(emp[0])))
        else:
            con.execute("INSERT INTO employees(employee_code, employee_name, current_position_id, active_flag) VALUES (?,?,?,?)", (code, str(r["employee_name"]).strip(), position_id, active))
        imported += 1
    _log_import(con, source_file, "Employee Master", len(df), imported, len(df)-imported, "SUCCESS")
    con.commit()
    return imported, len(df)-imported


def export_query_xlsx(df_dict, output_path):
    output_path = Path(output_path)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        wrote = False
        for sheet_name, frame in df_dict.items():
            if frame is None:
                continue
            safe_name = str(sheet_name)[:31] or "Sheet"
            frame.to_excel(writer, sheet_name=safe_name, index=False)
            wrote = True
        if not wrote:
            pd.DataFrame([{"message": "Tidak ada data untuk diekspor."}]).to_excel(
                writer, sheet_name="Result", index=False
            )
    return output_path
