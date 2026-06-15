"""导出配置管理模块

支持：
- 字段保留策略（保留/删除每个可选字段）
- CSV / JSONL 两种导出格式
- 复核摘要开关
- 配置冲突检测
- 旧配置导入与兼容迁移
- 配置持久化到数据库
- 多命名方案管理（新建/复制/重命名/切换/删除）
- 默认激活方案切换
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
    """保存配置（含验证、日志）。返回 (是否成功, 错误列表, 警告列表)

    - default 方案始终存在且为默认激活方案
    - 首次保存非 default 方案时默认不激活（保持原有激活方案不变）
    - 更新已有方案时，不改变 is_active 状态
    - 保存前确保 default 方案存在（若不存在则创建）
    """
    init_config_table()

    valid, name_errors = validate_config_name(config_name)
    if not valid:
        log_operation(
            operation='config_save_failed',
            operator=operator,
            details=f"保存导出配置失败：名称非法 - {'; '.join(name_errors)}",
            rule_version=0,
        )
        return False, name_errors, []

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

    if config_name != "default" and not config_exists("default"):
        default_cfg = ExportConfig()
        _save_config_direct(default_cfg, "default", is_active=1, operator=operator)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT is_active FROM export_configs WHERE config_name = ?', (config_name,))
    existing = cursor.fetchone()

    if existing:
        cursor.execute(
            'UPDATE export_configs SET config_json=?, updated_at=datetime(\'now\') '
            'WHERE config_name=?',
            (config.to_json(), config_name)
        )
        is_new = False
    else:
        is_default = config_name == "default"
        is_active = 1 if is_default else 0
        cursor.execute(
            'INSERT INTO export_configs (config_name, config_json, is_active, created_at, updated_at) '
            'VALUES (?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
            (config_name, config.to_json(), is_active)
        )
        is_new = True

    conn.commit()
    conn.close()

    log_operation(
        operation='config_save',
        operator=operator,
        details=(
            f"{'新建' if is_new else '更新'}导出配置 [{config_name}]："
            f"格式={config.format.upper()}, "
            f"字段数={len(config.get_effective_fields())}"
        ),
        rule_version=0,
    )

    return True, [], warnings


def _save_config_direct(config: ExportConfig, config_name: str, is_active: int = 0,
                        operator: str = "system") -> None:
    """内部函数：直接保存配置，不做验证和日志（用于内部初始化）"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO export_configs (config_name, config_json, is_active, created_at, updated_at) '
        'VALUES (?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
        (config_name, config.to_json(), is_active)
    )
    conn.commit()
    conn.close()


def load_config(config_name: str = "default") -> Tuple[Optional[ExportConfig], List[str]]:
    """加载指定名称的配置，找不到时返回默认配置。返回 (配置, 警告/提示列表)"""
    init_config_table()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT config_json FROM export_configs WHERE config_name = ?',
                   (config_name,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        default = ExportConfig()
        return default, [f"未找到配置 [{config_name}]，已使用默认导出配置（CSV格式，保留 id/脱敏文本/来源/规则版本/最终结论）"]

    try:
        raw = json.loads(row[0])
    except json.JSONDecodeError as e:
        default = ExportConfig()
        warnings = [f"配置 [{config_name}] JSON 解析失败 ({e})，已回退到默认配置"]
        return default, warnings

    compat_issues = detect_legacy_compat_issues(raw)
    config = ExportConfig.from_dict(raw)
    return config, compat_issues


def import_config_from_file(file_path: str, target_name: str = "default",
                            operator: str = "system",
                            overwrite: bool = False) -> Tuple[bool, List[str], List[str]]:
    """从 JSON 文件导入配置。

    - 使用 utf-8-sig 编码读取，自动兼容带 BOM 和无 BOM 的 UTF-8 文件
    - 旧配置 (include_fields + exclude_fields) 存在字段交集时直接作为致命错误返回
      （检测由 ExportConfig.from_dict → validate() 统一负责）
    - 导入失败会写入 config_import_failed 审计日志
    - overwrite=False（默认）时，若目标配置已存在则拒绝导入（保护已有方案）
    - overwrite=True 时，允许覆盖已存在的方案（default 方案也可被覆盖）
    - 导入失败时绝对不污染原配置
    """
    if not os.path.exists(file_path):
        return False, [f"配置文件不存在: {file_path}"], []

    valid, name_errors = validate_config_name(target_name)
    if not valid:
        log_operation(
            operation='config_import_failed',
            operator=operator,
            details=f"导入配置文件失败：目标名称非法 - {'; '.join(name_errors)}",
            rule_version=0,
        )
        return False, name_errors, []

    target_exists = config_exists(target_name)
    is_default = target_name == "default"
    if target_exists and not overwrite and not is_default:
        log_operation(
            operation='config_import_failed',
            operator=operator,
            details=(
                f"导入配置文件 [{os.path.basename(file_path)}] 失败："
                f"目标配置 [{target_name}] 已存在，如需覆盖请指定 overwrite=True"
            ),
            rule_version=0,
        )
        return False, [
            f"配置 [{target_name}] 已存在，不能直接覆盖；"
            f"如需覆盖请显式指定允许覆盖，或换用其他名称新建方案"
        ], []

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

    if ok and target_exists and overwrite:
        log_operation(
            operation='config_import_overwrite',
            operator=operator,
            details=f"导入配置文件 [{os.path.basename(file_path)}] 并覆盖方案 [{target_name}]",
            rule_version=0,
        )

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


def validate_config_name(name: str) -> Tuple[bool, List[str]]:
    """验证配置名称合法性

    规则：
    - 不能为空
    - 长度 1-64 字符
    - 只能包含字母、数字、下划线、中划线、点号
    - 不能以点号开头
    """
    errors: List[str] = []
    if not name or not isinstance(name, str):
        errors.append("配置名称不能为空")
        return False, errors

    name = name.strip()
    if len(name) == 0:
        errors.append("配置名称不能为空")
        return False, errors

    if len(name) > 64:
        errors.append(f"配置名称过长（最多 64 字符，当前 {len(name)} 字符）")

    if name.startswith("."):
        errors.append("配置名称不能以点号开头")

    import re
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        errors.append(
            f"配置名称 [{name}] 包含非法字符，只能使用字母、数字、下划线、中划线、点号"
        )

    return len(errors) == 0, errors


def config_exists(config_name: str) -> bool:
    """检查指定名称的配置是否存在"""
    init_config_table()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM export_configs WHERE config_name = ?', (config_name,))
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def list_configs() -> List[Dict[str, object]]:
    """列出所有配置方案

    返回列表，每项包含：name, is_active, created_at, updated_at, format, field_count
    """
    init_config_table()
    _ensure_default_active()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT config_name, is_active, created_at, updated_at, config_json
        FROM export_configs
        ORDER BY is_active DESC, created_at ASC
    ''')
    rows = cursor.fetchall()
    conn.close()

    result: List[Dict[str, object]] = []
    for name, is_active, created_at, updated_at, config_json in rows:
        try:
            cfg_data = json.loads(config_json)
            fmt = cfg_data.get("format", "csv")
            policies = cfg_data.get("field_policies", {})
            field_count = sum(1 for v in policies.values() if v == FieldPolicy.KEEP.value)
        except Exception:
            fmt = "unknown"
            field_count = 0

        result.append({
            "name": name,
            "is_active": bool(is_active),
            "created_at": created_at,
            "updated_at": updated_at,
            "format": fmt,
            "field_count": field_count,
        })

    return result


def get_active_config_name() -> Optional[str]:
    """获取当前激活的配置名称"""
    init_config_table()
    _ensure_default_active()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT config_name FROM export_configs WHERE is_active = 1 LIMIT 1')
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_active_config(config_name: str, operator: str = "system") -> Tuple[bool, List[str]]:
    """设置指定配置为激活方案

    - 配置必须存在
    - 只能有一个激活方案
    - 操作写入审计日志
    """
    init_config_table()

    if not config_exists(config_name):
        log_operation(
            operation='config_activate_failed',
            operator=operator,
            details=f"激活配置失败：配置 [{config_name}] 不存在",
            rule_version=0,
        )
        return False, [f"配置 [{config_name}] 不存在，无法激活"]

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('UPDATE export_configs SET is_active = 0 WHERE is_active = 1')
    cursor.execute('UPDATE export_configs SET is_active = 1 WHERE config_name = ?', (config_name,))
    conn.commit()
    conn.close()

    log_operation(
        operation='config_activate',
        operator=operator,
        details=f"激活导出配置方案 [{config_name}]",
        rule_version=0,
    )

    return True, []


def _ensure_default_active() -> None:
    """确保至少有一个激活的配置。如果没有，将 default 设为激活（若 default 不存在则创建）

    注意：此函数直接操作数据库，不调用 get_active_config_name / set_active_config 等高级函数，
    以避免递归调用。
    """
    init_config_table()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT config_name FROM export_configs WHERE is_active = 1 LIMIT 1')
    active_row = cursor.fetchone()

    if active_row:
        conn.close()
        return

    cursor.execute('SELECT COUNT(*) FROM export_configs WHERE config_name = ?', ("default",))
    default_exists = cursor.fetchone()[0] > 0

    if default_exists:
        cursor.execute('UPDATE export_configs SET is_active = 1 WHERE config_name = ?', ("default",))
        conn.commit()
        conn.close()
        log_operation(
            operation='config_activate',
            operator="system",
            details="激活导出配置方案 [default]（系统自动恢复）",
            rule_version=0,
        )
    else:
        default_cfg = ExportConfig()
        cursor.execute(
            'INSERT INTO export_configs (config_name, config_json, is_active, created_at, updated_at) '
            'VALUES (?, ?, 1, datetime(\'now\'), datetime(\'now\'))',
            ("default", default_cfg.to_json())
        )
        conn.commit()
        conn.close()
        log_operation(
            operation='config_save',
            operator="system",
            details="新建导出配置 [default]：格式=CSV, 字段数=5（系统默认初始化）",
            rule_version=0,
        )


def copy_config(source_name: str, target_name: str,
                operator: str = "system") -> Tuple[bool, List[str], List[str]]:
    """复制配置方案

    - source_name 必须存在
    - target_name 不能已存在
    - target_name 需通过名称合法性校验
    - 新配置默认非激活
    """
    init_config_table()

    valid, name_errors = validate_config_name(target_name)
    if not valid:
        log_operation(
            operation='config_copy_failed',
            operator=operator,
            details=f"复制配置失败：目标名称非法 - {'; '.join(name_errors)}",
            rule_version=0,
        )
        return False, name_errors, []

    if not config_exists(source_name):
        log_operation(
            operation='config_copy_failed',
            operator=operator,
            details=f"复制配置失败：源配置 [{source_name}] 不存在",
            rule_version=0,
        )
        return False, [f"源配置 [{source_name}] 不存在"], []

    if config_exists(target_name):
        log_operation(
            operation='config_copy_failed',
            operator=operator,
            details=f"复制配置失败：目标配置 [{target_name}] 已存在",
            rule_version=0,
        )
        return False, [f"目标配置 [{target_name}] 已存在，不能覆盖"], []

    src_cfg, warnings = load_config(source_name)
    if src_cfg is None:
        return False, [f"无法加载源配置 [{source_name}]"], []

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO export_configs (config_name, config_json, is_active, created_at, updated_at) '
        'VALUES (?, ?, 0, datetime(\'now\'), datetime(\'now\'))',
        (target_name, src_cfg.to_json())
    )
    conn.commit()
    conn.close()

    log_operation(
        operation='config_copy',
        operator=operator,
        details=f"复制配置：[{source_name}] -> [{target_name}]",
        rule_version=0,
    )

    return True, [], warnings


def rename_config(old_name: str, new_name: str,
                  operator: str = "system") -> Tuple[bool, List[str]]:
    """重命名配置方案

    - old_name 必须存在
    - new_name 不能已存在
    - new_name 需通过名称合法性校验
    - 不能重命名 default 方案（防止默认方案丢失）
    - 如果重命名的是激活方案，激活状态跟随新名称
    """
    init_config_table()

    if old_name == "default":
        log_operation(
            operation='config_rename_failed',
            operator=operator,
            details="重命名配置失败：不能重命名 default 默认方案",
            rule_version=0,
        )
        return False, ["不能重命名 default 默认方案"]

    valid, name_errors = validate_config_name(new_name)
    if not valid:
        log_operation(
            operation='config_rename_failed',
            operator=operator,
            details=f"重命名配置失败：新名称非法 - {'; '.join(name_errors)}",
            rule_version=0,
        )
        return False, name_errors

    if not config_exists(old_name):
        log_operation(
            operation='config_rename_failed',
            operator=operator,
            details=f"重命名配置失败：源配置 [{old_name}] 不存在",
            rule_version=0,
        )
        return False, [f"配置 [{old_name}] 不存在"]

    if config_exists(new_name):
        log_operation(
            operation='config_rename_failed',
            operator=operator,
            details=f"重命名配置失败：目标名称 [{new_name}] 已被占用",
            rule_version=0,
        )
        return False, [f"配置 [{new_name}] 已存在，无法重命名为该名称"]

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE export_configs SET config_name = ?, updated_at = datetime(\'now\') WHERE config_name = ?',
        (new_name, old_name)
    )
    conn.commit()
    conn.close()

    log_operation(
        operation='config_rename',
        operator=operator,
        details=f"重命名配置：[{old_name}] -> [{new_name}]",
        rule_version=0,
    )

    return True, []


def delete_config(config_name: str, operator: str = "system") -> Tuple[bool, List[str]]:
    """删除配置方案

    - 不能删除 default 默认方案
    - 如果删除的是激活方案，需要重新将 default 设为激活
    - 删除需确认（调用方负责提示，这里只执行删除）
    """
    init_config_table()

    if config_name == "default":
        log_operation(
            operation='config_delete_failed',
            operator=operator,
            details="删除配置失败：不能删除 default 默认方案",
            rule_version=0,
        )
        return False, ["不能删除 default 默认方案"]

    if not config_exists(config_name):
        log_operation(
            operation='config_delete_failed',
            operator=operator,
            details=f"删除配置失败：配置 [{config_name}] 不存在",
            rule_version=0,
        )
        return False, [f"配置 [{config_name}] 不存在"]

    was_active = get_active_config_name() == config_name

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM export_configs WHERE config_name = ?', (config_name,))
    conn.commit()
    conn.close()

    if was_active:
        _ensure_default_active()

    log_operation(
        operation='config_delete',
        operator=operator,
        details=f"删除配置方案 [{config_name}]{'（原激活方案，已切回 default）' if was_active else ''}",
        rule_version=0,
    )

    return True, []


def load_active_config() -> Tuple[Optional[ExportConfig], List[str]]:
    """加载当前激活的配置

    如果没有激活的配置，尝试使用 default；如果 default 也没有，返回默认配置
    """
    init_config_table()
    _ensure_default_active()

    active_name = get_active_config_name()
    if active_name:
        return load_config(active_name)

    default = ExportConfig()
    return default, ["未找到任何配置，已使用默认导出配置"]
