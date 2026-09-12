from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_BACKEND = os.getenv('COMPETENCY_DB_BACKEND', 'sqlite').lower()
DATABASE_URL = os.getenv('COMPETENCY_DATABASE_URL', '') or os.getenv('DATABASE_URL', '')
DB_PATH = ROOT / 'data' / 'competency_dss.db'
BUILD = "v22.10.5"

SESSION_COOKIE_NAME = os.getenv('COMPETENCY_SESSION_COOKIE', 'competency_session')
SESSION_COOKIE_SECURE = os.getenv('COMPETENCY_COOKIE_SECURE', '0').strip().lower() in {'1','true','yes','on'}
SESSION_IDLE_MINUTES = int(os.getenv('COMPETENCY_SESSION_IDLE_MINUTES', '60'))
SESSION_ABSOLUTE_HOURS = int(os.getenv('COMPETENCY_SESSION_ABSOLUTE_HOURS', '12'))

ROLE_LABELS = {
    'supervisor_tcd': 'Supervisor TCD',
    'hrd': 'HRD',
}

ROLE_PERMISSIONS = {
    'supervisor_tcd': {
        'read','export','audit_view','employee_manage','training_history_manage','certification_manage',
        'requirement_manage','assessment_manage','validation_manage','override_manage','action_manage',
        'evidence_manage','import_manage','refresh','backup_manage','user_manage','system_write'
    },
    'hrd': {
        'read','export','audit_view','employee_manage','training_history_manage','certification_manage',
        'evidence_manage','import_manage','refresh'
    },
}
