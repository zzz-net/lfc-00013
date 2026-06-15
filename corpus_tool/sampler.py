"""抽检与复核模块"""
import random
from typing import List, Tuple, Dict
from datetime import datetime
from .database import get_connection
from .models import ReviewRecord, ConflictRecord, Corpus
from .audit import log_operation


def sample_corpus(ratio: float = 0.1, count: int = None, batch_name: str = None,
                  operator: str = "system") -> Tuple[int, str]:
    conn = get_connection()
    cursor = conn.cursor()

    version = _get_active_version(cursor)

    if count:
        cursor.execute('''
            SELECT id FROM corpus
            WHERE status = 'desensitized' AND is_sampled = 0
            ORDER BY RANDOM() LIMIT ?
        ''', (count,))
    else:
        cursor.execute('''
            SELECT COUNT(*) FROM corpus WHERE status = 'desensitized' AND is_sampled = 0
        ''')
        total = cursor.fetchone()[0]
        sample_count = max(1, int(total * ratio))
        cursor.execute('''
            SELECT id FROM corpus
            WHERE status = 'desensitized' AND is_sampled = 0
            ORDER BY RANDOM() LIMIT ?
        ''', (sample_count,))

    rows = cursor.fetchall()
    if not rows:
        conn.close()
        return 0, ""

    sample_ids = [row[0] for row in rows]
    batch = batch_name or f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    placeholders = ','.join(['?'] * len(sample_ids))
    cursor.execute(f'''
        UPDATE corpus
        SET is_sampled = 1, sample_batch = ?, status = 'pending_review',
            updated_at = datetime('now')
        WHERE id IN ({placeholders})
    ''', [batch] + sample_ids)

    cursor.execute('''
        INSERT INTO sample_batches (batch_name, sample_count, created_at, rule_version)
        VALUES (?, ?, datetime('now'), ?)
    ''', (batch, len(sample_ids), version))

    conn.commit()
    conn.close()

    log_operation(
        operation='sample',
        operator=operator,
        details=f'抽检批次 [{batch}]，抽取 {len(sample_ids)} 条语料进入复核队列',
        rule_version=version,
    )

    return len(sample_ids), batch


def list_pending_reviews(batch_name: str = None) -> List[Corpus]:
    conn = get_connection()
    cursor = conn.cursor()
    if batch_name:
        cursor.execute('''
            SELECT c.id, c.original_text, c.desensitized_text, c.source_file, c.status,
                   c.rule_version, c.created_at, c.updated_at, c.is_sampled, c.sample_batch,
                   c.final_conclusion, c.metadata
            FROM corpus c
            WHERE c.is_sampled = 1 AND c.final_conclusion IS NULL AND c.sample_batch = ?
            ORDER BY c.id
        ''', (batch_name,))
    else:
        cursor.execute('''
            SELECT c.id, c.original_text, c.desensitized_text, c.source_file, c.status,
                   c.rule_version, c.created_at, c.updated_at, c.is_sampled, c.sample_batch,
                   c.final_conclusion, c.metadata
            FROM corpus c
            WHERE c.is_sampled = 1 AND c.final_conclusion IS NULL
            ORDER BY c.sample_batch, c.id
        ''')
    rows = cursor.fetchall()
    conn.close()
    return [Corpus.from_row(row) for row in rows]


def submit_review(corpus_id: int, reviewer: str, conclusion: str, comment: str = "",
                  operator: str = None) -> Dict:
    if conclusion not in ['approved', 'rejected']:
        raise ValueError("结论必须是 'approved' 或 'rejected'")

    conn = get_connection()
    cursor = conn.cursor()

    version = _get_active_version(cursor)

    cursor.execute('''
        SELECT id, final_conclusion FROM corpus WHERE id = ? AND is_sampled = 1
    ''', (corpus_id,))
    corpus = cursor.fetchone()
    if not corpus:
        conn.close()
        raise ValueError(f"抽检语料ID不存在: {corpus_id}")

    if corpus[1] is not None:
        conn.close()
        raise ValueError(f"语料 {corpus_id} 已有最终结论，不可重复复核")

    cursor.execute('''
        SELECT id, reviewer, conclusion FROM review_records
        WHERE corpus_id = ? ORDER BY id
    ''', (corpus_id,))
    existing_reviews = cursor.fetchall()

    for r in existing_reviews:
        if r[1] == reviewer:
            conn.close()
            raise ValueError(f"复核人 [{reviewer}] 已提交过该语料的复核意见")

    cursor.execute('''
        INSERT INTO review_records
        (corpus_id, reviewer, conclusion, comment, created_at, rule_version_at_review)
        VALUES (?, ?, ?, ?, datetime('now'), ?)
    ''', (corpus_id, reviewer, conclusion, comment, version))

    result = {
        'status': 'single_review',
        'review_count': len(existing_reviews) + 1,
        'conflict': False,
        'finalized': False,
    }

    log_op = None
    if len(existing_reviews) == 0:
        cursor.execute('''
            UPDATE corpus
            SET status = 'single_review', updated_at = datetime('now')
            WHERE id = ?
        ''', (corpus_id,))
        result['status'] = 'single_review'
        log_op = ('review_submit', operator or reviewer,
                  f'语料 {corpus_id} 提交复核，结论: {conclusion}',
                  version)
    elif len(existing_reviews) == 1:
        first_review = existing_reviews[0]
        if first_review[2] != conclusion:
            cursor.execute('''
                INSERT INTO conflict_records
                (corpus_id, reviewer1, reviewer2, conclusion1, conclusion2, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            ''', (corpus_id, first_review[1], reviewer, first_review[2], conclusion))
            cursor.execute('''
                UPDATE corpus
                SET status = 'conflict', updated_at = datetime('now')
                WHERE id = ?
            ''', (corpus_id,))
            result['status'] = 'conflict'
            result['conflict'] = True
            result['conflict_with'] = first_review[1]
            log_op = ('conflict', operator or reviewer,
                      f'语料 {corpus_id} 复核冲突：{first_review[1]}({first_review[2]}) vs {reviewer}({conclusion})',
                      version)
        else:
            cursor.execute('''
                UPDATE corpus
                SET final_conclusion = ?, status = 'reviewed', updated_at = datetime('now')
                WHERE id = ?
            ''', (conclusion, corpus_id))
            result['status'] = 'finalized'
            result['finalized'] = True
            result['final_conclusion'] = conclusion
            log_op = ('review_finalize', operator or reviewer,
                      f'语料 {corpus_id} 复核完成，结论: {conclusion}',
                      version)

    conn.commit()
    conn.close()

    if log_op:
        log_operation(
            operation=log_op[0],
            operator=log_op[1],
            details=log_op[2],
            rule_version=log_op[3],
        )

    return result


def resolve_conflict(corpus_id: int, final_conclusion: str, operator: str = "admin") -> Dict:
    if final_conclusion not in ['approved', 'rejected']:
        raise ValueError("结论必须是 'approved' 或 'rejected'")

    conn = get_connection()
    cursor = conn.cursor()

    version = _get_active_version(cursor)

    cursor.execute('''
        SELECT id FROM conflict_records
        WHERE corpus_id = ? AND resolved = 0
        ORDER BY id DESC LIMIT 1
    ''', (corpus_id,))
    conflict = cursor.fetchone()
    if not conflict:
        conn.close()
        raise ValueError(f"语料 {corpus_id} 没有未解决的冲突")

    cursor.execute('''
        UPDATE conflict_records SET resolved = 1 WHERE id = ?
    ''', (conflict[0],))

    cursor.execute('''
        UPDATE corpus
        SET final_conclusion = ?, status = 'reviewed', updated_at = datetime('now')
        WHERE id = ?
    ''', (final_conclusion, corpus_id))

    conn.commit()
    conn.close()

    log_operation(
        operation='conflict_resolve',
        operator=operator,
        details=f'管理员仲裁语料 {corpus_id}，最终结论: {final_conclusion}',
        rule_version=version,
    )

    return {
        'corpus_id': corpus_id,
        'final_conclusion': final_conclusion,
        'resolved_by': operator,
    }


def get_review_records(corpus_id: int) -> List[ReviewRecord]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, corpus_id, reviewer, conclusion, comment, created_at, rule_version_at_review
        FROM review_records WHERE corpus_id = ? ORDER BY id
    ''', (corpus_id,))
    rows = cursor.fetchall()
    conn.close()
    return [ReviewRecord.from_row(row) for row in rows]


def get_conflicts(resolved: bool = False) -> List[ConflictRecord]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, corpus_id, reviewer1, reviewer2, conclusion1, conclusion2, resolved, created_at
        FROM conflict_records WHERE resolved = ? ORDER BY id DESC
    ''', (1 if resolved else 0,))
    rows = cursor.fetchall()
    conn.close()
    return [ConflictRecord.from_row(row) for row in rows]


def list_batches() -> List[Dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT b.batch_name, b.sample_count, b.created_at, b.rule_version,
               COUNT(CASE WHEN c.final_conclusion IS NOT NULL THEN 1 END) as reviewed_count
        FROM sample_batches b
        LEFT JOIN corpus c ON b.batch_name = c.sample_batch
        GROUP BY b.batch_name
        ORDER BY b.created_at DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            'batch_name': row[0],
            'sample_count': row[1],
            'created_at': row[2],
            'rule_version': row[3],
            'reviewed_count': row[4],
            'pending_count': row[1] - row[4],
        }
        for row in rows
    ]


def _get_active_version(cursor) -> int:
    cursor.execute('''
        SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1
    ''')
    row = cursor.fetchone()
    return row[0] if row else 1
