"""脱敏引擎"""
import re
from typing import List, Tuple, Dict
from .database import get_connection
from .models import DesensitizationRule


def get_active_rules(version: int = None) -> List[DesensitizationRule]:
    conn = get_connection()
    cursor = conn.cursor()
    if version:
        cursor.execute('''
            SELECT id, name, category, pattern, replacement, version, created_at, description
            FROM desensitization_rules
            WHERE version = ?
            ORDER BY id
        ''', (version,))
    else:
        cursor.execute('''
            SELECT id, name, category, pattern, replacement, version, created_at, description
            FROM desensitization_rules
            WHERE is_active = 1
            ORDER BY id
        ''')
    rows = cursor.fetchall()
    conn.close()
    return [DesensitizationRule.from_row(row) for row in rows]


def get_current_version() -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1
    ''')
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 1


def desensitize_text(text: str, rules: List[DesensitizationRule]) -> Tuple[str, Dict]:
    result = text
    applied_rules = []
    for rule in rules:
        try:
            pattern = re.compile(rule.pattern)
            matches = pattern.findall(result)
            if matches:
                result = pattern.sub(rule.replacement, result)
                applied_rules.append({
                    'rule_id': rule.id,
                    'rule_name': rule.name,
                    'category': rule.category,
                    'match_count': len(matches),
                })
        except re.error:
            continue
    metadata = {
        'applied_rules': applied_rules,
        'rule_version': rules[0].version if rules else 0,
    }
    return result, metadata


def batch_desensitize(corpus_ids: List[int] = None, operator: str = "system") -> int:
    from .audit import log_operation
    version = get_current_version()
    rules = get_active_rules()

    conn = get_connection()
    cursor = conn.cursor()

    if corpus_ids:
        placeholders = ','.join(['?'] * len(corpus_ids))
        cursor.execute(f'''
            SELECT id, original_text FROM corpus WHERE id IN ({placeholders})
        ''', corpus_ids)
    else:
        cursor.execute('''
            SELECT id, original_text FROM corpus WHERE status IN ('imported', 'needs_review')
        ''')

    rows = cursor.fetchall()
    count = 0

    for row in rows:
        corpus_id, original_text = row
        desensitized_text, metadata = desensitize_text(original_text, rules)
        cursor.execute('''
            UPDATE corpus
            SET desensitized_text = ?, rule_version = ?, status = 'desensitized',
                updated_at = datetime('now'), metadata = ?
            WHERE id = ?
        ''', (desensitized_text, version, str(metadata).replace("'", '"'), corpus_id))
        count += 1

    conn.commit()
    conn.close()

    log_operation(
        operation='desensitize',
        operator=operator,
        details=f'完成 {count} 条语料脱敏，使用规则版本 v{version}',
        rule_version=version,
    )
    return count
