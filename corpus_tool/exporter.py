"""数据导出模块

支持：
- 基于 ExportConfig 的字段保留策略
- CSV / JSONL 两种导出格式
- 可选的复核摘要字段
- 严格的导出就绪检查（不绕过脱敏+双人复核约束）
- 敏感泄露检测
"""
import os
import csv
import json
from typing import List, Dict, Optional, Tuple
from .database import get_connection
from .audit import log_operation
from .models import Corpus
from . import export_config as ec


AVAILABLE_CORPUS_COLUMNS = [
    "id", "original_text", "desensitized_text", "source_file",
    "status", "rule_version", "created_at", "updated_at",
    "is_sampled", "sample_batch", "final_conclusion", "metadata",
]


def check_export_ready() -> tuple:
    """检查是否满足导出条件（待复核=0 且 未解决冲突=0）"""
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


def _build_review_summary(cursor, corpus_id: int) -> str:
    """构建单条语料的复核摘要（若开启则拼到 review_summary 字段）"""
    cursor.execute('''
        SELECT reviewer, conclusion, comment FROM review_records
        WHERE corpus_id = ? ORDER BY id
    ''', (corpus_id,))
    rows = cursor.fetchall()
    if not rows:
        return "未抽检"

    cursor.execute('''
        SELECT resolved, reviewer1, conclusion1, reviewer2, conclusion2
        FROM conflict_records WHERE corpus_id = ? ORDER BY id DESC LIMIT 1
    ''', (corpus_id,))
    conflict_row = cursor.fetchone()

    parts = []
    for reviewer, conclusion, comment in rows:
        c = comment or ""
        suffix = "(" + c[:20] + ")" if c else ""
        parts.append(f"{reviewer}:{conclusion}" + suffix)

    if conflict_row:
        resolved, r1, c1, r2, c2 = conflict_row
        tag = "已仲裁" if resolved else "未解决冲突"
        parts.append(f"[{tag}] {r1}:{c1} vs {r2}:{c2}")

    return "; ".join(parts)


def _fetch_exportable_rows(cursor) -> List[Corpus]:
    """拉取所有可导出的语料（脱敏完成 + 复核通过）"""
    cursor.execute('''
        SELECT id, original_text, desensitized_text, source_file, status, rule_version,
               created_at, updated_at, is_sampled, sample_batch, final_conclusion, metadata
        FROM corpus 
        WHERE status = 'desensitized' 
           OR (status = 'reviewed' AND final_conclusion = 'approved')
        ORDER BY id
    ''')
    rows = cursor.fetchall()
    return [Corpus.from_row(r) for r in rows]


def _corpus_to_row_dict(cursor, corpus: Corpus, fields: List[str],
                        include_review_summary: bool) -> Dict[str, object]:
    """按配置字段提取语料为字典，同时构造复核摘要"""
    data = {}
    review_summary_cached: Optional[str] = None

    def get_review_summary() -> str:
        nonlocal review_summary_cached
        if review_summary_cached is None:
            review_summary_cached = _build_review_summary(cursor, corpus.id)
        return review_summary_cached

    corpus_attrs = {
        "id": corpus.id,
        "original_text": corpus.original_text,
        "desensitized_text": corpus.desensitized_text,
        "source_file": corpus.source_file,
        "status": corpus.status,
        "rule_version": corpus.rule_version,
        "created_at": corpus.created_at,
        "updated_at": corpus.updated_at,
        "is_sampled": 1 if corpus.is_sampled else 0,
        "sample_batch": corpus.sample_batch or "",
        "final_conclusion": corpus.final_conclusion or "",
        "review_summary": get_review_summary() if include_review_summary else "",
    }

    for f in fields:
        data[f] = corpus_attrs.get(f, "")
    return data


def _get_active_rule_version(cursor) -> int:
    cursor.execute('''
        SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1
    ''')
    row = cursor.fetchone()
    return row[0] if row else 1


def export_desensitized(output_path: str, include_original: bool = False,
                        operator: str = "system",
                        config: "Optional[ec.ExportConfig]" = None,
                        use_saved_config: bool = False,
                        config_name: str = "default") -> int:
    """导出脱敏语料

    优先级：
    1. 显式传入 config > 2. use_saved_config=True 从 DB 加载 > 3. 回退到旧参数构造

    始终保留就绪检查与双人复核约束。
    """
    ready, pending, conflicts = check_export_ready()
    if not ready:
        raise ValueError(
            f"导出条件不满足：{pending} 条样本待复核，{conflicts} 个冲突未解决"
        )

    final_config: ec.ExportConfig
    compat_warnings: List[str] = []

    if config is not None:
        final_config = config
    elif use_saved_config:
        loaded_cfg, warnings = ec.load_config(config_name)
        final_config = loaded_cfg or ec.ExportConfig()
        compat_warnings = warnings
    else:
        final_config = ec.ExportConfig()
        if include_original:
            final_config.field_policies["original_text"] = ec.FieldPolicy.KEEP.value

    valid, errors = final_config.validate()
    fatal = [e for e in errors if "安全提示" not in e]
    if fatal:
        raise ValueError("导出配置存在冲突，已阻止导出：\n  - " + "\n  - ".join(fatal))

    ext_from_path = os.path.splitext(output_path)[1].lower().lstrip(".")
    effective_format = ext_from_path if ext_from_path in ("csv", "jsonl") else final_config.format
    if effective_format not in ("csv", "jsonl"):
        raise ValueError(f"无法确定导出格式：文件扩展名={ext_from_path or '空'}, 配置格式={final_config.format}")

    conn = get_connection()
    cursor = conn.cursor()
    version = _get_active_rule_version(cursor)
    corpus_list = _fetch_exportable_rows(cursor)
    fields = final_config.get_effective_fields()

    if final_config.include_review_summary and "review_summary" not in fields:
        fields = fields + ["review_summary"]

    need_summary = "review_summary" in fields or final_config.include_review_summary

    rows_written = 0

    if effective_format == "csv":
        with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(fields)
            for c in corpus_list:
                row_data = _corpus_to_row_dict(cursor, c, fields, need_summary)
                writer.writerow([str(row_data.get(f, "")) for f in fields])
                rows_written += 1
    elif effective_format == "jsonl":
        with open(output_path, 'w', encoding='utf-8') as f:
            for c in corpus_list:
                row_data = _corpus_to_row_dict(cursor, c, fields, need_summary)
                f.write(json.dumps(row_data, ensure_ascii=False) + "\n")
                rows_written += 1
    else:
        conn.close()
        raise ValueError(f"不支持的导出格式: {effective_format}")

    conn.close()

    extra_info = ""
    if compat_warnings:
        for w in compat_warnings:
            extra_info += f" [Compat] {w}"

    log_operation(
        operation='export',
        operator=operator,
        details=(
            f'导出 {len(corpus_list)} 条脱敏语料到 {os.path.basename(output_path)}，'
            f'规则版本 v{version}，格式={effective_format.upper()}，'
            f'字段数={len(fields)}，复核摘要={"开" if need_summary else "关"}'
            + extra_info
        ),
        rule_version=version,
    )

    return rows_written


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


def build_review_stats(config_name: str = "default") -> Dict[str, object]:
    """生成复核统计（用于导出摘要或日志分析）"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM corpus")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT status, COUNT(*) FROM corpus GROUP BY status")
    status_counts = dict(cursor.fetchall())

    cursor.execute("SELECT final_conclusion, COUNT(*) FROM corpus WHERE final_conclusion IS NOT NULL GROUP BY final_conclusion")
    conclusion_counts = dict(cursor.fetchall())

    cursor.execute("SELECT COUNT(*) FROM review_records")
    review_total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM conflict_records WHERE resolved = 0")
    unresolved_conflicts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM conflict_records WHERE resolved = 1")
    resolved_conflicts = cursor.fetchone()[0]

    conn.close()

    return {
        "total_corpus": total,
        "by_status": status_counts,
        "by_final_conclusion": conclusion_counts,
        "total_reviews": review_total,
        "unresolved_conflicts": unresolved_conflicts,
        "resolved_conflicts": resolved_conflicts,
    }
