"""规则版本管理"""
from typing import List, Dict
from .database import get_connection
from .models import DesensitizationRule
from .audit import log_operation
from datetime import datetime


def list_rules(active_only: bool = True) -> List[DesensitizationRule]:
    conn = get_connection()
    cursor = conn.cursor()
    if active_only:
        cursor.execute('''
            SELECT id, name, category, pattern, replacement, version, created_at, description
            FROM desensitization_rules WHERE is_active = 1 ORDER BY version DESC, id
        ''')
    else:
        cursor.execute('''
            SELECT id, name, category, pattern, replacement, version, created_at, description
            FROM desensitization_rules ORDER BY version DESC, id
        ''')
    rows = cursor.fetchall()
    conn.close()
    return [DesensitizationRule.from_row(row) for row in rows]


def add_rule(name: str, category: str, pattern: str, replacement: str,
             description: str = "", operator: str = "system") -> int:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT MAX(version) FROM rule_versions
    ''')
    current_version = cursor.fetchone()[0] or 0
    new_version = current_version + 1

    cursor.execute('''
        UPDATE rule_versions SET is_active = 0
    ''')
    cursor.execute('''
        UPDATE desensitization_rules SET is_active = 0
    ''')

    cursor.execute('''
        SELECT id, name, category, pattern, replacement, description
        FROM desensitization_rules WHERE version = ?
    ''', (current_version,))
    old_rules = cursor.fetchall()

    for rule in old_rules:
        cursor.execute('''
            INSERT INTO desensitization_rules
            (name, category, pattern, replacement, version, is_active, created_at, description)
            VALUES (?, ?, ?, ?, ?, 1, datetime('now'), ?)
        ''', (rule[1], rule[2], rule[3], rule[4], new_version, rule[5]))

    cursor.execute('''
        INSERT INTO desensitization_rules
        (name, category, pattern, replacement, version, is_active, created_at, description)
        VALUES (?, ?, ?, ?, ?, 1, datetime('now'), ?)
    ''', (name, category, pattern, replacement, new_version, description))
    new_rule_id = cursor.lastrowid

    cursor.execute('''
        INSERT INTO rule_versions (version, description, created_at, is_active)
        VALUES (?, ?, datetime('now'), 1)
    ''', (new_version, f'新增规则: {name}'))

    conn.commit()
    conn.close()

    _mark_approved_as_needs_review(new_version, operator)

    log_operation(
        operation='rule_add',
        operator=operator,
        details=f'新增规则 [{name}]，规则版本升级到 v{new_version}',
        rule_version=new_version,
    )

    return new_rule_id


def update_rule(rule_id: int, pattern: str = None, replacement: str = None,
                name: str = None, description: str = None, operator: str = "system") -> int:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, name, category, pattern, replacement, version, description
        FROM desensitization_rules WHERE id = ?
    ''', (rule_id,))
    rule = cursor.fetchone()
    if not rule:
        conn.close()
        raise ValueError(f"规则ID不存在: {rule_id}")

    current_version = rule[5]

    cursor.execute('''
        SELECT MAX(version) FROM rule_versions
    ''')
    max_version = cursor.fetchone()[0] or 0
    new_version = max_version + 1

    cursor.execute('''
        UPDATE rule_versions SET is_active = 0
    ''')
    cursor.execute('''
        UPDATE desensitization_rules SET is_active = 0
    ''')

    cursor.execute('''
        SELECT id, name, category, pattern, replacement, description
        FROM desensitization_rules WHERE version = ? AND id != ?
    ''', (current_version, rule_id))
    other_rules = cursor.fetchall()

    for r in other_rules:
        cursor.execute('''
            INSERT INTO desensitization_rules
            (name, category, pattern, replacement, version, is_active, created_at, description)
            VALUES (?, ?, ?, ?, ?, 1, datetime('now'), ?)
        ''', (r[1], r[2], r[3], r[4], new_version, r[5]))

    new_name = name if name else rule[1]
    new_pattern = pattern if pattern else rule[3]
    new_replacement = replacement if replacement else rule[4]
    new_desc = description if description else rule[6]

    cursor.execute('''
        INSERT INTO desensitization_rules
        (name, category, pattern, replacement, version, is_active, created_at, description)
        VALUES (?, ?, ?, ?, ?, 1, datetime('now'), ?)
    ''', (new_name, rule[2], new_pattern, new_replacement, new_version, new_desc))
    new_rule_id = cursor.lastrowid

    cursor.execute('''
        INSERT INTO rule_versions (version, description, created_at, is_active)
        VALUES (?, ?, datetime('now'), 1)
    ''', (new_version, f'更新规则: {new_name}'))

    conn.commit()
    conn.close()

    _mark_approved_as_needs_review(new_version, operator)

    log_operation(
        operation='rule_update',
        operator=operator,
        details=f'更新规则 [{new_name}]，规则版本升级到 v{new_version}',
        rule_version=new_version,
    )

    return new_rule_id


def delete_rule(rule_id: int, operator: str = "system") -> int:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, name, category, pattern, replacement, version, description
        FROM desensitization_rules WHERE id = ?
    ''', (rule_id,))
    rule = cursor.fetchone()
    if not rule:
        conn.close()
        raise ValueError(f"规则ID不存在: {rule_id}")

    current_version = rule[5]
    rule_name = rule[1]

    cursor.execute('''
        SELECT MAX(version) FROM rule_versions
    ''')
    max_version = cursor.fetchone()[0] or 0
    new_version = max_version + 1

    cursor.execute('''
        UPDATE rule_versions SET is_active = 0
    ''')
    cursor.execute('''
        UPDATE desensitization_rules SET is_active = 0
    ''')

    cursor.execute('''
        SELECT id, name, category, pattern, replacement, description
        FROM desensitization_rules WHERE version = ? AND id != ?
    ''', (current_version, rule_id))
    other_rules = cursor.fetchall()

    for r in other_rules:
        cursor.execute('''
            INSERT INTO desensitization_rules
            (name, category, pattern, replacement, version, is_active, created_at, description)
            VALUES (?, ?, ?, ?, ?, 1, datetime('now'), ?)
        ''', (r[1], r[2], r[3], r[4], new_version, r[5]))

    cursor.execute('''
        INSERT INTO rule_versions (version, description, created_at, is_active)
        VALUES (?, ?, datetime('now'), 1)
    ''', (new_version, f'删除规则: {rule_name}'))

    conn.commit()
    conn.close()

    _mark_approved_as_needs_review(new_version, operator)

    log_operation(
        operation='rule_delete',
        operator=operator,
        details=f'删除规则 [{rule_name}]，规则版本升级到 v{new_version}',
        rule_version=new_version,
    )

    return new_version


def rollback_to_version(target_version: int, operator: str = "system") -> int:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT COUNT(*) FROM rule_versions WHERE version = ?
    ''', (target_version,))
    if cursor.fetchone()[0] == 0:
        conn.close()
        raise ValueError(f"规则版本不存在: v{target_version}")

    cursor.execute('''
        UPDATE rule_versions SET is_active = 0
    ''')
    cursor.execute('''
        UPDATE desensitization_rules SET is_active = 0
    ''')

    cursor.execute('''
        UPDATE rule_versions SET is_active = 1 WHERE version = ?
    ''', (target_version,))

    cursor.execute('''
        UPDATE desensitization_rules SET is_active = 1 WHERE version = ?
    ''', (target_version,))

    conn.commit()
    conn.close()

    log_operation(
        operation='rollback',
        operator=operator,
        details=f'回滚规则版本到 v{target_version}',
        rule_version=target_version,
    )

    return target_version


def list_versions() -> List[Dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT v.version, v.description, v.created_at, v.is_active,
               COUNT(r.id) as rule_count
        FROM rule_versions v
        LEFT JOIN desensitization_rules r ON v.version = r.version AND r.is_active = 1
        GROUP BY v.version
        ORDER BY v.version DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            'version': row[0],
            'description': row[1],
            'created_at': row[2],
            'is_active': bool(row[3]),
            'rule_count': row[4],
        }
        for row in rows
    ]


def _mark_approved_as_needs_review(new_version: int, operator: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id FROM corpus WHERE is_sampled = 1 AND final_conclusion = 'approved'
    ''')
    corpus_ids = [row[0] for row in cursor.fetchall()]

    if corpus_ids:
        placeholders = ','.join(['?'] * len(corpus_ids))
        cursor.execute(f'''
            DELETE FROM review_records WHERE corpus_id IN ({placeholders})
        ''', corpus_ids)
        cursor.execute(f'''
            UPDATE corpus
            SET status = 'needs_review',
                final_conclusion = NULL,
                updated_at = datetime('now')
            WHERE id IN ({placeholders})
        ''', corpus_ids)

    updated_count = len(corpus_ids)
    conn.commit()
    conn.close()

    if updated_count > 0:
        log_operation(
            operation='status_change',
            operator=operator,
            details=f'规则变更触发 {updated_count} 条已通过样本标记为待复核',
            rule_version=new_version,
        )
