"""数据导入模块"""
import os
import csv
from typing import List
from .database import get_connection
from .audit import log_operation
from .models import Corpus


def import_txt_file(file_path: str, operator: str = "system") -> List[int]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    source_file = os.path.basename(file_path)
    imported_ids = []

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]

    conn = get_connection()
    cursor = conn.cursor()

    for line in lines:
        cursor.execute('''
            INSERT INTO corpus (original_text, source_file, status, created_at, updated_at)
            VALUES (?, ?, 'imported', datetime('now'), datetime('now'))
        ''', (line, source_file))
        imported_ids.append(cursor.lastrowid)

    conn.commit()
    conn.close()

    log_operation(
        operation='import',
        operator=operator,
        details=f'从 {source_file} 导入 {len(imported_ids)} 条语料',
        rule_version=0,
    )

    return imported_ids


def import_csv_file(file_path: str, text_column: str = "text", operator: str = "system") -> List[int]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    source_file = os.path.basename(file_path)
    imported_ids = []

    with open(file_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if text_column not in reader.fieldnames:
            raise ValueError(f"CSV文件中找不到列: {text_column}")

        conn = get_connection()
        cursor = conn.cursor()

        for row in reader:
            text = row[text_column].strip()
            if text:
                cursor.execute('''
                    INSERT INTO corpus (original_text, source_file, status, created_at, updated_at)
                    VALUES (?, ?, 'imported', datetime('now'), datetime('now'))
                ''', (text, source_file))
                imported_ids.append(cursor.lastrowid)

        conn.commit()
        conn.close()

    log_operation(
        operation='import',
        operator=operator,
        details=f'从 {source_file} (CSV) 导入 {len(imported_ids)} 条语料',
        rule_version=0,
    )

    return imported_ids


def import_file(file_path: str, operator: str = "system") -> List[int]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.txt':
        return import_txt_file(file_path, operator)
    elif ext == '.csv':
        return import_csv_file(file_path, operator=operator)
    else:
        raise ValueError(f"不支持的文件格式: {ext}，仅支持 .txt 和 .csv")


def list_corpus(status: str = None, limit: int = 50) -> List[Corpus]:
    conn = get_connection()
    cursor = conn.cursor()
    if status:
        cursor.execute('''
            SELECT id, original_text, desensitized_text, source_file, status, rule_version,
                   created_at, updated_at, is_sampled, sample_batch, final_conclusion, metadata
            FROM corpus WHERE status = ? ORDER BY id DESC LIMIT ?
        ''', (status, limit))
    else:
        cursor.execute('''
            SELECT id, original_text, desensitized_text, source_file, status, rule_version,
                   created_at, updated_at, is_sampled, sample_batch, final_conclusion, metadata
            FROM corpus ORDER BY id DESC LIMIT ?
        ''', (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [Corpus.from_row(row) for row in rows]


def get_corpus(corpus_id: int) -> Corpus:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, original_text, desensitized_text, source_file, status, rule_version,
               created_at, updated_at, is_sampled, sample_batch, final_conclusion, metadata
        FROM corpus WHERE id = ?
    ''', (corpus_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"语料ID不存在: {corpus_id}")
    return Corpus.from_row(row)
