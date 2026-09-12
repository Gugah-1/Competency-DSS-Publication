from __future__ import annotations

import io
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


DATASET_CONFIG: dict[str, dict[str, Any]] = {
    'Employee Master': {
        'template': 'Employee_Template.xlsx',
        'headers': ['employee_id','employee_name','department','position','join_date','employment_status','remarks'],
        'required': ['employee_name','position'],
    },
    'Training History': {
        'template': 'Training_History_Template.xlsx',
        'headers': ['employee_id','training_name','training_date','completion_date','provider','result_status','valid_until','certificate_reference','remarks'],
        'required': ['employee_id','training_name','result_status'],
    },
    'Certification': {
        'template': 'Certification_Template.xlsx',
        'headers': ['employee_id','certification_name','certificate_number','issuer','issue_date','expiry_date','certification_status','renewal_status','remarks'],
        'required': ['employee_id','certification_name'],
    },
    'Training Requirement': {
        'template': 'Training_Requirement_Template.xlsx',
        'headers': ['position','training_name','requirement_type','regulatory_flag','requirement_source','effective_from','effective_to','requirement_status','remarks'],
        'required': ['position','training_name','requirement_type'],
    },
}

EMPLOYMENT_STATUS = {'Active','Resigned','Contract End','Retired','Suspended','Other'}
TRAINING_RESULTS = {'Scheduled','In Progress','Completed','Passed','Failed','Cancelled'}
REQUIREMENT_TYPES = {'Mandatory','Additional','Optional'}
REQUIREMENT_STATUS = {'Active','Inactive','Replaced'}
CERT_STATUS = {'Auto','Active','Near Expiry','Expired','In Process'}
RENEWAL_STATUS = {'','Not Due','Renewal Planned','Renewal Required','Renewal Pending','Renewal Review Required','Completed','Not Renewed','No Action Required'}


def _clean(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    if isinstance(v, pd.Timestamp):
        return v.strftime('%Y-%m-%d')
    return str(v).strip()


def _date(v: Any) -> tuple[str | None, str | None]:
    text = _clean(v)
    if not text:
        return None, None
    parsed = pd.to_datetime(text, errors='coerce')
    if pd.isna(parsed):
        return None, f'Tanggal tidak valid: {text}'
    return parsed.strftime('%Y-%m-%d'), None


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {'severity': severity, 'code': code, 'message': message}


def _status_from_issues(issues: list[dict[str, str]], duplicate: bool = False) -> str:
    if any(i['severity'] == 'error' for i in issues):
        return 'Rejected'
    if duplicate:
        return 'Duplicate'
    if any(i['severity'] == 'warning' for i in issues):
        return 'Warning'
    return 'Valid'


def read_template_excel(content: bytes, dataset_type: str) -> pd.DataFrame:
    cfg = DATASET_CONFIG[dataset_type]
    bio = io.BytesIO(content)
    try:
        xl = pd.ExcelFile(bio)
    except Exception as exc:
        raise ValueError(f'File Excel tidak dapat dibaca: {exc}')
    sheet = 'DATA_ENTRY' if 'DATA_ENTRY' in xl.sheet_names else xl.sheet_names[0]
    # Official template starts the header at Excel row 4. For plain files, fall back to first row.
    df = pd.read_excel(xl, sheet_name=sheet, header=3 if sheet == 'DATA_ENTRY' else 0, dtype=object)
    df.columns = [re.sub(r'\s*\*\s*$', '', str(c).strip()).lower() for c in df.columns]
    # Remove completely blank rows and accidental unnamed columns.
    df = df[[c for c in df.columns if not str(c).startswith('unnamed:')]].copy()
    if not df.empty:
        df = df.dropna(how='all')
    missing_headers = [h for h in cfg['headers'] if h not in df.columns]
    if missing_headers:
        raise ValueError('Template structure tidak dikenali. Kolom belum tersedia: ' + ', '.join(missing_headers))
    # Ignore extra columns, but keep canonical ordering.
    return df[cfg['headers']].copy()


def _employee_ref(con: sqlite3.Connection, employee_id: str) -> tuple[int | None, str | None]:
    if not employee_id:
        return None, None
    row = con.execute('SELECT employee_pk, employee_name FROM employees WHERE trim(employee_code)=trim(?) LIMIT 1', (employee_id,)).fetchone()
    return (int(row[0]), str(row[1])) if row else (None, None)


def _position_ref(con: sqlite3.Connection, position: str) -> int | None:
    row = con.execute('SELECT position_id FROM positions WHERE lower(trim(position_name))=lower(trim(?)) AND active_flag=1 LIMIT 1', (position,)).fetchone()
    return int(row[0]) if row else None


def _training_ref(con: sqlite3.Connection, training: str) -> int | None:
    row = con.execute('SELECT training_id FROM training_catalog WHERE lower(trim(training_name))=lower(trim(?)) AND active_flag=1 LIMIT 1', (training,)).fetchone()
    return int(row[0]) if row else None


def _cert_catalog_ref(con: sqlite3.Connection, name: str) -> int | None:
    row = con.execute('SELECT certification_catalog_id FROM certification_catalog WHERE lower(trim(certification_name))=lower(trim(?)) LIMIT 1', (name,)).fetchone()
    return int(row[0]) if row else None


def validate_row(con: sqlite3.Connection, dataset_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    n = {k: _clean(raw.get(k)) for k in DATASET_CONFIG[dataset_type]['headers']}
    duplicate = False
    action = 'insert'
    target_key = ''

    for h in DATASET_CONFIG[dataset_type]['required']:
        if not n[h]:
            issues.append(_issue('error','required',f'{h} wajib diisi.'))

    if dataset_type == 'Employee Master':
        if not n['employee_id']:
            issues.append(_issue('warning','employee_id_blank','Employee ID kosong; duplicate detection akan menggunakan nama.'))
        pos_id = _position_ref(con, n['position']) if n['position'] else None
        n['_position_id'] = pos_id
        if n['position'] and not pos_id:
            issues.append(_issue('error','position_unknown',f'Position tidak ditemukan pada Position Master: {n["position"]}'))
        if n['employment_status'] and n['employment_status'] not in EMPLOYMENT_STATUS:
            issues.append(_issue('error','employment_status_invalid','Employment Status tidak dikenali.'))
        n['employment_status'] = n['employment_status'] or 'Active'
        jd, err = _date(n['join_date'])
        n['join_date'] = jd
        if err: issues.append(_issue('error','join_date_invalid',err))
        existing = None
        if n['employee_id']:
            existing = con.execute('SELECT employee_pk,current_position_id FROM employees WHERE trim(employee_code)=trim(?) LIMIT 1',(n['employee_id'],)).fetchone()
        if not existing and n['employee_name']:
            existing = con.execute('SELECT employee_pk,current_position_id FROM employees WHERE lower(trim(employee_name))=lower(trim(?)) LIMIT 1',(n['employee_name'],)).fetchone()
        if existing:
            duplicate = True; action = 'update_existing'; target_key = str(existing[0]); n['_existing_employee_pk'] = int(existing[0]); n['_old_position_id'] = existing[1]
            issues.append(_issue('warning','employee_exists','Karyawan sudah ada. Default import akan skip; pilih Update Existing jika memang ingin memperbarui.'))

    elif dataset_type == 'Training History':
        emp_pk, emp_name = _employee_ref(con, n['employee_id'])
        n['_employee_pk'] = emp_pk
        if n['employee_id'] and not emp_pk:
            issues.append(_issue('error','employee_unknown',f'Employee ID tidak ditemukan: {n["employee_id"]}'))
        tr_id = _training_ref(con, n['training_name']) if n['training_name'] else None
        n['_training_id'] = tr_id
        if n['training_name'] and not tr_id:
            issues.append(_issue('error','training_unknown',f'Training tidak ditemukan pada Training Catalog: {n["training_name"]}'))
        if n['result_status'] and n['result_status'] not in TRAINING_RESULTS:
            issues.append(_issue('error','result_status_invalid','Result Status tidak dikenali.'))
        for field in ['training_date','completion_date','valid_until']:
            d, err = _date(n[field]); n[field]=d
            if err: issues.append(_issue('error',field+'_invalid',err))
        if n['result_status'] in {'Completed','Passed'} and not n['completion_date']:
            issues.append(_issue('error','completion_date_required','Completion Date wajib untuk Completed/Passed.'))
        if emp_pk and tr_id:
            duplicate_row = con.execute('''SELECT history_id FROM training_history
                WHERE employee_pk=? AND training_id=? AND COALESCE(completion_date,'')=COALESCE(?, '')
                  AND COALESCE(record_status,'Active')='Active' LIMIT 1''',(emp_pk,tr_id,n['completion_date'])).fetchone()
            if duplicate_row:
                duplicate=True; action='update_existing'; target_key=str(duplicate_row[0]); n['_existing_history_id']=int(duplicate_row[0])
                issues.append(_issue('warning','training_duplicate','Training History dengan employee, training, dan completion date yang sama sudah ada.'))

    elif dataset_type == 'Certification':
        emp_pk, _ = _employee_ref(con, n['employee_id'])
        n['_employee_pk'] = emp_pk
        if n['employee_id'] and not emp_pk:
            issues.append(_issue('error','employee_unknown',f'Employee ID tidak ditemukan: {n["employee_id"]}'))
        cat_id = _cert_catalog_ref(con, n['certification_name']) if n['certification_name'] else None
        n['_certification_catalog_id'] = cat_id
        if n['certification_name'] and not cat_id:
            issues.append(_issue('warning','cert_catalog_new','Certification belum ada di catalog; akan dibuat sebagai catalog baru saat Confirm Import.'))
        for field in ['issue_date','expiry_date']:
            d, err=_date(n[field]); n[field]=d
            if err: issues.append(_issue('error',field+'_invalid',err))
        if n['certification_status'] and n['certification_status'] not in CERT_STATUS:
            issues.append(_issue('error','cert_status_invalid','Certification Status tidak dikenali.'))
        if n['renewal_status'] not in RENEWAL_STATUS:
            issues.append(_issue('error','renewal_status_invalid','Renewal Status tidak dikenali.'))
        existing = None
        if n['certificate_number']:
            existing=con.execute('SELECT certification_id FROM certifications WHERE trim(certificate_number)=trim(?) LIMIT 1',(n['certificate_number'],)).fetchone()
        if not existing and emp_pk and n['certification_name']:
            existing=con.execute('''SELECT certification_id FROM certifications c LEFT JOIN certification_catalog cc ON cc.certification_catalog_id=c.certification_catalog_id
                WHERE c.employee_pk=? AND lower(trim(COALESCE(cc.certification_name,c.certification_name_raw)))=lower(trim(?))
                  AND COALESCE(c.issue_date,c.certification_date,'')=COALESCE(?, '') AND COALESCE(c.expiry_date,c.expired_date,'')=COALESCE(?, '')
                LIMIT 1''',(emp_pk,n['certification_name'],n['issue_date'],n['expiry_date'])).fetchone()
        if existing:
            duplicate=True; action='update_existing'; target_key=str(existing[0]); n['_existing_certification_id']=int(existing[0])
            issues.append(_issue('warning','cert_duplicate','Possible duplicate certification ditemukan.'))

    elif dataset_type == 'Training Requirement':
        pos_id = _position_ref(con, n['position']) if n['position'] else None
        tr_id = _training_ref(con, n['training_name']) if n['training_name'] else None
        n['_position_id'] = pos_id; n['_training_id'] = tr_id
        if n['position'] and not pos_id:
            issues.append(_issue('error','position_unknown',f'Position tidak ditemukan: {n["position"]}'))
        if n['training_name'] and not tr_id:
            issues.append(_issue('error','training_unknown',f'Training tidak ditemukan pada Training Catalog: {n["training_name"]}'))
        if n['requirement_type'] and n['requirement_type'] not in REQUIREMENT_TYPES:
            issues.append(_issue('error','requirement_type_invalid','Requirement Type tidak dikenali.'))
        n['requirement_status'] = n['requirement_status'] or 'Active'
        if n['requirement_status'] not in REQUIREMENT_STATUS:
            issues.append(_issue('error','requirement_status_invalid','Requirement Status tidak dikenali.'))
        for field in ['effective_from','effective_to']:
            d,err=_date(n[field]); n[field]=d
            if err: issues.append(_issue('error',field+'_invalid',err))
        if pos_id and tr_id:
            existing=con.execute('''SELECT requirement_id FROM training_requirements
                WHERE position_id=? AND training_id=? AND COALESCE(requirement_status,'Active')='Active' LIMIT 1''',(pos_id,tr_id)).fetchone()
            if existing:
                duplicate=True; action='update_existing'; target_key=str(existing[0]); n['_existing_requirement_id']=int(existing[0])
                issues.append(_issue('warning','requirement_duplicate','Requirement aktif untuk position dan training yang sama sudah ada.'))

    return {
        'status': _status_from_issues(issues, duplicate),
        'action': action,
        'target_key': target_key,
        'normalized': n,
        'issues': issues,
    }


def _batch_duplicate_key(dataset_type: str, n: dict[str, Any]) -> str:
    """Stable duplicate key inside one upload batch. Empty/invalid keys are ignored."""
    def norm(v): return _clean(v).casefold()
    if dataset_type == 'Employee Master':
        return ('employee_id:' + norm(n.get('employee_id'))) if n.get('employee_id') else (('employee_name:' + norm(n.get('employee_name'))) if n.get('employee_name') else '')
    if dataset_type == 'Training History':
        emp = n.get('_employee_pk') or norm(n.get('employee_id'))
        tr = n.get('_training_id') or norm(n.get('training_name'))
        return f'training:{emp}:{tr}:{norm(n.get("completion_date"))}' if emp and tr else ''
    if dataset_type == 'Certification':
        if n.get('certificate_number'):
            return 'certificate_no:' + norm(n.get('certificate_number'))
        emp = n.get('_employee_pk') or norm(n.get('employee_id'))
        cert = norm(n.get('certification_name'))
        return f'cert:{emp}:{cert}:{norm(n.get("issue_date"))}:{norm(n.get("expiry_date"))}' if emp and cert else ''
    if dataset_type == 'Training Requirement':
        pos = n.get('_position_id') or norm(n.get('position'))
        tr = n.get('_training_id') or norm(n.get('training_name'))
        return f'req:{pos}:{tr}' if pos and tr else ''
    return ''


def create_validation_batch(con: sqlite3.Connection, dataset_type: str, source_file: str, df: pd.DataFrame, user_id: str) -> dict[str, Any]:
    batch_id = 'IMP-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:5].upper()
    validated=[]
    counts={'Valid':0,'Warning':0,'Duplicate':0,'Rejected':0}
    seen_batch_keys: dict[str,int] = {}
    for idx, row in df.iterrows():
        raw={k: (_clean(row.get(k))) for k in DATASET_CONFIG[dataset_type]['headers']}
        result=validate_row(con,dataset_type,raw)
        row_number=int(idx)+5  # official template data starts on Excel row 5
        key=_batch_duplicate_key(dataset_type,result.get('normalized',{}))
        if key and key in seen_batch_keys and result['status'] != 'Rejected':
            first_row=seen_batch_keys[key]
            result['issues'].append(_issue('warning','batch_duplicate',f'Duplicate dalam file yang sama; key yang sama sudah muncul pada Excel row {first_row}. Baris ini akan dilewati saat Confirm Import.'))
            result['status']='Duplicate'
            result['action']='skip_batch_duplicate'
            result['target_key']=f'batch_row:{first_row}'
            result['normalized']['_batch_duplicate_of_row']=first_row
        elif key:
            seen_batch_keys[key]=row_number
        counts[result['status']]+=1
        validated.append((row_number,raw,result))
    con.execute('''INSERT INTO import_batches(batch_id,source_file,dataset_type,status,rows_read,rows_valid,rows_warning,rows_duplicate,rows_rejected,created_by,message)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(
        batch_id,source_file,dataset_type,'Validated',len(validated),counts['Valid'],counts['Warning'],counts['Duplicate'],counts['Rejected'],user_id,
        'Validation completed. Database belum berubah.'
    ))
    for row_number,raw,result in validated:
        con.execute('''INSERT INTO import_batch_rows(batch_id,row_number,row_status,proposed_action,target_key,row_data_json,normalized_json,issues_json)
            VALUES (?,?,?,?,?,?,?,?)''',(
            batch_id,row_number,result['status'],result['action'],result['target_key'],json.dumps(raw,ensure_ascii=False),json.dumps(result['normalized'],ensure_ascii=False),json.dumps(result['issues'],ensure_ascii=False)
        ))
    con.commit()
    return batch_summary(con,batch_id,include_rows=True)


def batch_summary(con: sqlite3.Connection, batch_id: str, include_rows: bool = False, limit: int = 500) -> dict[str, Any]:
    con.row_factory=sqlite3.Row
    b=con.execute('SELECT * FROM import_batches WHERE batch_id=?',(batch_id,)).fetchone()
    if not b:
        raise KeyError('Import batch tidak ditemukan.')
    result=dict(b)
    if include_rows:
        rows=con.execute('SELECT * FROM import_batch_rows WHERE batch_id=? ORDER BY row_number LIMIT ?',(batch_id,limit)).fetchall()
        out=[]
        for r in rows:
            d=dict(r)
            d['row_data']=json.loads(d.pop('row_data_json') or '{}')
            d['normalized']=json.loads(d.pop('normalized_json') or '{}')
            d['issues']=json.loads(d.pop('issues_json') or '[]')
            out.append(d)
        result['rows']=out
    return result


def _mark_employee(con: sqlite3.Connection, employee_pk: int, user_id: str) -> None:
    row=con.execute('SELECT assessment_state FROM employees WHERE employee_pk=?',(employee_pk,)).fetchone()
    if row and (row[0] or 'Assessed')=='Assessed':
        con.execute("UPDATE employees SET assessment_state='Pending Reassessment',updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE employee_pk=?",(user_id,employee_pk))


def _mark_position(con: sqlite3.Connection, position_id: int, user_id: str) -> None:
    con.execute("""UPDATE employees SET assessment_state='Pending Reassessment',updated_by=?,updated_at=CURRENT_TIMESTAMP
        WHERE active_flag=1 AND current_position_id=? AND COALESCE(assessment_state,'Assessed')='Assessed'""",(user_id,position_id))


def _auto_cert_status(expiry: str | None, requested: str) -> str:
    if requested and requested != 'Auto':
        return requested
    if not expiry:
        return 'Active'
    d=datetime.strptime(expiry,'%Y-%m-%d').date(); now=date.today()
    if d < now: return 'Expired'
    if d <= now + timedelta(days=90): return 'Near Expiry'
    return 'Active'


def _commit_employee(con: sqlite3.Connection, n: dict, update: bool, user_id: str, source_file: str) -> tuple[str,int | None]:
    pos_id=int(n['_position_id'])
    active = 0 if n['employment_status'] in {'Resigned','Contract End','Retired'} else 1
    if update and n.get('_existing_employee_pk'):
        emp=int(n['_existing_employee_pk'])
        old_pos=n.get('_old_position_id')
        state=con.execute('SELECT assessment_state FROM employees WHERE employee_pk=?',(emp,)).fetchone()[0]
        con.execute('''UPDATE employees SET employee_code=COALESCE(NULLIF(?,''),employee_code),employee_name=?,department=?,current_position_id=?,join_date=?,employment_status=?,active_flag=?,remarks=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE employee_pk=?''',
                    (n['employee_id'],n['employee_name'],n['department'] or None,pos_id,n['join_date'],n['employment_status'],active,n['remarks'] or None,user_id,emp))
        if old_pos != pos_id:
            today_s=date.today().isoformat()
            con.execute("UPDATE employee_position_history SET effective_end=?,is_current=0 WHERE employee_pk=? AND (effective_end IS NULL OR is_current=1)",(today_s,emp))
            con.execute('''INSERT INTO employee_position_history(employee_pk,position_id,effective_start,is_current,change_type,change_reason,remarks,changed_by,source_note)
                VALUES (?,?,?,1,'Bulk Position Change','Bulk Import',?,?,?)''',(emp,pos_id,today_s,'Position updated by safe import',user_id,source_file))
            if state=='Assessed':
                con.execute("UPDATE employees SET assessment_state='Pending Reassessment' WHERE employee_pk=?",(emp,))
            elif state in {'Pending Initial Assessment','Assessment Ready'}:
                con.execute("UPDATE employees SET assessment_state='Pending Initial Assessment',initial_data_confirmed=0,initial_data_confirmed_at=NULL,initial_data_confirmed_by=NULL WHERE employee_pk=?",(emp,))
        else:
            _mark_employee(con,emp,user_id)
        return 'updated',emp
    cur=con.execute('''INSERT INTO employees(employee_code,employee_name,current_position_id,active_flag,department,join_date,employment_status,remarks,assessment_state,initial_data_confirmed,record_status,created_by,updated_by)
        VALUES (?,?,?,?,?,?,?,?, 'Pending Initial Assessment',0,'Active',?,?)''',
        (n['employee_id'] or None,n['employee_name'],pos_id,active,n['department'] or None,n['join_date'],n['employment_status'],n['remarks'] or None,user_id,user_id))
    emp=int(cur.lastrowid)
    con.execute('''INSERT INTO employee_position_history(employee_pk,position_id,effective_start,is_current,change_type,change_reason,remarks,changed_by,source_note)
        VALUES (?,?,?,1,'New Employee','Bulk Import',?,?,?)''',(emp,pos_id,n['join_date'] or date.today().isoformat(),'Initial position from bulk import',user_id,source_file))
    return 'inserted',emp


def _commit_training_history(con: sqlite3.Connection,n:dict,update:bool,user_id:str,source_file:str)->tuple[str,int]:
    emp=int(n['_employee_pk']); tid=int(n['_training_id']); status=n['result_status']
    if update and n.get('_existing_history_id'):
        hid=int(n['_existing_history_id'])
        con.execute('''UPDATE training_history SET training_date=?,completion_date=?,completion_year=?,completion_status=?,result_status=?,provider=?,valid_until=?,certificate_reference=?,remarks=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE history_id=?''',
                    (n['training_date'],n['completion_date'],int(n['completion_date'][:4]) if n['completion_date'] else None,status,status,n['provider'] or None,n['valid_until'],n['certificate_reference'] or None,n['remarks'] or None,user_id,hid))
        _mark_employee(con,emp,user_id); return 'updated',hid
    cur=con.execute('''INSERT INTO training_history(employee_pk,training_id,training_pk,training_name_raw,training_name,training_date,completion_date,completion_year,completion_status,result_status,provider,valid_until,certificate_reference,source_file,record_status,remarks,created_by,updated_by,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'Active',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
        (emp,tid,tid,n['training_name'],n['training_name'],n['training_date'],n['completion_date'],int(n['completion_date'][:4]) if n['completion_date'] else None,status,status,n['provider'] or None,n['valid_until'],n['certificate_reference'] or None,source_file,n['remarks'] or None,user_id,user_id))
    _mark_employee(con,emp,user_id); return 'inserted',int(cur.lastrowid)


def _commit_certification(con:sqlite3.Connection,n:dict,update:bool,user_id:str,source_file:str)->tuple[str,int]:
    emp=int(n['_employee_pk']); cat=n.get('_certification_catalog_id')
    if not cat:
        cur=con.execute('INSERT INTO certification_catalog(certification_name,issuer_type,active_flag) VALUES (?,?,1)',(n['certification_name'],n['issuer'] or None)); cat=int(cur.lastrowid)
    status=_auto_cert_status(n['expiry_date'],n['certification_status'] or 'Auto')
    renewal=n['renewal_status'] or ('Renewal Review Required' if status in {'Near Expiry','Expired'} else 'Not Due')
    if update and n.get('_existing_certification_id'):
        cid=int(n['_existing_certification_id'])
        con.execute('''UPDATE certifications SET employee_pk=?,certification_catalog_id=?,employee_code_raw=?,employee_name_raw=(SELECT employee_name FROM employees WHERE employee_pk=?),certification_name_raw=?,certificate_number=?,issuer=?,issuer_type=?,issue_date=?,certification_date=?,expiry_date=?,expired_date=?,certification_status=?,status=?,renewal_status=?,remarks=?,source_file=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE certification_id=?''',
                    (emp,cat,n['employee_id'],emp,n['certification_name'],n['certificate_number'] or None,n['issuer'] or None,n['issuer'] or None,n['issue_date'],n['issue_date'],n['expiry_date'],n['expiry_date'],status,status,renewal,n['remarks'] or None,source_file,user_id,cid))
        _mark_employee(con,emp,user_id); return 'updated',cid
    emp_name=con.execute('SELECT employee_name FROM employees WHERE employee_pk=?',(emp,)).fetchone()[0]
    cur=con.execute('''INSERT INTO certifications(employee_pk,certification_catalog_id,employee_code_raw,employee_name_raw,certification_name_raw,issuer_type,certification_date,expired_date,status,source_file,certificate_number,issuer,issue_date,expiry_date,certification_status,renewal_status,remarks,record_status,created_by,updated_by,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Active',?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
        (emp,cat,n['employee_id'],emp_name,n['certification_name'],n['issuer'] or None,n['issue_date'],n['expiry_date'],status,source_file,n['certificate_number'] or None,n['issuer'] or None,n['issue_date'],n['expiry_date'],status,renewal,n['remarks'] or None,user_id,user_id))
    _mark_employee(con,emp,user_id); return 'inserted',int(cur.lastrowid)


def _commit_requirement(con:sqlite3.Connection,n:dict,update:bool,user_id:str,source_file:str)->tuple[str,int]:
    pid=int(n['_position_id']); tid=int(n['_training_id']); status=n['requirement_status'] or 'Active'
    if update and n.get('_existing_requirement_id'):
        rid=int(n['_existing_requirement_id'])
        con.execute('''UPDATE training_requirements SET requirement_type=?,regulatory_flag=?,requirement_source=?,effective_from=?,effective_to=?,requirement_status=?,active_flag=?,remarks=?,source_file=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE requirement_id=?''',
                    (n['requirement_type'],n['regulatory_flag'] or None,n['requirement_source'] or 'Bulk Import',n['effective_from'],n['effective_to'],status,1 if status=='Active' else 0,n['remarks'] or None,source_file,user_id,rid))
        _mark_position(con,pid,user_id); return 'updated',rid
    cur=con.execute('''INSERT INTO training_requirements(training_id,position_id,position_raw,position_key,requirement_type,regulatory_flag,effective_year,source_file,active_flag,effective_from,effective_to,requirement_status,requirement_source,remarks,updated_by,updated_at)
        VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)''',
        (tid,pid,n['position'],n['position'].lower(),n['requirement_type'],n['regulatory_flag'] or None,int(n['effective_from'][:4]) if n['effective_from'] else None,source_file,1 if status=='Active' else 0,n['effective_from'],n['effective_to'],status,n['requirement_source'] or 'Bulk Import',n['remarks'] or None,user_id))
    _mark_position(con,pid,user_id); return 'inserted',int(cur.lastrowid)


def confirm_batch(con:sqlite3.Connection,batch_id:str,user_id:str,accept_warnings:bool=True,duplicate_action:str='skip')->dict[str,Any]:
    con.row_factory=sqlite3.Row
    batch=con.execute('SELECT * FROM import_batches WHERE batch_id=?',(batch_id,)).fetchone()
    if not batch: raise KeyError('Import batch tidak ditemukan.')
    if batch['status'] not in {'Validated','Failed'}: raise ValueError('Batch sudah pernah dikonfirmasi atau tidak dapat diproses lagi.')
    rows=con.execute('SELECT * FROM import_batch_rows WHERE batch_id=? ORDER BY row_number',(batch_id,)).fetchall()
    imported=skipped=0; affected=set(); source_file=batch['source_file']; dataset=batch['dataset_type']
    try:
        con.execute('BEGIN')
        for r in rows:
            status=r['row_status'];
            if status=='Rejected': skipped+=1; continue
            if status=='Warning' and not accept_warnings: skipped+=1; continue
            proposed_action=str(r['proposed_action'] or '')
            if proposed_action=='skip_batch_duplicate': skipped+=1; continue
            if status=='Duplicate' and duplicate_action=='skip': skipped+=1; continue
            n=json.loads(r['normalized_json'] or '{}')
            update = status=='Duplicate' and duplicate_action=='update'
            if dataset=='Employee Master': result,target=_commit_employee(con,n,update,user_id,source_file); affected.add(target)
            elif dataset=='Training History': result,target=_commit_training_history(con,n,update,user_id,source_file); affected.add(int(n['_employee_pk']))
            elif dataset=='Certification': result,target=_commit_certification(con,n,update,user_id,source_file); affected.add(int(n['_employee_pk']))
            elif dataset=='Training Requirement':
                result,target=_commit_requirement(con,n,update,user_id,source_file)
                affected.update(x[0] for x in con.execute('SELECT employee_pk FROM employees WHERE current_position_id=? AND active_flag=1',(int(n['_position_id']),)).fetchall())
            else: raise ValueError('Dataset type tidak didukung.')
            con.execute('UPDATE import_batch_rows SET imported_target_id=? WHERE row_id=?',(str(target),r['row_id']))
            imported+=1
        con.execute('''UPDATE import_batches SET status='Confirmed',rows_imported=?,rows_skipped=?,affected_employees=?,confirmed_by=?,confirmed_at=CURRENT_TIMESTAMP,message=? WHERE batch_id=?''',
                    (imported,skipped,len(affected),user_id,f'Import confirmed. imported={imported}; skipped={skipped}; duplicate_action={duplicate_action}',batch_id))
        notes=f'status=SUCCESS; imported={imported}; rejected={batch["rows_rejected"]}; skipped={skipped}; batch_id={batch_id}'
        con.execute('INSERT INTO data_import_log(source_name,source_type,record_count,imported_at,notes) VALUES (?,?,?,CURRENT_TIMESTAMP,?)',(source_file,dataset,batch['rows_read'],notes))
        con.commit()
    except Exception as exc:
        con.rollback()
        con.execute("UPDATE import_batches SET status='Failed',message=? WHERE batch_id=?",(str(exc),batch_id)); con.commit()
        raise
    result=batch_summary(con,batch_id,include_rows=False)
    result['affected_employee_ids']=sorted(int(x) for x in affected if x)
    return result


def recent_batches(con:sqlite3.Connection,limit:int=50)->list[dict[str,Any]]:
    con.row_factory=sqlite3.Row
    rows=con.execute('SELECT * FROM import_batches ORDER BY created_at DESC LIMIT ?',(min(max(limit,1),200),)).fetchall()
    return [dict(r) for r in rows]
