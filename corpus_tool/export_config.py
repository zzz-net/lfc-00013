"""导出配置管理模块

支持：
- 字段保留策略（保留/删除每个可选字段）
- CSV / JSONL 两种导出格式
- 复核摘要开关
- 配置冲突检测
- 旧配置导入与兼容迁移
- 配置持久化到数据库
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
from enum import Enum

from .database import get_connection
from .audit import log_operation


class FieldPolicy(str, Enum):
    KEEP = "keep"
    DROP = "drop"


class ExportFormat(str, Enum):
    CSV = "csv"
    JSONL = "jsonl"


ALL_EXPORTABLE_FIELDS: List[str] = [
    "id",
    "original_text",
    "desensitized_text",
    "source_file",
    "status",
    "rule_version",
    "created_at",
    "updated_at",
    "is_sampled",
    "sample_batch",
    "final_conclusion",
    "review_summary",
]


REQUIRED_FIELDS: List[str] = [
    "id",
    "desensitized_text",
]


DEFAULT_FIELD_POLICIES: Dict[str, str] = {
    "id": FieldPolicy.KEEP.value,
    "original_text": FieldPolicy.DROP.value,
    "desensitized_text": FieldPolicy.KEEP.value,
    "source_file": FieldPolicy.KEEP.value,
    "status": FieldPolicy.DROP.value,
    "rule_version": FieldPolicy.KEEP.value,
    "created_at": FieldPolicy.DROP.value,
    "updated_at": FieldPolicy.DROP.value,
    "is_sampled": FieldPolicy.DROP.value,
    "sample_batch": FieldPolicy.DROP.value,
    "final_conclusion": FieldPolicy.KEEP.value,
    "review_summary": FieldPolicy.DROP.value,
}


@dataclass
class ExportConfig:
    """导出配置数据类"""
    config_version: int = 2
    format: str = ExportFormat.CSV.value
    include_review_summary: bool = False
    field_policies: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FIELD_POLICIES))
    _migration_errors: List[str] = field(default_factory=list, repr=False)

    def to_json(self) -> str:
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "ExportConfig":
        data = json.loads(raw)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "ExportConfig":
        cfg = cls()
        cfg.config_version = data.get("config_version", 1)
        cfg.format = data.get("format", ExportFormat.CSV.value)
        cfg.include_review_summary = data.get("include_review_summary", False)

        cfg._migration_errors = list(_detect_legacy_field_conflicts(data))

        policies = data.get("field_policies")
        if policies is None:
            policies = _migrate_legacy_field_flags(data)
        cfg.field_policies = dict(DEFAULT_FIELD_POLICIES)
        for k, v in policies.items():
            if k in DEFAULT_FIELD_POLICIES:
                cfg.field_policies[k] = v

        if cfg.include_review_summary:
            cfg.field_policies["review_summary"] = FieldPolicy.KEEP.value
        return cfg

    def _auto_sync_flags(self) -> None:
        """根据布尔开关与字段策略的关系自动同步，避免用户设了一边忘记另一边"""
        if self.include_review_summary:
            self.field_policies["review_summary"] = FieldPolicy.KEEP.value
        if self.field_policies.get("review_summary") == FieldPolicy.KEEP.value:
            self.include_review_summary = True

    def validate(self) -> Tuple[bool, List[str]]:
        """验证配置合法性，返回 (是否通过, 错误列表)

        注意：warning 级别的提示（如安全提示、兼容说明）也会放在 errors 中，
        调用方需按是否含"安全提示"/"兼容提示"前缀区分致命错误与警告。
        """
        self._auto_sync_flags()
        errors: List[str] = []

        if self._migration_errors:
            errors.extend(self._migration_errors)

        if self.format not in (ExportFormat.CSV.value, ExportFormat.JSONL.value):
            errors.append(f"不支持的导出格式: {self.format}，必须是 csv 或 jsonl")

        for req in REQUIRED_FIELDS:
            if self.field_policies.get(req) != FieldPolicy.KEEP.value:
                errors.append(f"必填字段 [{req}] 不能被删除（策略必须为 keep）")

        conflicts = self._detect_policy_conflicts()
        errors.extend(conflicts)

        unknown_fields = set(self.field_policies.keys()) - set(ALL_EXPORTABLE_FIELDS)
        for uf in unknown_fields:
            errors.append(f"未知字段 [{uf}]，不在可导出字段列表中")

        invalid_values = [
            k for k, v in self.field_policies.items()
            if v not in (FieldPolicy.KEEP.value, FieldPolicy.DROP.value)
        ]
        for f in invalid_values:
            errors.append(
                f"字段 [{f}] 的策略值 [{self.field_policies[f]}] 不合法，必须是 keep 或 drop"
            )

        return len([e for e in errors if not (e.startswith("安全提示") or e.startswith("兼容提示"))]) == 0, errors

    def _detect_policy_conflicts(self) -> List[str]:
        """检测语义冲突。安全提示与兼容提示不作为致命错误。"""
        conflicts: List[str] = []

        if self.field_policies.get("original_text") == FieldPolicy.KEEP.value:
            conflicts.append(
                "安全提示：字段 [original_text] 被标记为保留，导出文件将包含原始敏感数据，"
                "请确认操作人已获得相应授权（此为警告，但不阻止导出）"
            )

        return conflicts

    def get_effective_fields(self) -> List[str]:
        """根据策略计算最终保留的字段（按 ALL_EXPORTABLE_FIELDS 顺序）"""
        return [
            f for f in ALL_EXPORTABLE_FIELDS
            if self.field_policies.get(f) == FieldPolicy.KEEP.value
        ]

    def summary_text(self) -> str:
        """生成人类可读的配置摘要"""
        fields = self.get_effective_fields()
        lines = [
            f"配置版本: v{self.config_version}",
            f"导出格式: {self.format.upper()}",
            f"复核摘要: {'开启' if self.include_review_summary else '关闭'}",
            f"保留字段 ({len(fields)} 个): {', '.join(fields)}",
        ]
        return "\n".join(lines)


def _detect_legacy_field_conflicts(data: dict) -> List[str]:
    """检测旧配置中同一字段同时出现在 include_fields 和 exclude_fields 的冲突。

    返回致命错误列表（非警告），发现冲突时阻止保存/导出。
    """
    errors: List[str] = []

    legacy_include = data.get("include_fields") or data.get("fields")
    legacy_exclude = data.get("exclude_fields")

    include_set: set = set()
    exclude_set: set = set()

    if isinstance(legacy_include, list):
        include_set = {str(f) for f in legacy_include if f in DEFAULT_FIELD_POLICIES}
    if isinstance(legacy_exclude, list):
        exclude_set = {str(f) for f in legacy_exclude if f in DEFAULT_FIELD_POLICIES}

    overlap = sorted(include_set & exclude_set)
    if overlap:
        errors.append(
            "旧配置字段冲突：字段 [" + ", ".join(overlap) + "] 同时出现在 include_fields/fields 和 exclude_fields 中；"
            "请删除冲突项后再导入"
        )

    include_original = data.get("include_original")
    if isinstance(include_original, bool) and not include_original and "original_text" in include_set:
        errors.append(
            "旧配置字段冲突：include_original=False 但 original_text 出现在 include_fields 中；"
            "请保留一致的设置后再导入"
        )
    if isinstance(include_original, bool) and include_original and "original_text" in exclude_set:
        errors.append(
            "旧配置字段冲突：include_original=True 但 original_text 出现在 exclude_fields 中；"
            "请保留一致的设置后再导入"
        )

    return errors


def _migrate_legacy_field_flags(data: dict) -> Dict[str, str]:
    """从 v1 旧配置（include_original 等布尔开关）迁移到 v2 字段策略"""
    policies = dict(DEFAULT_FIELD_POLICIES)

    include_original = data.get("include_original")
    if isinstance(include_original, bool):
        policies["original_text"] = FieldPolicy.KEEP.value if include_original else FieldPolicy.DROP.value

    legacy_include = data.get("include_fields") or data.get("fields")
    if isinstance(legacy_include, list):
        for f in legacy_include:
            if f in DEFAULT_FIELD_POLICIES:
                policies[f] = FieldPolicy.KEEP.value

    legacy_exclude = data.get("exclude_fields")
    if isinstance(legacy_exclude, list):
        for f in legacy_exclude:
            if f in DEFAULT_FIELD_POLICIES and f not in REQUIRED_FIELDS:
                policies[f] = FieldPolicy.DROP.value

    return policies


def detect_legacy_compat_issues(data: dict) -> List[str]:
    """检测旧配置中需要用户注意的兼容项"""
    issues: List[str] = []
    version = data.get("config_version", 1)

    if version < 2:
        issues.append(
            f"旧配置版本 v{version}，将自动迁移到 v2 字段策略配置；"
            "建议重新使用新格式保存配置以避免后续歧义"
        )

    if "include_original" in data:
        issues.append(
            "已识别到旧键 include_original，已映射为 field_policies['original_text']；"
            "后续建议直接在 field_policies 中设置该字段策略"
        )

    if "include_fields" in data or "fields" in data:
        issues.append(
            "已识别到旧键 include_fields/fields，已转换为对应字段的 keep 策略"
        )

    if "exclude_fields" in data:
        issues.append(
            "已识别到旧键 exclude_fields，已转换为对应字段的 drop 策略"
        )

    if "format" not in data:
        issues.append("未指定导出格式，默认使用 CSV")

    return issues


CONFIG_TABLE_SQL = '''
CREATE TABLE IF NOT EXISTS export_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_name TEXT NOT NULL DEFAULT 'default',
    config_json TEXT NOT NULL,
    is_active INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(config_name)
)
'''


def init_config_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(CONFIG_TABLE_SQL)
    conn.commit()
    conn.close()


def save_config(config: ExportConfig, config_name: str = "default",
                operator: str = "system") -> Tuple[bool, List[str], List[str]]:
    """保存配置（含验证、日志）。返回 (是否成功, 错误列表, 警告列表)"""
    init_config_table()

    valid, errors = config.validate()
    warnings = detect_legacy_compat_issues({"config_version": config.config_version})

    fatal_errors = [e for e in errors if "安全提示" not in e]
    safety_warnings = [e for e in errors if "安全提示" in e]
    warnings.extend(safety_warnings)

    if fatal_errors:
        log_operation(
            operation='config_save_failed',
            operator=operator,
            details=f"保存导出配置 [{config_name}] 失败: {'; '.join(fatal_errors)}",
            rule_version=0,
        )
        return False, fatal_errors, warnings

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT config_json FROM export_configs WHERE config_name = ?', (config_name,))
    existing = cursor.fetchone()

    if existing:
        cursor.execute(
            'UPDATE export_configs SET config_json=?, is_active=1, updated_at=datetime(\'now\') '
            'WHERE config_name=?',
            (config.to_json(), config_name)
        )
    else:
        cursor.execute(
            'INSERT INTO export_configs (config_name, config_json, is_active, created_at, updated_at) '
            'VALUES (?, ?, 1, datetime(\'now\'), datetime(\'now\'))',
            (config_name, config.to_json())
        )

    conn.commit()
    conn.close()

    log_operation(
        operation='config_save',
        operator=operator,
        details=f"保存导出配置 [{config_name}]：格式={config.format.upper()}, "
                f"字段数={len(config.get_effective_fields())}",
        rule_version=0,
    )

    return True, [], warnings


def load_config(config_name: str = "default") -> Tuple[Optional[ExportConfig], List[str]]:
    """加载配置，找不到时返回默认配置。返回 (配置, 迁移警告列表)"""
    init_config_table()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT config_json FROM export_configs WHERE config_name = ? AND is_active = 1',
                   (config_name,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        default = ExportConfig()
        return default, ["未找到已保存的配置，已使用默认导出配置（CSV格式，保留 id/脱敏文本/来源/规则版本/最终结论）"]

    try:
        raw = json.loads(row[0])
    except json.JSONDecodeError as e:
        default = ExportConfig()
        warnings = [f"配置 JSON 解析失败 ({e})，已回退到默认配置"]
        return default, warnings

    compat_issues = detect_legacy_compat_issues(raw)
    config = ExportConfig.from_dict(raw)
    return config, compat_issues


def import_config_from_file(file_path: str, target_name: str = "default",
                            operator: str = "system") -> Tuple[bool, List[str], List[str]]:
    """从 JSON 文件导入配置。

    - 使用 utf-8-sig 编码读取，自动兼容带 BOM 和无 BOM 的 UTF-8 文件
    - 旧配置 (include_fields + exclude_fields) 存在字段交集时直接作为致命错误返回
      （检测由 ExportConfig.from_dict → validate() 统一负责）
    - 导入失败会写入 config_import_failed 审计日志
    """
    if not os.path.exists(file_path):
        return False, [f"配置文件不存在: {file_path}"], []

    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log_operation(
            operation='config_import_failed',
            operator=operator,
            details=f"导入配置文件 [{os.path.basename(file_path)}] 失败: 文件读取/JSON 解析错误: {e}",
            rule_version=0,
        )
        return False, [f"配置文件读取失败: {e}"], []

    compat = detect_legacy_compat_issues(raw)
    config = ExportConfig.from_dict(raw)

    valid, errors = config.validate()
    fatal = [e for e in errors if "安全提示" not in e and "兼容提示" not in e]
    safety = [e for e in errors if "安全提示" in e or "兼容提示" in e]

    warnings = compat + safety

    if fatal:
        log_operation(
            operation='config_import_failed',
            operator=operator,
            details=f"导入配置文件 [{os.path.basename(file_path)}] 失败: {'; '.join(fatal)}",
            rule_version=0,
        )
        return False, fatal, warnings

    ok, save_errors, save_warnings = save_config(config, target_name, operator)
    all_warnings = warnings + save_warnings
    return ok, save_errors, all_warnings


def export_config_to_file(file_path: str, config_name: str = "default",
                          operator: str = "system") -> Tuple[bool, List[str]]:
    """导出当前配置到 JSON 文件"""
    config, _ = load_config(config_name)
    if config is None:
        return False, ["无法加载当前配置"]

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(config.to_json())
    except OSError as e:
        return False, [f"写入文件失败: {e}"]

    log_operation(
        operation='config_export',
        operator=operator,
        details=f"导出配置 [{config_name}] 到 {os.path.basename(file_path)}",
        rule_version=0,
    )
    return True, []


def reset_config(config_name: str = "default", operator: str = "system") -> Tuple[bool, List[str]]:
    """重置为默认配置"""
    default = ExportConfig()
    ok, errors, warnings = save_config(default, config_name, operator)
    if warnings:
        return ok, errors + warnings
    return ok, errors
