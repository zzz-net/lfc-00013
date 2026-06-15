"""数据集快照与回滚模块

支持：
- 创建命名快照，保存完整语料状态
- 查看快照列表
- 导出/导入快照文件
- 按快照回滚（含冲突检测、影响预览、审计日志）

快照包含：
- 语料数据（状态、脱敏结果等）
- 复核记录
- 冲突记录
- 抽检批次信息
- 规则版本
- 当前导出配置引用
"""
import json
import os
import re
from typing import List, Dict, Tuple, Optional
from datetime import datetime

from .database import get_connection
from .models import Snapshot
from .audit import log_operation
from . import export_config as ec


SNAPSHOT_SCHEMA_VERSION = 1


def create_snapshot(name: str, description: str = "",
                    operator: str = "system") -> Snapshot:
    """创建快照

    保存当前所有语料状态、脱敏结果、复核记录、冲突记录、
    抽检批次、规则版本、当前导出配置引用。
    """
    if not _validate_snapshot_name(name):
        raise ValueError(f"快照名称不合法: {name}")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM snapshots WHERE name = ?', (name,))
    if cursor.fetchone()[0] > 0:
        conn.close()
        raise ValueError(f"快照名称已存在: {name}")

    cursor.execute(
        "SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
    )
    row = cursor.fetchone()
    rule_version = row[0] if row else 0

    active_config_name = ec.get_active_config_name() or "default"

    cursor.execute('SELECT COUNT(*) FROM corpus')
    corpus_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM review_records')
    review_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM conflict_records')
    conflict_count = cursor.fetchone()[0]

    cursor.execute('''
        INSERT INTO snapshots
        (name, description, rule_version, export_config_name,
         corpus_count, review_count, conflict_count, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
    ''', (name, description, rule_version, active_config_name,
          corpus_count, review_count, conflict_count, operator))
    snapshot_id = cursor.lastrowid

    _serialize_snapshot_data(cursor, snapshot_id)

    conn.commit()
    conn.close()

    log_operation(
        operation='snapshot_create',
        operator=operator,
        details=(
            f'创建快照 [{name}]：语料 {corpus_count} 条，'
            f'复核记录 {review_count} 条，冲突 {conflict_count} 条，'
            f'规则版本 v{rule_version}'
        ),
        rule_version=rule_version,
    )

    return get_snapshot(snapshot_id)


def _serialize_snapshot_data(cursor, snapshot_id: int) -> None:
    """序列化快照数据到 snapshot_data 表"""
    cursor.execute('''
        SELECT id, original_text, desensitized_text, source_file, status,
               rule_version, created_at, updated_at, is_sampled, sample_batch,
               final_conclusion, metadata
        FROM corpus ORDER BY id
    ''')
    corpus_rows = cursor.fetchall()
    corpus_list = [
        {
            "id": r[0],
            "original_text": r[1],
            "desensitized_text": r[2],
            "source_file": r[3],
            "status": r[4],
            "rule_version": r[5],
            "created_at": r[6],
            "updated_at": r[7],
            "is_sampled": bool(r[8]),
            "sample_batch": r[9],
            "final_conclusion": r[10],
            "metadata": json.loads(r[11]) if r[11] else {},
        }
        for r in corpus_rows
    ]

    cursor.execute('''
        SELECT id, corpus_id, reviewer, conclusion, comment,
               created_at, rule_version_at_review
        FROM review_records ORDER BY id
    ''')
    review_rows = cursor.fetchall()
    review_list = [
        {
            "id": r[0],
            "corpus_id": r[1],
            "reviewer": r[2],
            "conclusion": r[3],
            "comment": r[4],
            "created_at": r[5],
            "rule_version_at_review": r[6],
        }
        for r in review_rows
    ]

    cursor.execute('''
        SELECT id, corpus_id, reviewer1, reviewer2, conclusion1,
               conclusion2, resolved, created_at
        FROM conflict_records ORDER BY id
    ''')
    conflict_rows = cursor.fetchall()
    conflict_list = [
        {
            "id": r[0],
            "corpus_id": r[1],
            "reviewer1": r[2],
            "reviewer2": r[3],
            "conclusion1": r[4],
            "conclusion2": r[5],
            "resolved": bool(r[6]),
            "created_at": r[7],
        }
        for r in conflict_rows
    ]

    cursor.execute('SELECT config_json FROM export_configs WHERE config_name = ?',
                   ("default",))
    row = cursor.fetchone()
    export_config_json = row[0] if row else ""

    cursor.execute('''
        SELECT batch_name, sample_count, created_at, rule_version
        FROM sample_batches ORDER BY id
    ''')
    batch_rows = cursor.fetchall()
    batch_list = [
        {
            "batch_name": r[0],
            "sample_count": r[1],
            "created_at": r[2],
            "rule_version": r[3],
        }
        for r in batch_rows
    ]

    cursor.execute('''
        INSERT INTO snapshot_data
        (snapshot_id, corpus_json, review_records_json, conflict_records_json,
         export_config_json, sample_batches_json)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        snapshot_id,
        json.dumps(corpus_list, ensure_ascii=False),
        json.dumps(review_list, ensure_ascii=False),
        json.dumps(conflict_list, ensure_ascii=False),
        export_config_json,
        json.dumps(batch_list, ensure_ascii=False),
    ))


def list_snapshots() -> List[Snapshot]:
    """列出所有快照（按创建时间倒序）"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, description, rule_version, export_config_name,
               corpus_count, review_count, conflict_count, created_at, created_by
        FROM snapshots ORDER BY created_at DESC, id DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    return [Snapshot.from_row(r) for r in rows]


def get_snapshot(snapshot_id: int) -> Snapshot:
    """根据ID获取快照元数据"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, description, rule_version, export_config_name,
               corpus_count, review_count, conflict_count, created_at, created_by
        FROM snapshots WHERE id = ?
    ''', (snapshot_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"快照不存在: ID={snapshot_id}")
    return Snapshot.from_row(row)


def get_snapshot_by_name(name: str) -> Snapshot:
    """根据名称获取快照元数据"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, description, rule_version, export_config_name,
               corpus_count, review_count, conflict_count, created_at, created_by
        FROM snapshots WHERE name = ?
    ''', (name,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"快照不存在: {name}")
    return Snapshot.from_row(row)


def _load_snapshot_data(cursor, snapshot_id: int) -> Dict:
    """加载快照的完整数据"""
    cursor.execute('''
        SELECT corpus_json, review_records_json, conflict_records_json,
               export_config_json, sample_batches_json
        FROM snapshot_data WHERE snapshot_id = ?
    ''', (snapshot_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"快照数据不存在: ID={snapshot_id}")

    try:
        corpus_data = json.loads(row[0]) if row[0] else []
        review_data = json.loads(row[1]) if row[1] else []
        conflict_data = json.loads(row[2]) if row[2] else []
        export_config_json = row[3] or ""
        batch_data = json.loads(row[4]) if row[4] else []
    except json.JSONDecodeError as e:
        raise ValueError(f"快照数据损坏，JSON解析失败: {e}")

    return {
        "corpus": corpus_data,
        "review_records": review_data,
        "conflict_records": conflict_data,
        "export_config_json": export_config_json,
        "sample_batches": batch_data,
    }


def _validate_snapshot_name(name: str) -> bool:
    """验证快照名称合法性"""
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if len(name) == 0 or len(name) > 64:
        return False
    if not re.match(r'^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$', name):
        return False
    return True


def export_snapshot(snapshot_id: int, file_path: str,
                    operator: str = "system") -> bool:
    """导出快照到 JSON 文件"""
    snap = get_snapshot(snapshot_id)

    conn = get_connection()
    cursor = conn.cursor()
    data = _load_snapshot_data(cursor, snapshot_id)
    conn.close()

    export_data = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_info": {
            "name": snap.name,
            "description": snap.description,
            "rule_version": snap.rule_version,
            "export_config_name": snap.export_config_name,
            "corpus_count": snap.corpus_count,
            "review_count": snap.review_count,
            "conflict_count": snap.conflict_count,
            "created_at": snap.created_at,
            "created_by": snap.created_by,
        },
        "data": data,
    }

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise ValueError(f"导出快照文件失败: {e}")

    log_operation(
        operation='snapshot_export',
        operator=operator,
        details=f'导出快照 [{snap.name}] 到 {os.path.basename(file_path)}',
        rule_version=snap.rule_version,
    )

    return True


def import_snapshot(file_path: str, operator: str = "system",
                    overwrite: bool = False,
                    rename_to: Optional[str] = None) -> Snapshot:
    """从 JSON 文件导入快照

    - overwrite=False 时若同名快照已存在则拒绝
    - rename_to 可指定导入后的新名称
    """
    if not os.path.exists(file_path):
        raise ValueError(f"快照文件不存在: {file_path}")

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            export_data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"快照文件读取失败或损坏: {e}")

    _validate_snapshot_file(export_data)

    info = export_data["snapshot_info"]
    data = export_data["data"]

    target_name = rename_to if rename_to else info["name"]
    if not _validate_snapshot_name(target_name):
        raise ValueError(f"快照名称不合法: {target_name}")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT id FROM snapshots WHERE name = ?', (target_name,))
    existing = cursor.fetchone()

    if existing and not overwrite:
        conn.close()
        raise ValueError(
            f"快照 [{target_name}] 已存在，如需覆盖请指定 overwrite=True"
        )

    if existing and overwrite:
        old_id = existing[0]
        cursor.execute('DELETE FROM snapshot_data WHERE snapshot_id = ?', (old_id,))
        cursor.execute('DELETE FROM snapshots WHERE id = ?', (old_id,))

    cursor.execute('''
        INSERT INTO snapshots
        (name, description, rule_version, export_config_name,
         corpus_count, review_count, conflict_count, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        target_name,
        info.get("description", ""),
        info.get("rule_version", 0),
        info.get("export_config_name", "default"),
        len(data.get("corpus", [])),
        len(data.get("review_records", [])),
        len(data.get("conflict_records", [])),
        info.get("created_at", datetime.now().isoformat()),
        f"{info.get('created_by', 'unknown')} (imported by {operator})",
    ))
    snapshot_id = cursor.lastrowid

    cursor.execute('''
        INSERT INTO snapshot_data
        (snapshot_id, corpus_json, review_records_json, conflict_records_json,
         export_config_json, sample_batches_json)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        snapshot_id,
        json.dumps(data.get("corpus", []), ensure_ascii=False),
        json.dumps(data.get("review_records", []), ensure_ascii=False),
        json.dumps(data.get("conflict_records", []), ensure_ascii=False),
        data.get("export_config_json", ""),
        json.dumps(data.get("sample_batches", []), ensure_ascii=False),
    ))

    conn.commit()
    conn.close()

    log_operation(
        operation='snapshot_import',
        operator=operator,
        details=(
            f'从 {os.path.basename(file_path)} 导入快照 [{target_name}]：'
            f'语料 {len(data.get("corpus", []))} 条'
        ),
        rule_version=info.get("rule_version", 0),
    )

    return get_snapshot(snapshot_id)


def _validate_snapshot_file(export_data: dict) -> None:
    """验证快照文件结构完整性，损坏则抛出异常"""
    if not isinstance(export_data, dict):
        raise ValueError("快照文件格式错误：根对象不是字典")

    if export_data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"快照文件版本不兼容：期望 v{SNAPSHOT_SCHEMA_VERSION}，"
            f"实际 v{export_data.get('schema_version')}"
        )

    if "snapshot_info" not in export_data:
        raise ValueError("快照文件缺少 snapshot_info 字段")

    if "data" not in export_data:
        raise ValueError("快照文件缺少 data 字段")

    data = export_data["data"]
    for required_key in ("corpus", "review_records", "conflict_records"):
        if required_key not in data:
            raise ValueError(f"快照数据缺少 {required_key} 字段")

    for item in data.get("corpus", []):
        for field in ("id", "original_text", "status", "rule_version"):
            if field not in item:
                raise ValueError(f"语料数据缺少必填字段: {field}")


def delete_snapshot(snapshot_id: int, operator: str = "system") -> bool:
    """删除快照"""
    snap = get_snapshot(snapshot_id)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM snapshot_data WHERE snapshot_id = ?', (snapshot_id,))
    cursor.execute('DELETE FROM snapshots WHERE id = ?', (snapshot_id,))
    conn.commit()
    conn.close()

    log_operation(
        operation='snapshot_delete',
        operator=operator,
        details=f'删除快照 [{snap.name}]',
        rule_version=snap.rule_version,
    )

    return True


def preview_rollback(snapshot_id: int) -> Dict:
    """预览回滚影响，返回影响统计

    返回内容：
    - 语料变化数（新增/删除/修改）
    - 复核记录变化数
    - 冲突记录变化数
    - 规则版本变化
    - 导出配置变化
    - 警告信息（配置不存在、冲突等）
    """
    snap = get_snapshot(snapshot_id)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
    )
    current_version_row = cursor.fetchone()
    current_rule_version = current_version_row[0] if current_version_row else 0

    active_config_name = ec.get_active_config_name() or "default"

    cursor.execute('SELECT COUNT(*) FROM corpus')
    current_corpus_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM review_records')
    current_review_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM conflict_records')
    current_conflict_count = cursor.fetchone()[0]

    data = _load_snapshot_data(cursor, snapshot_id)
    conn.close()

    warnings: List[str] = []

    if snap.export_config_name:
        if not ec.config_exists(snap.export_config_name):
            warnings.append(
                f"快照引用的导出配置 [{snap.export_config_name}] 不存在，"
                f"回滚时将无法恢复该配置，请先导入或创建该配置"
            )

    if snap.rule_version != current_rule_version:
        warnings.append(
            f"快照规则版本 v{snap.rule_version} 与当前 v{current_rule_version} 不同，"
            f"回滚仅恢复语料数据，不会自动切换规则版本"
        )

    return {
        "snapshot_name": snap.name,
        "snapshot_rule_version": snap.rule_version,
        "current_rule_version": current_rule_version,
        "snapshot_export_config": snap.export_config_name,
        "current_export_config": active_config_name,
        "corpus": {
            "current": current_corpus_count,
            "snapshot": snap.corpus_count,
            "delta": snap.corpus_count - current_corpus_count,
        },
        "review_records": {
            "current": current_review_count,
            "snapshot": snap.review_count,
            "delta": snap.review_count - current_review_count,
        },
        "conflict_records": {
            "current": current_conflict_count,
            "snapshot": snap.conflict_count,
            "delta": snap.conflict_count - current_conflict_count,
        },
        "warnings": warnings,
    }


def rollback_snapshot(snapshot_id: int, operator: str = "system") -> Dict:
    """执行回滚

    安全检查：
    - 配置不存在则拦截
    - 快照数据损坏则拦截
    - 回滚前写入审计日志

    回滚内容：
    - 完全替换 corpus 表
    - 完全替换 review_records 表
    - 完全替换 conflict_records 表
    - 完全替换 sample_batches 表
    - 不自动切换规则版本（需用户手动切换）
    - 不自动切换导出配置（需用户手动切换）

    回滚使用事务，失败时不污染原数据。
    """
    snap = get_snapshot(snapshot_id)

    conn = get_connection()
    cursor = conn.cursor()

    try:
        data = _load_snapshot_data(cursor, snapshot_id)
    except ValueError as e:
        conn.close()
        raise ValueError(f"回滚被拦截：快照数据损坏 - {e}")

    try:
        preview = preview_rollback(snapshot_id)
    except ValueError as e:
        conn.close()
        raise ValueError(f"回滚被拦截：{e}")

    config_missing_warnings = [
        w for w in preview["warnings"]
        if "导出配置" in w and "不存在" in w
    ]
    if config_missing_warnings:
        conn.close()
        raise ValueError(
            "回滚被拦截：" + "; ".join(config_missing_warnings)
        )

    try:
        cursor.execute('BEGIN')

        cursor.execute('DELETE FROM review_records')
        cursor.execute('DELETE FROM conflict_records')
        cursor.execute('DELETE FROM sample_batches')
        cursor.execute('DELETE FROM corpus')

        for item in data["corpus"]:
            cursor.execute('''
                INSERT INTO corpus
                (id, original_text, desensitized_text, source_file, status,
                 rule_version, created_at, updated_at, is_sampled, sample_batch,
                 final_conclusion, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item["id"],
                item["original_text"],
                item.get("desensitized_text", ""),
                item.get("source_file", ""),
                item.get("status", "imported"),
                item.get("rule_version", 0),
                item.get("created_at", datetime.now().isoformat()),
                item.get("updated_at", datetime.now().isoformat()),
                1 if item.get("is_sampled") else 0,
                item.get("sample_batch"),
                item.get("final_conclusion"),
                json.dumps(item.get("metadata", {}), ensure_ascii=False),
            ))

        for item in data["review_records"]:
            cursor.execute('''
                INSERT INTO review_records
                (id, corpus_id, reviewer, conclusion, comment,
                 created_at, rule_version_at_review)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                item["id"],
                item["corpus_id"],
                item["reviewer"],
                item["conclusion"],
                item.get("comment", ""),
                item.get("created_at", datetime.now().isoformat()),
                item.get("rule_version_at_review", 0),
            ))

        for item in data["conflict_records"]:
            cursor.execute('''
                INSERT INTO conflict_records
                (id, corpus_id, reviewer1, reviewer2, conclusion1,
                 conclusion2, resolved, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item["id"],
                item["corpus_id"],
                item["reviewer1"],
                item["reviewer2"],
                item["conclusion1"],
                item["conclusion2"],
                1 if item.get("resolved") else 0,
                item.get("created_at", datetime.now().isoformat()),
            ))

        for item in data.get("sample_batches", []):
            cursor.execute('''
                INSERT INTO sample_batches
                (batch_name, sample_count, created_at, rule_version)
                VALUES (?, ?, ?, ?)
            ''', (
                item["batch_name"],
                item["sample_count"],
                item.get("created_at", datetime.now().isoformat()),
                item.get("rule_version", 0),
            ))

        conn.commit()

    except Exception as e:
        conn.rollback()
        conn.close()
        log_operation(
            operation='snapshot_rollback_failed',
            operator=operator,
            details=f'回滚快照 [{snap.name}] 失败: {e}',
            rule_version=snap.rule_version,
        )
        raise ValueError(f"回滚失败，已回滚事务: {e}")

    conn.close()

    affected_corpus = data.get("corpus", []).__len__()
    affected_reviews = data.get("review_records", []).__len__()
    affected_conflicts = data.get("conflict_records", []).__len__()

    log_operation(
        operation='snapshot_rollback',
        operator=operator,
        details=(
            f'回滚到快照 [{snap.name}]：'
            f'影响语料 {affected_corpus} 条，'
            f'复核记录 {affected_reviews} 条，'
            f'冲突记录 {affected_conflicts} 条，'
            f'规则版本 v{snap.rule_version}'
        ),
        rule_version=snap.rule_version,
    )

    return {
        "snapshot_name": snap.name,
        "corpus_restored": affected_corpus,
        "review_records_restored": affected_reviews,
        "conflict_records_restored": affected_conflicts,
        "rule_version": snap.rule_version,
    }
