"""数据导出模块"""
import os
import csv
from typing import List
from .database import get_connection
from .audit import log_operation
from .models import Corpus


def check_export_ready() -> tuple:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) FROM corpus WHERE is_sampled = 1 AND final_conclusion IS NULL
    ''')
    pending_review = cursor.fetchone()[0]
    cursor.execute('''
        SELECT COUNT(*) FROM conflict_records WHERE resolved = 0
    ''')
    unresolved_conflicts = cursor.fetchone()[0]
    conn.close()
    return pending_review == 0 and unresolved_conflicts == 0, pending_review, unresolved_conflicts


def export_desensitized(output_path: str, include_original: bool = False, operator: str = "system") -> int:
    ready, pending, conflicts = check_export_ready()
    if not ready:
        raise ValueError(
            f"导出条件不满足：{pending} 条样本待复核，{conflicts} 个冲突未解决"
        )

    conn = get_connection()
    cursor = conn.cursor()

    version = _get_active_rule_version(cursor)

    cursor.execute('''
        SELECT id, original_text, desensitized_text, source_file, status, rule_version,
               created_at, updated_at, is_sampled, sample_batch, final_conclusion, metadata
        FROM corpus 
        WHERE status = 'desensitized' 
           OR (status = 'reviewed' AND final_conclusion = 'approved')
        ORDER BY id
    ''')
    rows = cursor.fetchall()

    ext = os.path.splitext(output_path)[1].lower()

    if ext == '.csv':
        with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            headers = ['id', 'desensitized_text', 'source_file', 'rule_version', 'final_conclusion']
            if include_original:
                headers.insert(1, 'original_text')
            writer.writerow(headers)
            for row in rows:
                corpus = Corpus.from_row(row)
                data = [corpus.id, corpus.desensitized_text, corpus.source_file,
                        corpus.rule_version, corpus.final_conclusion]
                if include_original:
                    data.insert(1, corpus.original_text)
                writer.writerow(data)
    elif ext == '.txt':
        with open(output_path, 'w', encoding='utf-8') as f:
            for row in rows:
                corpus = Corpus.from_row(row)
                f.write(corpus.desensitized_text + '\n')
    else:
        conn.close()
        raise ValueError(f"不支持的导出格式: {ext}")

    conn.close()

    log_operation(
        operation='export',
        operator=operator,
        details=f'导出 {len(rows)} 条脱敏语料到 {os.path.basename(output_path)}，规则版本 v{version}',
        rule_version=version,
    )

    return len(rows)


def _get_active_rule_version(cursor) -> int:
    cursor.execute('''
        SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1
    ''')
    row = cursor.fetchone()
    return row[0] if row else 1


def check_sensitive_leakage(file_path: str) -> List[str]:
    from .desensitizer import get_active_rules
    import re
    issues = []
    rules = get_active_rules()
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    for rule in rules:
        try:
            pattern = re.compile(rule.pattern)
            matches = pattern.findall(content)
            if matches:
                issues.append(f"规则 [{rule.name}] 发现 {len(matches)} 处潜在泄露")
        except re.error:
            continue
    return issues
