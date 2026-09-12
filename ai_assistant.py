import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from workflow_v226 import action_completion_ready

NEAR_DAYS = 90

def analysis_today() -> pd.Timestamp:
    """Current analysis date from the server clock; avoids stale hard-coded UAT dates."""
    return pd.Timestamp(datetime.now().date())


def normalize_question(q: str) -> str:
    q = (q or "").strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q


def normalize_entity_text(value: str) -> str:
    """Normalize names/employee codes to comparable alphanumeric tokens."""
    return re.sub(r"[^a-z0-9]+", " ", normalize_question(value)).strip()


EMPLOYEE_QUERY_STOPWORDS = {
    "ada", "akan", "apa", "apakah", "assessment", "bagaimana", "berapa", "belum",
    "berakhir", "berdasarkan", "butuh", "certification", "closed", "dalam", "dan",
    "dari", "data", "dibutuhkan", "diikuti", "employee", "expired", "history", "ikut",
    "kadaluwarsa", "kapan", "kedaluwarsa", "kompeten", "kompetensi", "kondisi", "mana",
    "menjadi", "milik", "near", "nilai", "open", "pelatihan", "pemenuhan", "pernah",
    "persen", "record", "requirement", "riwayat", "saja", "seluruh", "sertifikasi",
    "sertifikat", "siapa", "skor", "status", "sudah", "tahun", "tampilkan", "tersedia",
    "tidak", "training", "ubah", "untuk", "wajib", "yang", "action", "karyawan",
    "tolong", "coba", "carikan", "semua", "daftar", "punya", "memiliki", "terpenuhi",
    "fulfillment", "hampir", "tanggal", "expiry", "date", "berapa", "lama", "nya",
    "potential", "gap", "validation", "required", "not", "assessed", "covered",
    "supervisor", "manager", "operator", "jabatan", "position", "department", "departemen",
    "masalah", "rekomendasi", "tindak", "lanjut", "expired", "aktif", "valid", "hari",
    "mengikuti", "kedaluwarsanya", "kadaluarsanya", "expirednya", "berakhirnya",
    "sertifikasinya", "sertifikatnya", "trainingnya", "pelatihannya", "kompetensinya",
    "statusnya", "actionnya", "requirementnya", "sekarang", "berikutnya",
    "instruktur", "provider", "penyedia", "prediksi", "lulus", "terbaik", "ujian",
}


def _contains_token_phrase(question: str, value: str) -> bool:
    q_tokens = normalize_entity_text(question).split()
    v_tokens = normalize_entity_text(value).split()
    if not v_tokens or len(v_tokens) > len(q_tokens):
        return False
    width = len(v_tokens)
    return any(q_tokens[i:i + width] == v_tokens for i in range(len(q_tokens) - width + 1))


def _explicit_employee_codes(question: str) -> list[str]:
    """Extract employee-code-like references while excluding four-digit years."""
    found = re.findall(r"\b(?:bb\s*[- ]?\s*)?\d{5,}\b", normalize_question(question), flags=re.I)
    return [re.sub(r"[^a-z0-9]", "", value.lower()) for value in found]


def _catalog_matches(con: sqlite3.Connection, question: str) -> list[dict[str, Any]]:
    """Return exact training-catalog phrases, longest first.

    A short token such as ``K3`` is intentionally not promoted to a specific
    training because several catalog items can share it.
    """
    try:
        rows = con.execute(
            "SELECT training_id,training_name FROM training_catalog "
            "WHERE COALESCE(active_flag,1)=1 AND training_name IS NOT NULL"
        ).fetchall()
    except Exception:
        return []
    matches = [
        {"training_id": int(r[0]), "training_name": str(r[1])}
        for r in rows if _contains_token_phrase(question, str(r[1]))
    ]
    matches.sort(key=lambda x: len(normalize_entity_text(x["training_name"]).split()), reverse=True)
    if not matches:
        return []
    longest = len(normalize_entity_text(matches[0]["training_name"]).split())
    return [m for m in matches if len(normalize_entity_text(m["training_name"]).split()) == longest]


def _exact_employee_mentions(records: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """Resolve explicit full names/codes without accepting overlapping shorter names.

    For example, ``Al Akbar`` must resolve only to that employee rather than also
    matching the separate employee named ``Akbar``. Distinct employees explicitly
    mentioned in the same question remain supported because non-overlapping spans
    are retained.
    """
    question_tokens = normalize_entity_text(question).split()
    if not question_tokens:
        return []

    candidates: list[tuple[int, int, int, dict[str, Any]]] = []
    for record in records:
        for value in (record.get("employee_name"), record.get("employee_code")):
            entity_tokens = normalize_entity_text(str(value or "")).split()
            if not entity_tokens:
                continue
            width = len(entity_tokens)
            for start in range(0, len(question_tokens) - width + 1):
                if question_tokens[start:start + width] == entity_tokens:
                    candidates.append((start, start + width, width, record))

    # Most specific mention wins at an overlapping location. A second, separate
    # occurrence is still selected, so questions may name more than one employee.
    selected: list[tuple[int, int, dict[str, Any]]] = []
    occupied: set[int] = set()
    for start, end, width, record in sorted(candidates, key=lambda x: (-x[2], x[0], int(x[3]["employee_pk"]))):
        span = set(range(start, end))
        if span & occupied:
            continue
        selected.append((start, end, record))
        occupied.update(span)

    resolved = []
    seen = set()
    for _, _, record in sorted(selected, key=lambda x: x[0]):
        employee_pk = int(record["employee_pk"])
        if employee_pk not in seen:
            resolved.append(record)
            seen.add(employee_pk)
    return resolved


def detect_intents(q: str):
    q = normalize_question(q)
    intents = set()

    cert_words = [
        "sertifikasi", "sertifikat", "certification", "certificate", "expired", "kadaluarsa", "kedaluwarsa",
        "renewal", "perpanjang", "masa berlaku", "akan expired",
        "akan kedaluwarsa", "akan kadaluarsa", "akan berakhir", "segera berakhir",
        "near expiry"
    ]
    training_words = [
        "training", "pelatihan", "tna", "training requirement", "requirement",
        "kebutuhan training", "masalah pada training", "masalah training", "pelatihan wajib", "pelatihan mandatory",
        "mengikuti", "diikuti", "riwayat"
    ]
    competency_words = [
        "competency", "kompetensi", "masalah pada competency", "masalah competency", "masalah pada kompetensi", "masalah kompetensi", "gap", "potential gap", "coverage",
        "covered", "validation required", "validation", "not assessed",
        "belum assessed", "belum dinilai", "pemenuhan", "assessment", "kompeten",
        "skor assessment", "nilai assessment", "nilai ujian", "skor ujian"
    ]
    action_words = ["action", "action center"]

    if any(w in q for w in cert_words):
        intents.add("certification")
    if any(w in q for w in training_words):
        intents.add("training")
    if any(w in q for w in competency_words):
        intents.add("competency")
    if any(w in q for w in action_words):
        intents.add("action")

    return intents


def detect_intent_details(q: str):
    ql = normalize_question(q)
    details = set()

    # Ordering matters: future-expiry markers must beat plain "expired".
    if any(x in ql for x in [
        "kapan kedaluwarsa", "kapan kadaluarsa", "kapan expired", "kapan berakhir",
        "tanggal kedaluwarsa", "tanggal kadaluarsa", "tanggal expired", "expiry date"
    ]):
        details.add("expiry_date_lookup")
    if any(x in ql for x in [
        "akan expired", "akan kedaluwarsa", "akan kadaluarsa",
        "akan berakhir", "segera berakhir", "near expiry",
        "dalam 90 hari", "90 hari ke depan", "segera expired"
    ]):
        details.add("near_expiry")
    if any(x in ql for x in [
        "sudah expired", "telah expired", "sudah kedaluwarsa",
        "telah kedaluwarsa", "sudah kadaluarsa", "telah kadaluarsa"
    ]):
        details.add("expired")
    elif "expired" in ql or "kedaluwarsa" in ql or "kadaluarsa" in ql:
        # Plain expiry wording is interpreted as already expired unless future wording exists.
        if "near_expiry" not in details and "expiry_date_lookup" not in details:
            details.add("expired")

    if "potential gap" in ql or re.search(r"\bgap\b", ql):
        details.add("potential_gap")
    if "validation required" in ql or re.search(r"\bvalidation\b", ql):
        details.add("validation_required")
    if "not assessed" in ql or "belum assessed" in ql or "belum dinilai" in ql:
        details.add("not_assessed")
    if "covered" in ql or "sudah terpenuhi" in ql or "terpenuhi" in ql:
        details.add("covered")
    if any(x in ql for x in [
        "pemenuhan training", "pemenuhan pelatihan", "training fulfillment", "fulfillment training",
        "persen pemenuhan", "persentase pemenuhan", "berapa persen", "belum terpenuhi",
        "sudah terpenuhi", "mana yang belum", "mana yang sudah terpenuhi",
        "sudah memenuhi training", "sudah memenuhi pelatihan", "sudah ikut training",
        "sudah mengikuti seluruh training", "memenuhi seluruh training requirement"
    ]):
        details.add("training_fulfillment")
    if any(x in ql for x in ["belum terpenuhi", "mana yang belum", "yang belum terpenuhi"]):
        details.add("unfulfilled_only")
    if any(x in ql for x in ["mana yang sudah terpenuhi", "tampilkan yang sudah terpenuhi", "yang sudah terpenuhi saja"]):
        details.add("fulfilled_only")
    if any(x in ql for x in [
        "training history", "riwayat training", "riwayat pelatihan", "pernah diikuti",
        "pernah mengikuti", "yang sudah diikuti", "sudah diikuti", "sudah mengikuti"
    ]):
        details.add("training_history")
    if any(x in ql for x in [
        "training requirement", "requirement", "kebutuhan training", "training yang dibutuhkan",
        "training apa yang dibutuhkan", "training wajib", "pelatihan wajib", "diwajibkan"
    ]):
        details.add("training_requirement")
    if re.search(r"\b(?:19|20)\d{2}\b", ql) and "training" in detect_intents(ql):
        details.add("training_history")
    if any(x in ql for x in ["skor assessment", "nilai assessment", "skor ujian", "nilai ujian", "berapa skor", "berapa nilai"]):
        details.add("assessment_score")
    if "instruktur" in ql:
        details.add("unsupported_instructor")
    if "prediksi" in ql or "perkiraan kapan" in ql:
        details.add("prediction_request")
    if "employee terbaik" in ql or "karyawan terbaik" in ql:
        details.add("unsupported_ranking")
    if "nomor sertifikat" in ql or "nomor certification" in ql:
        details.add("certificate_number_lookup")
    if "action" in detect_intents(ql) and any(
        re.search(rf"\b{verb}\b", ql) for verb in ["ubah", "tutup", "hapus", "delete", "buat", "create", "pulihkan", "restore", "upload", "anggap"]
    ):
        details.add("mutation_request")
    if any(x in ql for x in ["password", "credential", "kredensial", "struktur database", "database schema"]):
        details.add("sensitive_request")
    if any(x in ql for x in ["hapus seluruh evidence", "hapus semua evidence", "delete all evidence"]):
        details.add("unsafe_mutation")
    if any(x in ql for x in [
        "rekomendasi", "tindak lanjut", "apa yang harus dilakukan", "harus dilakukan", "action"
    ]):
        details.add("recommendation")

    if any(x in ql for x in ["status kompetensinya belum dapat dipastikan","status kompetensi belum dapat dipastikan","kompetensinya belum dapat dipastikan","belum dapat dipastikan","belum pasti","perlu divalidasi"]) and "competency" in detect_intents(ql):
        details.add("competency_uncertain")
    if any(x in ql for x in ["sertifikasinya bermasalah","sertifikasinya juga bermasalah","sertifikatnya bermasalah","sertifikasi bermasalah","sertifikat bermasalah","masalah sertifikasi","masalah sertifikat"]) and "certification" in detect_intents(ql):
        details.add("certification_issue")
    if "gap" in ql and "training requirement" in ql:
        details.add("gap_training_link")

    # Explicit multi-domain issue wording. These markers distinguish a true three-domain
    # decision request from a simple keyword lookup.
    if any(x in ql for x in [
        "masalah pada training", "masalah training", "training bermasalah",
        "masalah pada pelatihan", "masalah pelatihan"
    ]):
        details.add("training_issue")
    if any(x in ql for x in [
        "masalah pada competency", "masalah competency", "masalah pada kompetensi",
        "masalah kompetensi", "kompetensinya bermasalah", "status kompetensi bermasalah"
    ]):
        details.add("competency_issue")
    if any(x in ql for x in [
        "masalah pada certification", "masalah certification", "masalah pada sertifikasi",
        "masalah sertifikasi", "sertifikasinya bermasalah", "sertifikasi bermasalah"
    ]):
        details.add("certification_issue")

    if all(x in detect_intents(ql) for x in ["training", "competency", "certification"]) and "sekaligus" in ql:
        details.add("three_domain_issue")

    return details


def extract_search_terms(q: str):
    stop = {
        "siapa", "yang", "sudah", "akan", "dalam", "hari", "sertifikasi",
        "sertifikat", "training", "pelatihan", "apa", "saja", "dan", "punya",
        "memiliki", "tampilkan", "tolong", "coba", "carikan", "ada", "untuk",
        "dengan", "pada", "di", "ke", "dari", "yang", "belum", "masuk", "mohon",
        "berdasarkan", "karyawan", "karyawan", "mau", "ingin", "sebutkan", "siapa saja"
    }
    terms = []
    for token in re.findall(r"[a-z0-9]+", normalize_question(q)):
        if len(token) >= 3 and token not in stop and not token.isdigit():
            terms.append(token)
    return list(dict.fromkeys(terms))


def extract_entities(con, q: str):
    ql = normalize_question(q)
    entities = {
        "employees": [], "employee_pks": [], "employee_resolution": "none",
        "employee_candidates": [], "employee_reference": "", "positions": [],
        "training_ids": [], "training_names": [], "training_resolution": "none",
        "certification_ids": [],
    }

    try:
        emp = pd.read_sql_query(
            """SELECT employee_pk,employee_code,employee_name
               FROM employees
               WHERE active_flag = 1 AND employee_name IS NOT NULL
               ORDER BY employee_name,employee_pk""",
            con,
        ).fillna("")
        records = emp.to_dict(orient="records")
        compact_codes = {
            re.sub(r"[^a-z0-9]", "", str(r.get("employee_code") or "").lower()): r
            for r in records if str(r.get("employee_code") or "").strip()
        }

        # Employee IDs are authoritative. An explicit but unknown ID must stop
        # resolution; it must never fall through to a name token such as "employee".
        code_mentions = _explicit_employee_codes(ql)
        if code_mentions:
            matched = [compact_codes[c] for c in code_mentions if c in compact_codes]
            if len(matched) == len(code_mentions):
                exact = list({int(r["employee_pk"]): r for r in matched}.values())
                entities["employees"] = [str(r["employee_name"]).strip() for r in exact]
                entities["employee_pks"] = [int(r["employee_pk"]) for r in exact]
                entities["employee_resolution"] = "exact_id"
            else:
                entities["employee_resolution"] = "not_found"
                entities["employee_reference"] = next(c for c in code_mentions if c not in compact_codes)
        else:
            # Exact full-name mentions remain the preferred name route. This keeps
            # Al Akbar and Akbar as two distinct entities.
            exact = _exact_employee_mentions(records, ql)
            if exact:
                entities["employees"] = [str(r["employee_name"]).strip() for r in exact]
                entities["employee_pks"] = [int(r["employee_pk"]) for r in exact]
                entities["employee_resolution"] = "exact_name"
            else:
                # Remove known domain/filter phrases and exact catalog phrases before
                # considering a shortened employee name.
                residual = [
                    t for t in normalize_entity_text(ql).split()
                    if t not in EMPLOYEE_QUERY_STOPWORDS and not t.isdigit()
                ]
                for m in _catalog_matches(con, ql):
                    for token in normalize_entity_text(m["training_name"]).split():
                        while token in residual:
                            residual.remove(token)
                try:
                    known_phrases = [r[0] for r in con.execute(
                        "SELECT position_name FROM positions WHERE active_flag=1 UNION "
                        "SELECT certification_name FROM certification_catalog WHERE COALESCE(active_flag,1)=1"
                    ).fetchall() if r[0]]
                except Exception:
                    known_phrases = []
                for phrase in known_phrases:
                    if _contains_token_phrase(ql, str(phrase)):
                        for token in normalize_entity_text(str(phrase)).split():
                            while token in residual:
                                residual.remove(token)

                candidates = []
                residual_set = set(residual)
                for r in records:
                    name_tokens = set(normalize_entity_text(str(r["employee_name"])).split())
                    if residual and residual_set.issubset(name_tokens):
                        candidates.append(r)

                if len(candidates) == 1:
                    r = candidates[0]
                    entities["employees"] = [str(r["employee_name"]).strip()]
                    entities["employee_pks"] = [int(r["employee_pk"])]
                    entities["employee_resolution"] = "unique_partial"
                elif len(candidates) > 1:
                    entities["employee_resolution"] = "ambiguous"
                    entities["employee_candidates"] = [
                        {"employee_pk": int(r["employee_pk"]), "employee_code": str(r["employee_code"]), "employee_name": str(r["employee_name"])}
                        for r in candidates[:8]
                    ]
                    entities["employee_reference"] = " ".join(residual)
                elif residual:
                    # Residual person-like text was supplied but no full token set
                    # matches an employee. Do not silently choose a fuzzy neighbour.
                    entities["employee_resolution"] = "not_found"
                    entities["employee_reference"] = " ".join(residual)
    except Exception:
        pass

    try:
        pos = pd.read_sql_query(
            "SELECT position_name FROM positions WHERE active_flag = 1 AND position_name IS NOT NULL",
            con,
        )
        pos_names = sorted(pos["position_name"].astype(str).unique(), key=len, reverse=True)
        entities["positions"] = [p for p in pos_names if normalize_question(p) in ql]
    except Exception:
        pass

    aliases = {
        "supervisor": ["supervisor"],
        "supervisor lapangan": ["supervisor lapangan"],
        "shift spv": ["shift spv", "shift supervisor"],
        "operation shift spv": ["operation shift spv", "operation shift supervisor"],
        "manager": ["manager"],
        "operator": ["operator"],
    }
    for alias, pats in aliases.items():
        if any(p in ql for p in pats) and alias not in entities["positions"]:
            entities["positions"].append(alias)

    training_matches = _catalog_matches(con, ql)
    if training_matches:
        entities["training_ids"] = [int(x["training_id"]) for x in training_matches]
        entities["training_names"] = [str(x["training_name"]) for x in training_matches]
        entities["training_resolution"] = "exact" if len(training_matches) == 1 else "ambiguous"

    # Certification names are also retained as exact record identifiers so a
    # follow-up such as "kapan kedaluwarsanya?" stays on the selected certificate.
    try:
        cert_rows = con.execute(
            "SELECT certification_id,certification_name_raw FROM certifications "
            "WHERE COALESCE(record_status,'Active')<>'Archived' "
            "AND certification_name_raw IS NOT NULL"
        ).fetchall()
        matches = [
            (int(r[0]), str(r[1])) for r in cert_rows
            if _contains_token_phrase(ql, str(r[1]))
        ]
        if matches:
            longest = max(len(normalize_entity_text(x[1]).split()) for x in matches)
            entities["certification_ids"] = list(dict.fromkeys(
                x[0] for x in matches
                if len(normalize_entity_text(x[1]).split()) == longest
            ))
    except Exception:
        pass

    return entities


def apply_person_position_filter(df, entities):
    if df.empty:
        return df
    out = df.copy()

    if entities.get("employees") and "employee_name" in out.columns:
        names = {normalize_question(x) for x in entities["employees"]}
        mask = out["employee_name"].fillna("").astype(str).map(normalize_question).isin(names)
        out = out[mask]
        # An explicitly requested employee that is not present must return no evidence;
        # never fall back to unrelated employees.
        if out.empty:
            return out

    if entities.get("positions"):
        pos_cols = [c for c in ["position_name", "position", "position_tna", "position_cert_db"] if c in out.columns]
        if pos_cols:
            pats = [normalize_question(x) for x in entities["positions"]]
            mask = pd.Series(False, index=out.index)
            for col in pos_cols:
                mask |= out[col].fillna("").astype(str).map(normalize_question).apply(
                    lambda s: bool(s) and any(p == s or p in s or s in p for p in pats)
                )
            out = out[mask]
            if out.empty:
                return out
    return out


def query_certification(con, q: str, entities=None):
    ql = normalize_question(q)
    intent_details = detect_intent_details(ql)
    today_ts = analysis_today()
    future_markers = [
        "akan expired", "akan kedaluwarsa", "akan kadaluarsa", "akan berakhir",
        "segera berakhir", "near expiry", "dalam 90 hari", "90 hari ke depan",
        "segera expired"
    ]
    expired_markers = [
        "sudah expired", "telah expired", "sudah kedaluwarsa",
        "telah kedaluwarsa", "sudah kadaluarsa", "telah kadaluarsa"
    ]

    base_sql = """
        SELECT c.certification_id, c.employee_pk,
               e.employee_name AS employee_name,
               p.position_name AS position_name,
               c.certification_name_raw AS certification_name,
               COALESCE(c.issue_date,c.certification_date) AS certification_date,
               c.certificate_number,
               COALESCE(c.expiry_date,c.expired_date) AS expired_date,
               COALESCE(c.certification_status,c.status) AS status,
               COALESCE(c.renewal_status,'') AS renewal_status,
               COALESCE(c.record_status,'Active') AS record_status
        FROM certifications c
        JOIN employees e ON e.employee_pk = c.employee_pk
        LEFT JOIN positions p ON p.position_id = e.current_position_id
        WHERE COALESCE(c.record_status,'Active') <> 'Archived'
    """

    params = []
    if "expiry_date_lookup" in intent_details:
        sql = base_sql + " ORDER BY e.employee_name,c.certification_name_raw"
        mode = "Expiry Date Lookup"
    elif any(m in ql for m in future_markers):
        sql = base_sql + """
              AND COALESCE(c.expiry_date,c.expired_date) IS NOT NULL
              AND date(COALESCE(c.expiry_date,c.expired_date)) >= date(?)
              AND date(COALESCE(c.expiry_date,c.expired_date)) <= date(?, '+90 day')
            ORDER BY date(c.expired_date), e.employee_name
        """
        params = [today_ts.strftime("%Y-%m-%d"), today_ts.strftime("%Y-%m-%d")]
        mode = "Near Expiry ≤90 Hari"
    elif any(m in ql for m in expired_markers):
        sql = base_sql + """
              AND COALESCE(c.expiry_date,c.expired_date) IS NOT NULL
              AND date(COALESCE(c.expiry_date,c.expired_date)) < date(?)
            ORDER BY date(c.expired_date), e.employee_name
        """
        params = [today_ts.strftime("%Y-%m-%d")]
        mode = "Expired"
    elif "expired" in ql or "kadaluarsa" in ql or "kedaluwarsa" in ql:
        sql = base_sql + """
              AND COALESCE(c.expiry_date,c.expired_date) IS NOT NULL
              AND date(COALESCE(c.expiry_date,c.expired_date)) < date(?)
            ORDER BY date(c.expired_date), e.employee_name
        """
        params = [today_ts.strftime("%Y-%m-%d")]
        mode = "Expired"
    else:
        sql = base_sql + " ORDER BY e.employee_name, c.certification_name_raw"
        mode = "Certification Search"

    df = pd.read_sql_query(sql, con, params=params)
    if not df.empty:
        dt = pd.to_datetime(df["expired_date"], errors="coerce")
        def _cert_display(row):
            src = str(row.get("status", "") or "")
            if src in {"Superseded", "Inactive", "Not Renewed"}:
                return src
            x = pd.to_datetime(row.get("expired_date"), errors="coerce")
            if pd.notna(x) and x < today_ts:
                return "Expired"
            if pd.notna(x) and today_ts <= x <= today_ts + pd.Timedelta(days=NEAR_DAYS):
                return "Near Expiry"
            if pd.notna(x) and x > today_ts:
                return "Active"
            if src.lower() in {"dalam proses", "in process"}:
                return "In Process"
            return src or "Not Available"
        df["display_status"] = df.apply(_cert_display, axis=1)
        def _renewal_display(row):
            current = str(row.get("renewal_status", "") or "").strip()
            terminal = {"Completed", "Not Renewed", "No Action Required", "Renewal Planned", "Renewal Pending"}
            if current in terminal:
                return current
            if str(row.get("display_status", "")) in {"Near Expiry", "Expired"} and current in {"", "Not Due"}:
                return "Renewal Review Required"
            return current or "Not Due"
        df["renewal_status"] = df.apply(_renewal_display, axis=1)

    if entities:
        df = apply_person_position_filter(df, entities)
        cert_ids = [int(x) for x in entities.get("certification_ids", [])]
        if cert_ids and "certification_id" in df.columns:
            df = df[pd.to_numeric(df["certification_id"], errors="coerce").isin(cert_ids)]

    # Do not apply generic keyword search to semantic condition queries such as
    # "masalah pada certification"; those must return the full issue population
    # so the Orchestrator can intersect it with other agents.
    terms = extract_search_terms(q)
    semantic_issue_query = (
        any(x in ql for x in [
            "masalah pada certification", "masalah certification", "masalah pada sertifikasi",
            "masalah sertifikasi", "sertifikasinya bermasalah", "sertifikasi bermasalah"
        ])
        or ("masalah pada" in ql and "certification" in ql and "sekaligus" in ql)
        or ("masalah pada" in ql and "sertifikasi" in ql and "sekaligus" in ql)
    )
    if terms and mode == "Certification Search" and not semantic_issue_query:
        mask = pd.Series(False, index=df.index)
        for col in ["employee_name", "position_name", "certification_name"]:
            mask |= df[col].fillna("").astype(str).str.lower().apply(lambda s: bool(s) and any(t in s for t in terms))
        filtered = df[mask]
        if not filtered.empty:
            df = filtered

    return df, mode


def _filter_exact_training_phrase(df: pd.DataFrame, q: str) -> pd.DataFrame:
    """If a known training/requirement name is explicitly mentioned, narrow to it."""
    if df.empty or "training_name" not in df.columns:
        return df
    qn = normalize_question(q)
    names = [str(x).strip() for x in df["training_name"].dropna().unique() if str(x).strip()]
    matches = [name for name in sorted(names, key=len, reverse=True) if normalize_question(name) in qn]
    if not matches:
        return df
    wanted = {normalize_question(x) for x in matches}
    return df[df["training_name"].astype(str).map(normalize_question).isin(wanted)].copy()


def _filter_training_entities(df: pd.DataFrame, entities=None) -> pd.DataFrame:
    if df.empty or "training_name" not in df.columns or not entities:
        return df
    wanted = {normalize_question(x) for x in entities.get("training_names", []) if str(x).strip()}
    if not wanted:
        return df
    return df[df["training_name"].astype(str).map(normalize_question).isin(wanted)].copy()


def query_training_employee_level(con, q: str, entities=None):
    """Resolve employees to their current positions and return position-based training requirements.

    This answers employee-level questions such as ``siapa yang butuh training`` using
    active Employee Master records joined to the active position-based TNA. It does
    not infer that the employee has not completed the training; completion requires
    Training History. Because the current TNA records do not contain effective/frequency
    year values, the result represents active position-based requirements rather than
    a confirmed calendar-year schedule.
    """
    sql = """
        SELECT e.employee_pk, e.employee_name,
               p.position_name, COALESCE(p.department,'') AS department,
               tr.requirement_id, tc.training_name, tr.requirement_type,
               tr.delivery_type, tr.regulatory_flag, tr.effective_year,
               tr.frequency_type, tr.frequency_value, tr.frequency_unit,
               tr.position_crosswalk_status, tr.crosswalk_status,
               tr.validation_status
        FROM employees e
        JOIN positions p ON p.position_id = e.current_position_id
        JOIN training_requirements tr
          ON tr.position_standard_id = e.current_position_id
         AND COALESCE(tr.active_flag,1) = 1
        JOIN training_catalog tc ON tc.training_id = tr.training_id
        WHERE e.active_flag = 1
        ORDER BY e.employee_name, tc.training_name
    """
    df = pd.read_sql_query(sql, con).fillna("")
    if df.empty:
        return df, "Employee Training Requirement"

    # Explicit employee/position entities narrow the employee-level result.
    if entities and entities.get('employees'):
        wanted = {normalize_question(x) for x in entities['employees']}
        df = df[df['employee_name'].map(normalize_question).isin(wanted)]
    if entities and entities.get('positions'):
        pats = [normalize_question(x) for x in entities['positions']]
        df = df[df['position_name'].map(normalize_question).apply(
            lambda s: any(p == s or p in s or s in p for p in pats)
        )]

    return df, "Employee Training Requirement"


def query_training(con, q: str, entities=None):
    # Training requirements are position-based, but employee context is resolved
    # through the employee's current position so a question about a person can
    # return the applicable training requirements.
    df = pd.read_sql_query("""
        SELECT tr.requirement_id,
               tc.training_name,
               tr.position_raw,
               COALESCE(pstd.position_name, p.position_name, tr.position_raw) AS position_standard,
               tr.position_standard_id,
               tr.position_id,
               tr.requirement_code,
               tr.requirement_type,
               tr.delivery_type,
               tr.regulatory_flag,
               tr.crosswalk_status,
               tr.position_crosswalk_status,
               tr.mapping_category,
               tr.validation_status,
               COALESCE(pstd.position_name, p.position_name, tr.position_raw) AS position_name,
               COALESCE(pstd.department, p.department, '') AS department
        FROM training_requirements tr
        JOIN training_catalog tc ON tc.training_id = tr.training_id
        LEFT JOIN positions p ON p.position_id = tr.position_id
        LEFT JOIN positions pstd ON pstd.position_id = tr.position_standard_id
        ORDER BY position_name, tc.training_name
    """, con)

    if df.empty:
        return df, "Training Requirement"

    # Resolve employee -> current position, then narrow the requirement set.
    if entities and entities.get("employees"):
        emp_names = entities["employees"]
        placeholders = ",".join(["?"] * len(emp_names))
        emp_df = pd.read_sql_query(
            f"""SELECT e.employee_name, e.current_position_id,
                       p.position_name, p.department
                FROM employees e
                LEFT JOIN positions p ON p.position_id = e.current_position_id
                WHERE e.active_flag = 1 AND lower(e.employee_name) IN ({placeholders})""",
            con,
            params=[normalize_question(x) for x in emp_names],
        )
        if not emp_df.empty:
            pos_ids = set(pd.to_numeric(emp_df["current_position_id"], errors="coerce").dropna().astype(int).tolist())
            pos_names = set(emp_df["position_name"].fillna("").astype(str).str.casefold().tolist())
            mask = pd.Series(False, index=df.index)
            mask |= pd.to_numeric(df["position_standard_id"], errors="coerce").isin(pos_ids)
            mask |= pd.to_numeric(df["position_id"], errors="coerce").isin(pos_ids)
            mask |= df["position_name"].fillna("").astype(str).str.casefold().isin(pos_names)
            filtered = df[mask]
            if not filtered.empty:
                df = filtered.copy()
                df["employee_names"] = ", ".join(emp_names)
                df["employee_name"] = emp_names[0] if len(emp_names) == 1 else ", ".join(emp_names)
                df["employee_count"] = len(emp_names)
            else:
                # When no exact position mapping exists, try a deterministic fuzzy
                # position-name match against TNA source positions. This is used only
                # to surface likely training requirements; the response will flag that
                # the mapping should be validated rather than claiming a hard match.
                try:
                    from difflib import SequenceMatcher
                    employee_pos = " ".join(emp_df["position_name"].dropna().astype(str).tolist())
                    target = normalize_question(employee_pos)
                    candidates = []
                    for col in ["position_raw", "position_standard", "position_name"]:
                        for val in df[col].dropna().astype(str).unique():
                            nv = normalize_question(val)
                            if not nv:
                                continue
                            score = SequenceMatcher(None, target, nv).ratio()
                            # Token overlap helps cases like 'Supervisor Training & Competency Development'
                            # vs 'Training & Comp Dev Supervisor'.
                            a=set(re.findall(r"[a-z0-9]+", target)); b=set(re.findall(r"[a-z0-9]+", nv))
                            overlap=(len(a & b)/max(1,len(a|b)))
                            score=max(score, overlap*0.9)
                            candidates.append((score,val))
                    candidates=sorted(candidates, reverse=True)
                    if candidates and candidates[0][0] >= 0.55:
                        best=candidates[0][1]
                        f2=df[df["position_raw"].fillna("").astype(str).str.casefold().eq(str(best).casefold()) |
                              df["position_standard"].fillna("").astype(str).str.casefold().eq(str(best).casefold()) |
                              df["position_name"].fillna("").astype(str).str.casefold().eq(str(best).casefold())]
                        if not f2.empty:
                            df=f2.copy()
                            df["employee_names"] = ", ".join(emp_names) + " (position mapping needs validation)"
                            df["employee_name"] = emp_names[0] if len(emp_names) == 1 else ", ".join(emp_names)
                            df["employee_count"] = len(emp_names)
                except Exception:
                    pass
            # If even the fuzzy aid cannot find a requirement, keep an explicit data gap marker.
            if df.empty:
                df = pd.DataFrame([{
                    "training_name":"Belum ada Training Requirement terpetakan",
                    "position_raw": employee_pos if 'employee_pos' in locals() else "",
                    "position_standard": "",
                    "position_standard_id":"",
                    "position_id":"",
                    "requirement_code":"",
                    "requirement_type":"Data Not Available",
                    "delivery_type":"",
                    "regulatory_flag":"",
                    "crosswalk_status":"Mapping Required",
                    "position_crosswalk_status":"Mapping Required",
                    "mapping_category":"",
                    "validation_status":"Validation Required",
                    "position_name": employee_pos if 'employee_pos' in locals() else "",
                    "department": emp_df["department"].fillna("").iloc[0] if not emp_df.empty else "",
                    "employee_names": ", ".join(emp_names),
                    "employee_name": emp_names[0] if len(emp_names) == 1 else ", ".join(emp_names),
                    "employee_count": len(emp_names),
                }])

    if entities and entities.get("positions"):
        pats = [normalize_question(x) for x in entities["positions"]]
        pos_cols = [c for c in ["position_raw", "position_standard", "position_name"] if c in df.columns]
        mask = pd.Series(False, index=df.index)
        for col in pos_cols:
            mask |= df[col].fillna("").astype(str).map(normalize_question).apply(
                lambda s: bool(s) and any(p == s or p in s or s in p for p in pats)
            )
        filtered = df[mask]
        if not filtered.empty:
            df = filtered

    # Search training/position terms when no explicit person/position entity is found.
    if not (entities and (entities.get("positions") or entities.get("employees"))):
        terms = extract_search_terms(q)
        if terms:
            mask = pd.Series(False, index=df.index)
            for col in ["training_name", "position_raw", "position_standard", "position_name", "department", "requirement_type", "requirement_code"]:
                mask |= df[col].fillna("").astype(str).str.lower().apply(lambda s: bool(s) and any(t in s for t in terms))
            filtered = df[mask]
            if not filtered.empty:
                df = filtered

    df = _filter_exact_training_phrase(df, q)
    df = _filter_training_entities(df, entities)
    return df, "Training Requirement"


def query_competency(con, q: str, entities=None):
    ql = normalize_question(q)
    details = detect_intent_details(ql)

    source_sql = """
        SELECT employee_name, position_tna, position_cert_db, training_name,
               requirement_type, requirement_code, mapping_category,
               certification_candidate, coverage_status, expiry_status,
               priority, recommendation
        FROM v_coverage_gap_results
    """
    df = pd.read_sql_query(source_sql, con)
    mode = "Competency Coverage"

    if "potential_gap" in details:
        df = df[df["coverage_status"].astype(str).str.casefold() == "potential gap"]
        mode = "Potential Gap"
    elif "validation_required" in details:
        df = df[df["coverage_status"].astype(str).str.casefold() == "validation required"]
        mode = "Validation Required"
    elif "covered" in details:
        df = df[df["coverage_status"].astype(str).str.casefold() == "covered"]
        mode = "Covered"
    elif "not_assessed" in details:
        df = df[df["coverage_status"].astype(str).str.casefold() == "not assessed"]
        mode = "Not Assessed"

    if entities:
        df = apply_person_position_filter(df, entities)

    df = _filter_exact_training_phrase(df, q)
    df = _filter_training_entities(df, entities)
    return df, mode


def query_competency_pending_context(con, q: str, entities=None):
    """Return requirement context when coverage does not exist because assessment has not run yet.

    This is intentionally *not* an assessment result. It only confirms that an active
    requirement exists for the employee's current position and explains why coverage
    is not yet available. It prevents a misleading generic ``NO_MATCH`` response for
    pre-assessment employees.
    """
    employees = (entities or {}).get("employees") or []
    if not employees:
        return pd.DataFrame(), "Coverage Pending Assessment"

    placeholders = ",".join(["?"] * len(employees))
    emp = pd.read_sql_query(
        f"""
        SELECT e.employee_pk, e.employee_name, e.current_position_id,
               COALESCE(e.assessment_state,'Pending Initial Assessment') AS assessment_state,
               p.position_name
        FROM employees e
        LEFT JOIN positions p ON p.position_id=e.current_position_id
        WHERE e.active_flag=1
          AND lower(e.employee_name) IN ({placeholders})
        """,
        con,
        params=[normalize_question(x) for x in employees],
    ).fillna("")
    if emp.empty:
        return pd.DataFrame(), "Coverage Pending Assessment"

    frames = []
    for _, er in emp.iterrows():
        state = str(er.get("assessment_state", "") or "").strip()
        # Do not mask a real retrieval problem for employees that are already assessed.
        if state.casefold() == "assessed":
            continue
        req = pd.read_sql_query(
            """
            SELECT tc.training_name, tr.requirement_type, tr.requirement_code,
                   COALESCE(tr.mapping_category,'') AS mapping_category,
                   COALESCE(tr.cert_candidate,'') AS certification_candidate
            FROM training_requirements tr
            JOIN training_catalog tc ON tc.training_id=tr.training_id
            WHERE COALESCE(tr.active_flag,1)=1
              AND COALESCE(tr.requirement_status,'Active')='Active'
              AND COALESCE(tr.position_standard_id,tr.position_id)=?
            ORDER BY tc.training_name
            """,
            con, params=(int(er["current_position_id"]),)
        ).fillna("")
        if req.empty:
            continue
        # When a requirement is named explicitly, only return that requirement.
        req = _filter_exact_training_phrase(req, q)
        req = _filter_training_entities(req, entities)
        if req.empty:
            continue
        # For generic employee coverage questions, avoid dumping the entire TNA into
        # this semantic fallback. The normal assessment lifecycle page remains the source
        # for full pre-assessment requirement counts.
        if not entities.get("training_names") and not any(normalize_question(str(x)) in normalize_question(q) for x in req["training_name"].tolist()):
            continue

        if state == "Pending Initial Assessment":
            action = "Confirm employee data, then run Initial Assessment before interpreting requirement coverage."
        elif state == "Assessment Ready":
            action = "Run Initial Assessment before interpreting requirement coverage."
        elif state == "Pending Reassessment":
            action = "Run reassessment before interpreting updated requirement coverage."
        else:
            action = "Complete the pending assessment lifecycle before interpreting requirement coverage."

        req = req.copy()
        req["employee_name"] = str(er.get("employee_name", ""))
        req["position_tna"] = str(er.get("position_name", ""))
        req["position_cert_db"] = str(er.get("position_name", ""))
        req["coverage_status"] = "Coverage Pending Assessment"
        req["expiry_status"] = "—"
        req["priority"] = "—"
        req["recommendation"] = action
        req["assessment_state"] = state
        frames.append(req[[
            "employee_name","position_tna","position_cert_db","training_name",
            "requirement_type","requirement_code","mapping_category",
            "certification_candidate","coverage_status","expiry_status",
            "priority","recommendation","assessment_state"
        ]])

    if not frames:
        return pd.DataFrame(), "Coverage Pending Assessment"
    return pd.concat(frames, ignore_index=True), "Coverage Pending Assessment"



def enrich_evidence(agent: str, mode: str, df: pd.DataFrame) -> Dict[str, Any]:
    """Build a grounded evidence package for the response layer."""
    if df.empty:
        return {"record_count": 0, "summary": "Tidak ada record yang sesuai.", "evidence": []}

    evidence = df.copy()
    if agent == "Certification Agent":
        summary = f"Ditemukan {len(evidence)} record sertifikasi pada kondisi {mode}."
        if mode == "Near Expiry ≤90 Hari":
            summary += " Kondisi ini berarti tanggal berakhir berada antara tanggal analisis dan 90 hari ke depan."
        elif mode == "Expired":
            summary += " Kondisi ini berarti tanggal berakhir telah melewati tanggal analisis."
        cols = [c for c in ["employee_name","position_name","certification_name","certification_date","expired_date","status"] if c in evidence.columns]
    elif agent == "Training Agent":
        summary = f"Ditemukan {len(evidence)} training requirement yang sesuai dengan pertanyaan."
        cols = [c for c in ["position_raw","position_standard","training_name","requirement_type","delivery_type","regulatory_flag"] if c in evidence.columns]
    else:
        summary = f"Ditemukan {len(evidence)} record assessment/coverage pada kondisi {mode}."
        cols = [c for c in ["employee_name","position_tna","position_cert_db","training_name","mapping_category","certification_candidate","coverage_status","expiry_status","priority","recommendation"] if c in evidence.columns]
    return {"record_count": len(evidence), "summary": summary, "evidence": evidence[cols].copy() if cols else evidence.copy()}


def build_rationale(agent: str, mode: str, df: pd.DataFrame) -> str:
    """Explain the result using only deterministic database fields."""
    if df.empty:
        return "Tidak ada evidence yang cocok pada sumber data yang diperiksa."
    if agent == "Competency Agent":
        statuses = set(df.get("coverage_status", pd.Series(dtype=str)).dropna().astype(str).str.casefold())
        if mode == "Coverage Pending Assessment" or "coverage pending assessment" in statuses:
            states = list(dict.fromkeys(df.get("assessment_state", pd.Series(dtype=str)).dropna().astype(str)))
            state_text = ", ".join(states) if states else "assessment belum selesai"
            return f"Requirement teridentifikasi pada posisi aktif, tetapi coverage belum tersedia karena lifecycle assessment masih {state_text}."
        if mode == "Potential Gap" or "potential gap" in statuses:
            return "Potential Gap diperlakukan sebagai indikasi kebutuhan validasi; hasil ini tidak membuktikan training belum pernah dilakukan karena Training History aktual belum tersedia."
        if mode == "Validation Required" or "validation required" in statuses:
            return "Validation Required menunjukkan hubungan requirement-evidence atau kondisi data belum cukup pasti untuk keputusan final."
        if mode == "Not Assessed" or "not assessed" in statuses:
            return "Not Assessed menunjukkan data atau mapping belum cukup untuk menghasilkan assessment, bukan berarti karyawan tidak kompeten."
        if mode == "Covered" or "covered" in statuses:
            return "Covered menunjukkan terdapat bukti pemenuhan yang sesuai pada data coverage yang tersedia."
    if agent == "Certification Agent":
        if mode == "Near Expiry ≤90 Hari":
            return "Near Expiry dihitung dari expired_date yang berada mulai tanggal analisis sampai 90 hari ke depan."
        if mode == "Expired":
            return "Expired dihitung dari expired_date yang lebih kecil dari tanggal analisis."
    return "Interpretasi mengikuti field dan aturan pada sumber data yang digunakan."

def intersect_results(results):
    """Apply cross-agent entity intersection for multi-domain questions."""
    nonempty = {a: p["data"].copy() for a, p in results.items() if not p["data"].empty}
    if len(nonempty) < 2:
        return results, None

    employee_sets = []
    for agent, df in nonempty.items():
        if "employee_name" in df.columns:
            employee_sets.append(set(df["employee_name"].dropna().astype(str)))
    if len(employee_sets) >= 2:
        common = set.intersection(*employee_sets)
        if common:
            for agent, payload in results.items():
                df = payload["data"]
                if "employee_name" in df.columns:
                    payload["data"] = df[df["employee_name"].astype(str).isin(common)].copy()
            return results, {"dimension": "employee", "values": sorted(common)}

    return results, None


def query_training_fulfillment(con, q: str, entities=None):
    """Compare applicable position requirements with actual Training History evidence.

    A requirement is marked Fulfilled only when a matching actual Training History
    record has a completion/result status that evidences completion. Missing history
    is reported as No Training History Evidence, not inferred as failure.
    """
    employees = (entities or {}).get("employees") or []
    if not employees:
        return pd.DataFrame(columns=["employee_name","position_name","training_name","requirement_type","fulfillment_status","latest_completion_date","history_result","decision_action"]), "Training Fulfillment — Employee Required"

    placeholders = ",".join(["?"] * len(employees))
    emp = pd.read_sql_query(
        f"""SELECT e.employee_pk,e.employee_name,e.current_position_id,p.position_name
            FROM employees e LEFT JOIN positions p ON p.position_id=e.current_position_id
            WHERE e.active_flag=1 AND lower(e.employee_name) IN ({placeholders})""",
        con, params=[normalize_question(x) for x in employees]
    ).fillna("")
    if emp.empty:
        return pd.DataFrame(), "Training Fulfillment — Employee Not Found"

    frames=[]
    completed_values={"completed","passed","lulus","selesai","complete"}
    for _, er in emp.iterrows():
        req = pd.read_sql_query("""
            SELECT tr.requirement_id,tc.training_id,tc.training_name,tr.requirement_type
            FROM training_requirements tr
            JOIN training_catalog tc ON tc.training_id=tr.training_id
            WHERE COALESCE(tr.active_flag,1)=1
              AND COALESCE(tr.requirement_status,'Active')='Active'
              AND COALESCE(tr.position_standard_id,tr.position_id)=?
            ORDER BY tc.training_name
        """, con, params=(int(er["current_position_id"]),)).fillna("")
        hist = pd.read_sql_query("""
            SELECT COALESCE(th.training_id,th.training_pk) training_id,
                   COALESCE(th.training_name,th.training_name_raw,tc.training_name) training_name,
                   COALESCE(th.result_status,th.completion_status,'') history_result,
                   COALESCE(th.completion_date,th.training_date,'') latest_completion_date
            FROM training_history th
            LEFT JOIN training_catalog tc ON tc.training_id=COALESCE(th.training_id,th.training_pk)
            WHERE th.employee_pk=? AND COALESCE(th.record_status,'Active')<>'Archived'
            ORDER BY COALESCE(th.completion_date,th.training_date,'') DESC
        """, con, params=(int(er["employee_pk"]),)).fillna("")
        rows=[]
        for _, rr in req.iterrows():
            matches=hist[(pd.to_numeric(hist["training_id"],errors="coerce")==int(rr["training_id"])) | (hist["training_name"].astype(str).map(normalize_question)==normalize_question(rr["training_name"]))] if not hist.empty else hist
            fulfilled=False; latest=""; result=""
            if not matches.empty:
                for _, hr in matches.iterrows():
                    if normalize_question(hr["history_result"]) in completed_values:
                        fulfilled=True; latest=str(hr["latest_completion_date"] or ""); result=str(hr["history_result"] or ""); break
                if not result:
                    latest=str(matches.iloc[0]["latest_completion_date"] or ""); result=str(matches.iloc[0]["history_result"] or "")
            status="Fulfilled" if fulfilled else "No Training History Evidence"
            action="No follow-up from fulfillment evidence" if fulfilled else "Review or complete actual Training History evidence"
            rows.append({"employee_pk":int(er["employee_pk"]),"employee_name":er["employee_name"],"position_name":er["position_name"],"requirement_id":int(rr["requirement_id"]),"training_name":rr["training_name"],"requirement_type":rr["requirement_type"],"fulfillment_status":status,"latest_completion_date":latest,"history_result":result,"decision_action":action})
        frames.append(pd.DataFrame(rows))
    out=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
    out=_filter_exact_training_phrase(out,q)
    out=_filter_training_entities(out, entities)
    details = detect_intent_details(q)
    if "unfulfilled_only" in details and not out.empty:
        out = out[out["fulfillment_status"] != "Fulfilled"].copy()
    elif "fulfilled_only" in details and not out.empty:
        out = out[out["fulfillment_status"] == "Fulfilled"].copy()
    return out, "Training Fulfillment"


def query_training_history(con, q: str, entities=None):
    """Return only actual Training History records and enforce every filter.

    An empty result deliberately remains empty. It must never fall back to the
    position-based Training Requirement table.
    """
    sql = """
        SELECT th.history_id,th.employee_pk,e.employee_code,e.employee_name,
               p.position_name,
               COALESCE(th.training_id,th.training_pk) AS training_id,
               COALESCE(tc.training_name,th.training_name,th.training_name_raw) AS training_name,
               th.training_date,th.completion_date,
               COALESCE(th.completion_year,
                        CAST(strftime('%Y',COALESCE(th.completion_date,th.training_date)) AS INTEGER)) AS completion_year,
               COALESCE(th.result_status,th.completion_status) AS history_result,
               th.provider,th.valid_until,th.certificate_reference,th.evidence_note,
               COALESCE(th.record_status,'Active') AS record_status
        FROM training_history th
        JOIN employees e ON e.employee_pk=th.employee_pk
        LEFT JOIN positions p ON p.position_id=e.current_position_id
        LEFT JOIN training_catalog tc ON tc.training_id=COALESCE(th.training_id,th.training_pk)
        WHERE COALESCE(th.record_status,'Active')<>'Archived'
    """
    params: list[Any] = []
    clauses = []
    entities = entities or {}
    employee_pks = [int(x) for x in entities.get("employee_pks", [])]
    if employee_pks:
        clauses.append("th.employee_pk IN (" + ",".join("?" for _ in employee_pks) + ")")
        params.extend(employee_pks)
    training_ids = [int(x) for x in entities.get("training_ids", [])]
    if training_ids:
        clauses.append("COALESCE(th.training_id,th.training_pk) IN (" + ",".join("?" for _ in training_ids) + ")")
        params.extend(training_ids)
    years = [int(x) for x in re.findall(r"\b(?:19|20)\d{2}\b", normalize_question(q))]
    if years:
        clauses.append(
            "COALESCE(th.completion_year,CAST(strftime('%Y',COALESCE(th.completion_date,th.training_date)) AS INTEGER)) "
            "IN (" + ",".join("?" for _ in years) + ")"
        )
        params.extend(years)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(th.completion_date,th.training_date) DESC,e.employee_name,training_name"
    return pd.read_sql_query(sql, con, params=params).fillna(""), "Training History"


def query_actions(con, q: str, entities=None):
    """Read active Action Center records; the AI endpoint never mutates them."""
    sql = """
        SELECT a.action_id,a.employee_pk,e.employee_code,e.employee_name,p.position_name,
               a.requirement_id,tc.training_name,a.title,a.action_type,a.priority,
               a.pic_user_id,COALESCE(u.display_name,'Unassigned') AS pic_name,
               a.due_date,a.status,a.no_action_reason,a.remarks,a.completed_at,a.closed_at,
               (SELECT COUNT(*) FROM evidence_documents ed
                 WHERE ed.entity_type='action' AND ed.entity_id=a.action_id AND ed.active_flag=1) AS evidence_count
        FROM actions a
        JOIN employees e ON e.employee_pk=a.employee_pk
        LEFT JOIN positions p ON p.position_id=e.current_position_id
        LEFT JOIN training_requirements tr ON tr.requirement_id=a.requirement_id
        LEFT JOIN training_catalog tc ON tc.training_id=tr.training_id
        LEFT JOIN app_users u ON u.user_id=a.pic_user_id
        WHERE COALESCE(a.is_deleted,0)=0
    """
    params: list[Any] = []
    clauses = []
    entities = entities or {}
    pks = [int(x) for x in entities.get("employee_pks", [])]
    if pks:
        clauses.append("a.employee_pk IN (" + ",".join("?" for _ in pks) + ")")
        params.extend(pks)
    status_names = [
        "Waiting External Party", "No Action Required", "Waiting Evidence",
        "In Progress", "Completed", "Cancelled", "Scheduled", "Planned", "Closed", "Open",
    ]
    qn = normalize_question(q)
    requested = next((s for s in status_names if re.search(r"\b" + re.escape(s.lower()) + r"\b", qn)), None)
    if requested:
        clauses.append("lower(a.status)=?")
        params.append(requested.lower())
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(a.due_date,'9999-12-31'),a.action_id DESC"
    df = pd.read_sql_query(sql, con, params=params).fillna("")
    if not df.empty:
        readiness = []
        reasons = []
        for _, row in df.iterrows():
            ready = action_completion_ready(con, dict(row))
            readiness.append("READY" if ready.get("ready") else "NOT READY")
            reasons.append(str(ready.get("reason") or ""))
        df["closure_readiness"] = readiness
        df["closure_reason"] = reasons
    return df, "Action Center"


def _employee_names(df: pd.DataFrame):
    if df.empty or "employee_name" not in df.columns: return set()
    return set(df["employee_name"].dropna().astype(str).str.strip())

def _filter_cert_problem(df: pd.DataFrame):
    if df.empty: return df
    out=df.copy()
    if "display_status" not in out.columns:
        dt=pd.to_datetime(out.get("expired_date"),errors="coerce")
        today_ts=analysis_today()
        out["display_status"]=dt.map(lambda x: "Expired" if pd.notna(x) and x<today_ts else ("Near Expiry" if pd.notna(x) and today_ts<=x<=today_ts+pd.Timedelta(days=NEAR_DAYS) else ("Active" if pd.notna(x) else "In Process")))
    return out[out["display_status"].isin(["Expired","Near Expiry","In Process"])].copy()


def detect_boolean_logic(q: str, intents=None):
    """Detect explicit boolean intent for cross-agent employee coordination.

    Returns a compact plan:
      - single: normal query (no explicit boolean operator)
      - and: all detected domain conditions must hold on the same employee
      - or: any of the detected domain conditions may hold
      - or_then_and_cert: (training OR competency) AND certification
    """
    ql = normalize_question(q)
    intents = set(intents or detect_intents(ql))
    has_or = any(x in ql for x in [" atau ", " or ", "/atau "])
    has_and = any(x in ql for x in [" dan ", " and ", " sekaligus ", " + "])

    # Specific natural-language grouping used by the UAT query:
    # (Training OR Competency) AND Certification Expired
    if has_or and "certification" in intents and ("training" in intents or "competency" in intents):
        if any(x in ql for x in [
            "training atau competency dan sertifikasinya",
            "training atau kompetensi dan sertifikasinya",
            "training atau competency dan sertifikasi",
            "training atau kompetensi dan sertifikasi",
            "training atau competency"
        ]) and any(x in ql for x in ["expired", "kedaluwarsa", "kadaluarsa"]):
            return "or_then_and_cert"

    if has_or:
        return "or"
    if has_and and len(intents) > 1:
        return "and"
    return "single"


def _filter_results_to_people(results, people):
    """Restrict every employee-level agent dataset to a coordinated employee set."""
    people = set(str(x).strip() for x in (people or []) if str(x).strip())
    for agent, payload in results.items():
        df = payload.get("data", pd.DataFrame())
        if not df.empty and "employee_name" in df.columns:
            payload["data"] = df[df["employee_name"].astype(str).str.strip().isin(people)].copy()
    return results


def coordinate_employee_scope(results, question: str, intents=None):
    """Apply boolean employee-level coordination across specialist agents.

    Returns (results, coordination_metadata).  The raw evidence remains inside
    each specialist result; only employee scope is changed.
    """
    logic = detect_boolean_logic(question, intents)
    frames = {a: p.get("data", pd.DataFrame()) for a, p in results.items() if not p.get("data", pd.DataFrame()).empty}
    sets_by_agent = {a: _employee_names(df) for a, df in frames.items() if "employee_name" in df.columns}
    sets_by_agent = {a:s for a,s in sets_by_agent.items() if s}
    if len(sets_by_agent) < 2:
        return results, {"logic": logic, "dimension": "employee" if sets_by_agent else None, "values": sorted(next(iter(sets_by_agent.values())) if sets_by_agent else set())}

    if logic == "or_then_and_cert" and "Certification Agent" in sets_by_agent:
        left_sets=[sets_by_agent[a] for a in ("Training Agent", "Competency Agent") if a in sets_by_agent]
        left = set.union(*left_sets) if left_sets else set()
        selected = left & sets_by_agent["Certification Agent"]
    elif logic == "or":
        selected = set.union(*sets_by_agent.values())
    else:
        selected = set.intersection(*sets_by_agent.values())

    if selected:
        results = _filter_results_to_people(results, selected)
    else:
        # Explicit boolean expression with no matching employees should clear
        # employee-level frames rather than leaving unrelated evidence behind.
        results = _filter_results_to_people(results, set())

    return results, {
        "logic": logic,
        "dimension": "employee",
        "values": sorted(selected),
        "source_sets": {a: sorted(v) for a,v in sets_by_agent.items()},
    }

def _filter_comp_uncertain(df: pd.DataFrame):
    if df.empty or "coverage_status" not in df.columns: return df
    return df[df["coverage_status"].astype(str).str.casefold().isin({"validation required","not assessed"})].copy()

def _build_decision_summary(results, coordinated_people=None):
    frames={a:p["data"] for a,p in results.items() if not p["data"].empty}
    sets=[_employee_names(df) for df in frames.values() if "employee_name" in df.columns]
    if not sets: return []
    common=set(coordinated_people) if coordinated_people is not None else (set.intersection(*sets) if len(sets)>1 else sets[0])
    if not common: return []
    out=[]
    for person in sorted(common):
        comp=frames.get("Competency Agent",pd.DataFrame()); tr=frames.get("Training Agent",pd.DataFrame()); cert=frames.get("Certification Agent",pd.DataFrame())
        cg=comp[comp["employee_name"].astype(str).eq(person)] if not comp.empty and "employee_name" in comp.columns else pd.DataFrame()
        tg=tr[tr["employee_name"].astype(str).eq(person)] if not tr.empty and "employee_name" in tr.columns else pd.DataFrame()
        ce=cert[cert["employee_name"].astype(str).eq(person)] if not cert.empty and "employee_name" in cert.columns else pd.DataFrame()
        pos=""
        for g,c in [(cg,"position_tna"),(cg,"position_cert_db"),(tg,"position_standard"),(tg,"position_name"),(ce,"position_name")]:
            if not g.empty and c in g.columns:
                v=g[c].dropna().astype(str).str.strip(); v=v[v!=""]
                if len(v): pos=v.iloc[0]; break
        statuses=sorted(set(cg.get("coverage_status",pd.Series(dtype=str)).dropna().astype(str)))
        priorities=sorted(set(cg.get("priority",pd.Series(dtype=str)).dropna().astype(str)))
        trainings=list(dict.fromkeys(tg.get("training_name",pd.Series(dtype=str)).dropna().astype(str).str.strip()))
        cstatus=list(dict.fromkeys(ce.get("display_status",pd.Series(dtype=str)).dropna().astype(str)))
        cnames=list(dict.fromkeys(ce.get("certification_name",pd.Series(dtype=str)).dropna().astype(str)))
        issues=[x for x in cstatus if x in {"Expired","Near Expiry","In Process"}]
        gap="Potential Gap" in statuses; uncertain=bool(set(statuses)&{"Validation Required","Not Assessed"}); cproblem=bool(issues)
        if gap and cproblem: final,action,reason,prio="High Priority – Validation & Renewal","Validate competency/training evidence and follow up certification renewal or status validation","Potential Gap ditemukan bersamaan dengan kondisi sertifikasi yang memerlukan tindak lanjut.","High"
        elif gap: final,action,reason,prio="Potential Gap – Validation Required","Validate training/certification fulfillment","Potential Gap masih indikatif dan belum menjadi gap final karena Training History aktual belum tersedia.",("High" if "High" in priorities else "Validation")
        elif uncertain and cproblem: final,action,reason,prio="Validation Required","Validate competency evidence and certification status","Status kompetensi belum cukup pasti dan terdapat kondisi sertifikasi yang memerlukan tindak lanjut.",("High" if "High" in priorities else "Validation")
        elif cproblem and trainings: final,action,reason,prio="Training + Certification Follow-up","Review applicable training requirement and follow up certification renewal/validation","Training Requirement terpetakan dan sertifikasi memiliki kondisi yang memerlukan tindak lanjut.",("High" if "High" in priorities else "Validation")
        elif cproblem: final,action,reason,prio="Certification Follow-up","Review certification status and schedule renewal/validation as applicable","Sertifikasi memiliki kondisi Expired, Near Expiry, atau In Process.",("High" if "High" in priorities else "Validation")
        elif uncertain: final,action,reason,prio="Validation Required","Validate competency evidence or position mapping","Coverage belum cukup pasti untuk keputusan final.","Validation"
        elif trainings: final,action,reason,prio="Training Requirement Identified","Review applicable training requirement; fulfillment requires Training History","Training Requirement terpetakan berdasarkan posisi karyawan, tetapi Training History aktual belum tersedia.","—"
        else: final,action,reason,prio="Review Evidence","Review supporting evidence","Hasil berasal dari evidence agent yang terkoordinasi.","—"
        cond=statuses.copy()
        condition_details=[]
        if statuses:
            condition_details.append({"domain":"Competency","items":statuses})
        if issues:
            condition_details.append({"domain":"Certification Issue","items":issues})
        cert_details=[]
        if not ce.empty:
            for _, cr in ce.iterrows():
                cert_details.append({
                    "certification_name": str(cr.get("certification_name", "—")),
                    "status": str(cr.get("display_status", cr.get("status", "—"))),
                    "expired_date": str(cr.get("expired_date", "—")) if pd.notna(cr.get("expired_date")) else "—",
                    "certification_date": str(cr.get("certification_date", "—")) if pd.notna(cr.get("certification_date")) else "—",
                })
        if issues:
            cond.append("Certification: "+", ".join(issues))
        if trainings:
            cond.append(f"{len(trainings)} Training Requirement")
        if trainings:
            condition_details.append({"domain":"Training Requirement","items":[f"{len(trainings)} requirement(s)"]})
        out.append({
            "employee_name":person,
            "position_name":pos or "—",
            "conditions":" + ".join(cond) if cond else "—",
            "condition_details":condition_details,
            "training_requirements":"; ".join(trainings[:12])+(f"; +{len(trainings)-12} lainnya" if len(trainings)>12 else "") if trainings else "—",
            "training_requirement_list":trainings,
            "training_count":len(trainings),
            "certification_names":"; ".join(cnames[:8])+(f"; +{len(cnames)-8} lainnya" if len(cnames)>8 else "") if cnames else "—",
            "certification_name_list":cnames,
            "certification_details":cert_details,
            "certification_count":len(cert_details),
            "certification_status":", ".join(cstatus) if cstatus else "—",
            "coverage_status":", ".join(statuses) if statuses else "—",
            "priority":prio,
            "final_status":final,
            "recommended_action":action,
            "decision_reason":reason,
        })
    return out

def _apply_multi_agent_constraints(con, question, details, results):
    comp=results.get("Competency Agent",{}).get("data",pd.DataFrame())
    cert=results.get("Certification Agent",{}).get("data",pd.DataFrame())

    # Certification problem = only genuinely actionable states.
    if ("certification_issue" in details or "three_domain_issue" in details) and not cert.empty:
        cert=_filter_cert_problem(cert)
        results["Certification Agent"]["data"]=cert
        results["Certification Agent"]["mode"]="Certification Issue"
        results["Certification Agent"]["rationale"]="Kondisi sertifikasi bermasalah dibatasi pada Expired, Near Expiry, atau In Process."

    # Competency problem/uncertainty = statuses that need validation.
    if ("competency_uncertain" in details or "competency_issue" in details or "three_domain_issue" in details) and not comp.empty:
        comp=_filter_comp_uncertain(comp)
        results["Competency Agent"]["data"]=comp
        results["Competency Agent"]["mode"]="Competency Issue / Uncertain"
        results["Competency Agent"]["rationale"]="Kondisi kompetensi yang memerlukan tindak lanjut dibatasi pada Validation Required dan Not Assessed."

    # For coordinated questions, Training must be employee-level, never the global 787 requirement list.
    if "Training Agent" in results and ("Competency Agent" in results or "Certification Agent" in results):
        seed=[]
        if not comp.empty:
            seed.extend(sorted(_employee_names(comp)))
        if not cert.empty:
            seed.extend(sorted(_employee_names(cert)))
        people=sorted(set(seed))
        if people:
            t2,_=query_training_employee_level(con,question,{"employees":people,"positions":[]})
            results["Training Agent"]["data"]=t2
            results["Training Agent"]["mode"]="Training Requirement – Coordinated Employee Context"
            results["Training Agent"]["rationale"]="Training Requirement dibatasi pada karyawan yang memenuhi kondisi agent lain."

    return results

def _decision_trace(results, decision_summary):
    rows=[{"agent":a,"mode":p.get("mode",""),"records":len(p.get("data",pd.DataFrame()))} for a,p in results.items()]
    if decision_summary: rows.append({"agent":"Decision Synthesis","mode":"Employee-level coordinated decision","records":len(decision_summary)})
    return rows


def _is_summary_query(qnorm: str) -> bool:
    q = qnorm.casefold()
    markers = [
        "berikan ringkasan",
        "ringkasan kondisi",
        "ringkas kondisi",
        "summary kondisi",
        "kondisi training, competency, dan certification",
        "kondisi training competency certification",
        "secara keseluruhan",
        "overview kondisi",
    ]
    return any(m in q for m in markers)


def _build_system_summary(con) -> dict[str, Any]:
    d = analysis_today().date()
    employee_count = int(pd.read_sql_query(
        "SELECT COUNT(*) n FROM employees WHERE active_flag=1", con
    ).iloc[0]["n"])
    cert_count = int(pd.read_sql_query(
        "SELECT COUNT(*) n FROM certifications", con
    ).iloc[0]["n"])
    expired_count = int(pd.read_sql_query(
        "SELECT COUNT(*) n FROM certifications WHERE expired_date IS NOT NULL AND date(expired_date) < date(?)",
        con, params=(d.isoformat(),)
    ).iloc[0]["n"])
    near_count = int(pd.read_sql_query(
        """SELECT COUNT(*) n FROM certifications
           WHERE expired_date IS NOT NULL
             AND date(expired_date) >= date(?)
             AND date(expired_date) <= date(?, '+90 day')""",
        con, params=(d.isoformat(), d.isoformat())
    ).iloc[0]["n"])
    history_count = int(pd.read_sql_query(
        "SELECT COUNT(*) n FROM training_history", con
    ).iloc[0]["n"])
    coverage = pd.read_sql_query(
        "SELECT coverage_status, COUNT(*) total FROM v_coverage_gap_results GROUP BY coverage_status",
        con
    ).fillna("")
    coverage_map = {str(r["coverage_status"]): int(r["total"]) for _, r in coverage.iterrows()}
    return {
        "employees": employee_count,
        "certifications": cert_count,
        "expired": expired_count,
        "near_expiry_90d": near_count,
        "training_history": history_count,
        "coverage": coverage_map,
        "coverage_total": int(sum(coverage_map.values())),
    }

def _context_domain(intents: set[str], details: set[str]) -> str:
    if "action" in intents:
        return "action"
    if "training_history" in details:
        return "training_history"
    if "training_fulfillment" in details:
        return "training_fulfillment"
    if "training" in intents:
        return "training_requirement"
    if "certification" in intents:
        return "certification"
    if "competency" in intents:
        return "competency"
    return ""


def _result_context(previous: dict[str, Any], domain: str, entities: dict[str, Any],
                    details: set[str], results: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, verified context for the next turn.

    Database identifiers are retained; the next turn never re-resolves a person
    from a fuzzy text fragment.
    """
    ctx = dict(previous or {})
    ctx.update({
        "domain": domain or ctx.get("domain", ""),
        "employee_pks": list(entities.get("employee_pks", []) or ctx.get("employee_pks", [])),
        "employees": list(entities.get("employees", []) or ctx.get("employees", [])),
        "training_ids": list(entities.get("training_ids", []) or ctx.get("training_ids", [])),
        "training_names": list(entities.get("training_names", []) or ctx.get("training_names", [])),
        "last_intent_details": sorted(details),
    })
    cert_payload = results.get("Certification Agent", {})
    cert_df = cert_payload.get("data", pd.DataFrame())
    if not cert_df.empty and "certification_id" in cert_df.columns:
        ctx["certification_ids"] = [
            int(x) for x in pd.to_numeric(cert_df["certification_id"], errors="coerce").dropna().unique()
        ]
        if "certification_name" in cert_df.columns:
            ctx["certification_names"] = list(dict.fromkeys(
                cert_df["certification_name"].dropna().astype(str).tolist()
            ))
    elif entities.get("certification_ids"):
        ctx["certification_ids"] = list(entities["certification_ids"])
    return ctx


def _terminal_result(status: str, entities=None, details=None, context=None, **extra):
    result = {
        "intents": [], "intent_details": sorted(details or []), "agents": [],
        "entities": entities or {}, "results": {}, "intersection": None,
        "decision_summary": [], "decision_trace": [], "summary_query": False,
        "system_summary": None, "evidence_total": 0, "unique_employee_count": 0,
        "total": 0, "status": status, "boolean_logic": "single", "plan": {},
        "conversation_context": context or {},
    }
    result.update(extra)
    return result


def run_query(db_path, question: str, context: dict[str, Any] | None = None):
    q = normalize_question(question)
    previous = dict(context or {})
    results: dict[str, Any] = {}
    agents: list[str] = []

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        entities = extract_entities(con, q)
        details = set(detect_intent_details(q))
        intents = set(detect_intents(q))
        explicit_employee = entities.get("employee_resolution") not in {None, "none"}

        if "sensitive_request" in details:
            return _terminal_result("SECURITY_REFUSAL", entities, details, previous)
        if "unsafe_mutation" in details:
            return _terminal_result("MUTATION_BLOCKED", entities, details, previous)
        if "action" in intents and "mutation_request" in details:
            return _terminal_result("MUTATION_BLOCKED", entities, details, previous, intents=["action"])

        if entities.get("employee_resolution") == "not_found":
            return _terminal_result(
                "ENTITY_NOT_FOUND", entities, details, previous,
                reference=entities.get("employee_reference", ""),
            )
        if entities.get("employee_resolution") == "ambiguous":
            return _terminal_result("ENTITY_AMBIGUOUS", entities, details, previous)

        if "unsupported_instructor" in details:
            return _terminal_result("DATA_NOT_AVAILABLE", entities, details, previous, requested_field="instruktur")
        if "prediction_request" in details:
            return _terminal_result("PREDICTION_UNAVAILABLE", entities, details, previous)
        if "unsupported_ranking" in details:
            return _terminal_result("CRITERIA_REQUIRED", entities, details, previous)

        if {"training_history", "training_requirement"}.issubset(details) and re.search(r"\b(?:19|20)\d{2}\b", q):
            return _terminal_result(
                "UNSUPPORTED_FILTER", entities, details, previous,
                clarification="Training Requirement tidak memiliki atribut tahun. Gunakan Training History untuk filter berdasarkan tahun.",
            )

        # Carry verified identifiers only when the user did not name a new person.
        if not explicit_employee and previous:
            for key in ["employee_pks", "employees", "training_ids", "training_names", "certification_ids"]:
                if not entities.get(key) and previous.get(key):
                    entities[key] = list(previous[key])
        elif explicit_employee and previous and not entities.get("training_ids"):
            # "Sekarang AMBA" may keep the same requirement, but never a certificate
            # record owned by the previously selected employee.
            entities["training_ids"] = list(previous.get("training_ids", []))
            entities["training_names"] = list(previous.get("training_names", []))
            entities["certification_ids"] = []
            previous.pop("certification_ids", None)
            previous.pop("certification_names", None)

        # Resolve short follow-ups from the previous verified context.
        followup_domain = str(previous.get("domain", ""))
        if "training_fulfillment" in details:
            intents = {"training"}
        elif "training_history" in details:
            intents = {"training"}
        elif "expiry_date_lookup" in details:
            intents = {"certification"}
        elif not intents and followup_domain:
            if any(x in q for x in ["mana yang belum", "belum terpenuhi", "berapa persen"]):
                intents = {"training"}
                details.add("training_fulfillment")
                details.add("unfulfilled_only")
            else:
                domain_intent = {
                    "training_history": "training", "training_fulfillment": "training",
                    "training_requirement": "training", "certification": "certification",
                    "competency": "competency", "action": "action",
                }.get(followup_domain)
                if domain_intent:
                    intents = {domain_intent}
                    if followup_domain in {"training_history", "training_fulfillment"}:
                        details.add(followup_domain)

        summary_query = _is_summary_query(q)
        if summary_query:
            intents.update({"training", "competency", "certification"})

        if "action" in intents and "mutation_request" in details:
            ctx = _result_context(previous, "action", entities, details, results)
            return _terminal_result("MUTATION_BLOCKED", entities, details, ctx, intents=["action"])

        # A person name by itself is not permission to dump three unrelated domains.
        if not intents and entities.get("employees"):
            return _terminal_result(
                "CLARIFICATION_REQUIRED", entities, details, previous,
                clarification="Pilih data yang ingin dilihat: Training Requirement, Training History, sertifikasi, kompetensi, atau Action Center.",
            )
        if not intents:
            return _terminal_result("NO_INTENT", entities, details, previous)

        # Minimum-necessary routing for every ordinary question.
        if "action" in intents:
            intents = {"action"}
        if "training_fulfillment" in details or "training_history" in details:
            intents = {"training"}

        wants_training_people = (
            "training" in intents and any(x in q for x in [
                "siapa yang butuh training", "siapa yang membutuhkan training",
                "siapa yang perlu training", "karyawan yang membutuhkan training",
            ])
        )
        if wants_training_people:
            details.add("employee_training_list")

        if "certification" in intents:
            df, mode = query_certification(con, q, entities)
            results["Certification Agent"] = {"mode": mode, "data": df, "rationale": build_rationale("Certification Agent", mode, df)}
            agents.append("Certification Agent")

        if "training" in intents:
            if "training_history" in details:
                df, mode = query_training_history(con, q, entities)
            elif "training_fulfillment" in details:
                df, mode = query_training_fulfillment(con, q, entities)
            elif "employee_training_list" in details:
                df, mode = query_training_employee_level(con, q, entities)
            else:
                df, mode = query_training(con, q, entities)
            results["Training Agent"] = {"mode": mode, "data": df, "rationale": build_rationale("Training Agent", mode, df)}
            agents.append("Training Agent")

        if "competency" in intents:
            df, mode = query_competency(con, q, entities)
            if df.empty and entities.get("employees"):
                fallback_df, fallback_mode = query_competency_pending_context(con, q, entities)
                if not fallback_df.empty:
                    df, mode = fallback_df, fallback_mode
                    details.add("pre_assessment_coverage")
            results["Competency Agent"] = {"mode": mode, "data": df, "rationale": build_rationale("Competency Agent", mode, df)}
            agents.append("Competency Agent")

        if "action" in intents:
            df, mode = query_actions(con, q, entities)
            results["Action Agent"] = {"mode": mode, "data": df, "rationale": "Action Center read-only query."}
            agents.append("Action Agent")

        boolean_plan = detect_boolean_logic(q, intents)
        intersection = None
        decision_summary: list[dict[str, Any]] = []
        if len(intents) > 1 and not summary_query:
            results = _apply_multi_agent_constraints(con, question, sorted(details), results)
            results, coordination = coordinate_employee_scope(results, q, intents)
            intersection = coordination if coordination.get("dimension") == "employee" else None
            decision_summary = _build_decision_summary(
                results, coordination.get("values") if intersection else None
            )
        system_summary = _build_system_summary(con) if summary_query else None

        evidence_total = int(sum(len(v["data"]) for v in results.values()))
        unique_employees: set[str] = set()
        for payload in results.values():
            frame = payload.get("data", pd.DataFrame())
            if "employee_name" in frame.columns:
                unique_employees.update(frame["employee_name"].dropna().astype(str).str.strip())
        total = len(intersection.get("values", [])) if intersection else evidence_total
        informational_empty = bool({"training_history", "training_fulfillment", "assessment_score"} & details)
        status = "OK" if total > 0 else ("INFO_ONLY" if informational_empty else "NO_MATCH")
        domain = _context_domain(intents, details)
        next_context = _result_context(previous, domain, entities, details, results)

    return {
        "intents": sorted(intents), "intent_details": sorted(details), "agents": agents,
        "entities": entities, "results": results, "intersection": intersection,
        "decision_summary": decision_summary,
        "decision_trace": _decision_trace(results, decision_summary) if decision_summary else [],
        "summary_query": summary_query, "system_summary": system_summary,
        "evidence_total": evidence_total,
        "unique_employee_count": len([x for x in unique_employees if x]),
        "total": int(total), "status": status, "boolean_logic": boolean_plan,
        "conversation_context": next_context,
        "plan": {"routing": agents, "details": sorted(details), "boolean_logic": boolean_plan},
    }


def log_query(db_path, question, result):
    agents = ", ".join(result.get("agents", []))
    with sqlite3.connect(db_path) as con:
        cur = con.execute("""
            INSERT INTO ai_query_log
            (question, normalized_question, intent, agents_used, result_count, response_status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            question,
            normalize_question(question),
            ", ".join(result.get("intents", [])),
            agents,
            result.get("total", 0),
            result.get("status", "UNKNOWN")
        ))
        query_id = cur.lastrowid
        for agent, payload in result.get("results", {}).items():
            df = payload["data"]
            if df.empty:
                continue
            for _, row in df.head(500).iterrows():
                record = row.where(pd.notna(row), None).to_dict()
                con.execute("""
                    INSERT INTO ai_result_cache(query_id, source_table, record_key, payload_json)
                    VALUES (?, ?, ?, ?)
                """, (
                    query_id,
                    agent,
                    str(record.get("certification_id", record.get("requirement_id", record.get("employee_name", "")))),
                    json.dumps(record, default=str, ensure_ascii=False)
                ))
        con.commit()
    return query_id


def export_results(result, output_path):
    output_path = Path(output_path)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        decision = pd.DataFrame(result.get("decision_summary", []))
        if not decision.empty:
            decision.to_excel(writer, sheet_name="Decision Summary", index=False)
        evidence=[]
        for agent,p in result.get("results",{}).items():
            df=p.get("data",pd.DataFrame())
            if not df.empty:
                f=df.copy(); f.insert(0,"agent",agent); evidence.append(f)
        if evidence:
            pd.concat(evidence,ignore_index=True,sort=False).to_excel(writer,sheet_name="Evidence",index=False)
        else:
            pd.DataFrame([{"message":"Tidak ada evidence yang sesuai."}]).to_excel(writer,sheet_name="Evidence",index=False)
        trace=pd.DataFrame(result.get("decision_trace",[]))
        if not trace.empty:
            trace.to_excel(writer,sheet_name="Agent Trace",index=False)
        for agent,p in result.get("results",{}).items():
            df=p.get("data",pd.DataFrame())
            if not df.empty:
                df.to_excel(writer,sheet_name=agent.replace(" Agent","")[:31],index=False)
    return output_path


def response_text(question, result):
    """Answer the user's question directly without exposing internal routing."""
    status = result.get("status")
    details = set(result.get("intent_details", []))
    entities = result.get("entities", {})
    people = list(entities.get("employees", []))
    person = ", ".join(people) if people else ""

    if status == "ENTITY_NOT_FOUND":
        ref = result.get("reference") or entities.get("employee_reference") or "tersebut"
        return f"Employee '{ref}' tidak ditemukan. Periksa Employee ID atau gunakan nama lengkap yang terdaftar."
    if status == "ENTITY_AMBIGUOUS":
        candidates = entities.get("employee_candidates", [])
        labels = [f"{x.get('employee_name')} ({x.get('employee_code') or 'tanpa ID'})" for x in candidates]
        return "Nama tersebut cocok dengan beberapa employee. Pilih salah satu: " + "; ".join(labels) + "."
    if status == "MUTATION_BLOCKED":
        if "unsafe_mutation" in details:
            return "Evidence tidak dihapus melalui AI Assistant. Pengelolaan evidence harus dilakukan dari modul berwenang dan tetap mengikuti audit trail."
        return (
            "Status Action tidak diubah melalui AI Assistant. Buka Action Center, pilih action yang dimaksud, "
            "lalu gunakan Manage; status Closed tetap hanya dapat dipilih saat closure readiness READY."
        )
    if status == "SECURITY_REFUSAL":
        return "Permintaan tersebut ditolak karena menyangkut password, credential, atau struktur internal yang bersifat sensitif."
    if status == "CLARIFICATION_REQUIRED":
        prefix = f"{person} ditemukan. " if person else ""
        return prefix + str(result.get("clarification") or "Sebutkan data yang ingin dilihat.")
    if status == "UNSUPPORTED_FILTER":
        return str(result.get("clarification") or "Filter tersebut tidak tersedia pada sumber data yang diminta.")
    if status == "DATA_NOT_AVAILABLE":
        return f"Data {result.get('requested_field') or 'tersebut'} tidak tersedia pada record sistem."
    if status == "PREDICTION_UNAVAILABLE":
        return "Sistem tidak memiliki data yang cukup untuk memprediksi kapan employee akan lulus assessment."
    if status == "CRITERIA_REQUIRED":
        return "Employee terbaik tidak dapat ditentukan tanpa kriteria penilaian yang jelas dan data pendukung."
    if status == "NO_INTENT":
        return "Pertanyaan belum cukup spesifik. Sebutkan employee dan data yang ingin dilihat, misalnya Training History, sertifikasi, kompetensi, atau action."

    if result.get("summary_query"):
        s = result.get("system_summary") or {}
        cov = s.get("coverage", {})
        return (
            f"Terdapat {s.get('employees', 0)} employee aktif, {s.get('certifications', 0)} sertifikasi, "
            f"{s.get('expired', 0)} Expired, {s.get('near_expiry_90d', 0)} Near Expiry, dan "
            f"{s.get('training_history', 0)} Training History. Coverage saat ini: "
            f"{cov.get('Covered', 0)} Covered, {cov.get('Potential Gap', 0)} Potential Gap, "
            f"{cov.get('Validation Required', 0)} Validation Required, dan {cov.get('Not Assessed', 0)} Not Assessed."
        )

    training = result.get("results", {}).get("Training Agent", {})
    tdf = training.get("data", pd.DataFrame())
    if "training_history" in details:
        years = re.findall(r"\b(?:19|20)\d{2}\b", normalize_question(question))
        year_text = f" pada tahun {', '.join(years)}" if years else ""
        subject = f" untuk {person}" if person else ""
        named_training = ", ".join(entities.get("training_names", []))
        training_text = f" {named_training}" if named_training else ""
        if tdf.empty:
            return (
                f"Tidak ditemukan Training History{training_text}{subject}{year_text}. "
                "Artinya belum ada evidence yang tercatat untuk filter tersebut, bukan memastikan training tidak pernah diikuti."
            )
        names = list(dict.fromkeys(tdf.get("training_name", pd.Series(dtype=str)).dropna().astype(str).tolist()))
        shown = ", ".join(names[:12])
        suffix = f"; dan {len(names)-12} lainnya" if len(names) > 12 else ""
        return f"Ditemukan {len(tdf)} Training History{subject}{year_text}: {shown}{suffix}."

    if "training_fulfillment" in details:
        if tdf.empty:
            return f"Tidak ada Training Requirement yang dapat dihitung untuk {person or 'employee yang diminta'}."
        fulfilled = int((tdf["fulfillment_status"].astype(str) == "Fulfilled").sum())
        total = len(tdf)
        missing = total - fulfilled
        if "unfulfilled_only" in details:
            return (
                f"Untuk {person or 'employee tersebut'}, {missing} requirement belum memiliki evidence completion pada Training History. "
                "Status ini berarti evidence belum tercatat, bukan memastikan training tidak pernah diikuti."
            )
        return (
            f"Pemenuhan training {person or 'employee tersebut'} berdasarkan Training History tercatat adalah "
            f"{fulfilled} dari {total} requirement ({fulfilled/total*100:.1f}%). "
            f"Sebanyak {missing} requirement belum memiliki evidence completion; ini bukan bukti bahwa training tidak pernah diikuti."
        )

    if result.get("intents") == ["training"]:
        if tdf.empty:
            return f"Tidak ditemukan Training Requirement yang sesuai{f' untuk {person}' if person else ''}."
        names = list(dict.fromkeys(tdf.get("training_name", pd.Series(dtype=str)).dropna().astype(str).tolist()))
        shown = ", ".join(names[:16])
        suffix = f"; dan {len(names)-16} lainnya" if len(names) > 16 else ""
        return f"{person or 'Hasil'} memiliki {len(tdf)} Training Requirement aktif: {shown}{suffix}."

    cert = result.get("results", {}).get("Certification Agent", {})
    cdf = cert.get("data", pd.DataFrame())
    if result.get("intents") == ["certification"]:
        if cdf.empty:
            return f"Tidak ditemukan sertifikasi yang sesuai{f' untuk {person}' if person else ''}."
        if "certificate_number_lookup" in details:
            values = [
                f"{row.get('certification_name', 'Sertifikasi')}: {row.get('certificate_number')}"
                for _, row in cdf.iterrows() if str(row.get("certificate_number") or "").strip()
            ]
            return ("Nomor sertifikat " + (f"{person}: " if person else "") + "; ".join(values) + ".") if values else f"Nomor sertifikat {person or 'yang diminta'} belum tersedia pada data yang tercatat."
        if "expiry_date_lookup" in details:
            items = [
                f"{row.get('certification_name', 'Sertifikasi')} milik {row.get('employee_name', person or 'employee')} — {row.get('expired_date') or 'Expiry Date belum tersedia'}"
                for _, row in cdf.iterrows()
            ]
            return "Expiry Date: " + "; ".join(items[:12]) + "."
        if "near_expiry" in details:
            names = list(dict.fromkeys(cdf["employee_name"].dropna().astype(str).tolist())) if "employee_name" in cdf else []
            return f"Ditemukan {len(cdf)} sertifikasi Near Expiry" + (f" untuk {', '.join(names)}" if names else "") + "."
        if "expired" in details:
            return f"Ditemukan {len(cdf)} sertifikasi Expired" + (f" untuk {person}" if person else "") + "."
        return f"Ditemukan {len(cdf)} sertifikasi" + (f" untuk {person}" if person else "") + "."

    competency = result.get("results", {}).get("Competency Agent", {})
    kdf = competency.get("data", pd.DataFrame())
    if result.get("intents") == ["competency"]:
        requirement = ", ".join(entities.get("training_names", [])) or (
            str(kdf.iloc[0].get("training_name", "requirement tersebut")) if not kdf.empty else "requirement tersebut"
        )
        if "assessment_score" in details:
            if not kdf.empty and str(kdf.iloc[0].get("assessment_state", "")):
                return f"Skor assessment {person or 'employee tersebut'} untuk {requirement} belum tersedia karena Initial Assessment belum dilakukan."
            return f"Skor assessment {person or 'employee tersebut'} untuk {requirement} tidak tersedia pada data yang tercatat."
        if "pre_assessment_coverage" in details and not kdf.empty:
            state = str(kdf.iloc[0].get("assessment_state", "Pending Initial Assessment"))
            return f"Status kompetensi {person or 'employee tersebut'} untuk {requirement} belum dapat ditentukan karena statusnya masih {state}."
        if kdf.empty:
            return f"Tidak ditemukan hasil kompetensi yang sesuai{f' untuk {person}' if person else ''}."
        statuses = list(dict.fromkeys(kdf.get("coverage_status", pd.Series(dtype=str)).dropna().astype(str).tolist()))
        return f"Status kompetensi {person or 'employee yang diminta'} untuk {requirement}: {', '.join(statuses)}."

    actions = result.get("results", {}).get("Action Agent", {})
    adf = actions.get("data", pd.DataFrame())
    if result.get("intents") == ["action"]:
        if adf.empty:
            return f"Tidak ditemukan action yang sesuai{f' untuk {person}' if person else ''}."
        statuses = ", ".join(f"{k}: {v}" for k, v in adf["status"].value_counts().to_dict().items())
        return f"Ditemukan {len(adf)} action{f' milik {person}' if person else ''} ({statuses})."

    if status == "NO_MATCH":
        return "Tidak ditemukan record yang sesuai dengan filter yang diminta."

    # Explicit cross-domain questions remain concise and user-facing.
    people_found = sorted({
        str(x).strip() for payload in result.get("results", {}).values()
        for x in payload.get("data", pd.DataFrame()).get("employee_name", pd.Series(dtype=str)).dropna()
        if str(x).strip()
    })
    if people_found:
        return f"Ditemukan {len(people_found)} employee yang sesuai: {', '.join(people_found[:20])}."
    return "Tidak ditemukan record yang sesuai dengan filter yang diminta."
