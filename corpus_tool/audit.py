"""审计追踪模块"""
from typing import List
from .database import get_connection
from .models import AuditLog
from datetime import datetime


def log_operation(operation: str, operator: str, details: str, rule_version: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO audit_logs (operation, operator, details, rule_version, created_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (operation, operator, details, rule_version, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_audit_logs(limit: int = 100, operation: str = None) -> List[AuditLog]:
    conn = get_connection()
    cursor = conn.cursor()
    if operation:
        cursor.execute('''
            SELECT id, operation, operator, details, rule_version, created_at
            FROM audit_logs WHERE operation = ? ORDER BY id DESC LIMIT ?
        ''', (operation, limit))
    else:
        cursor.execute('''
            SELECT id, operation, operator, details, rule_version, created_at
            FROM audit_logs ORDER BY id DESC LIMIT ?
        ''', (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [AuditLog.from_row(row) for row in rows]


def get_audit_logs_by_version(version: int) -> List[AuditLog]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, operation, operator, details, rule_version, created_at
        FROM audit_logs WHERE rule_version = ? ORDER BY id DESC
    ''', (version,))
    rows = cursor.fetchall()
    conn.close()
    return [AuditLog.from_row(row) for row in rows]
