"""导出配置发布单管理模块

支持：
- 新建发布单（草稿）
- 查看配置差异
- 锁定/审批
- 正式发布（含冲突检测）
- 撤销到上一版
- 导入导出 JSON
- 权限控制（管理员才能发布和撤销）
- 审计日志
- 数据持久化（重启可恢复）

统一导入入口：
  _prepare_import_context   - 解析文件并返回结构化上下文
  _check_import_conflicts   - 三类冲突统一校验（目标配置已存在/激活配置漂移/规则版本落后）
  _persist_imported_order   - 落库 + 历史 + 审计（供 create/import 等多处复用）
  import_release_order      - 对外统一入口，串起以上三步
"""
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional, Dict, Tuple, Any

from .database import get_connection
from .models import ReleaseOrder, ReleaseOrderHistory
from .audit import log_operation
from . import export_config as ec
from . import rules


RELEASE_ORDER_SCHEMA_VERSION = 1


ADMIN_ROLES = {"admin", "administrator"}


@dataclass
class ImportContext:
    """导入过程的结构化上下文，用于在解析、校验、落库各阶段传递数据。"""
    raw_data: Dict[str, Any]
    order_name: str
    source_config_name: str
    target_config_name: str
    description: str
    rule_version: int
    config: "ec.ExportConfig"
    config_json: str
    approver: Optional[str] = None
    created_by: str = ""
    created_at: str = ""
    approved_at: Optional[str] = None
    published_at: Optional[str] = None
    history_data: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.history_data is None:
            self.history_data = []


def _is_admin(operator: str) -> bool:
    return operator.lower() in ADMIN_ROLES


def _get_current_rule_version() -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 1


def _get_config_hash(config_name: str) -> Tuple[Optional[str], Optional[str]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT config_json, updated_at FROM export_configs WHERE config_name = ?",
        (config_name,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, None


def _compute_diff(config1_json: str, config2_json: str) -> Dict[str, Any]:
    try:
        cfg1 = json.loads(config1_json)
    except (json.JSONDecodeError, TypeError):
        cfg1 = {}
    try:
        cfg2 = json.loads(config2_json)
    except (json.JSONDecodeError, TypeError):
        cfg2 = {}

    all_keys = set(cfg1.keys()) | set(cfg2.keys())
    changes = []
    additions = []
    deletions = []

    for key in sorted(all_keys):
        v1 = cfg1.get(key)
        v2 = cfg2.get(key)
        if key not in cfg1:
            additions.append({"field": key, "new_value": v2})
        elif key not in cfg2:
            deletions.append({"field": key, "old_value": v1})
        elif v1 != v2:
            changes.append({"field": key, "old_value": v1, "new_value": v2})

    return {
        "changes": changes,
        "additions": additions,
        "deletions": deletions,
        "has_diff": len(changes) + len(additions) + len(deletions) > 0,
    }


def _validate_order_name(name: str) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if not name or not isinstance(name, str):
        errors.append("发布单名称不能为空")
        return False, errors
    name = name.strip()
    if len(name) == 0:
        errors.append("发布单名称不能为空")
        return False, errors
    if len(name) > 64:
        errors.append(f"发布单名称过长（最多 64 字符，当前 {len(name)} 字符）")
    if name.startswith("."):
        errors.append("发布单名称不能以点号开头")
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        errors.append(
            f"发布单名称 [{name}] 包含非法字符，只能使用字母、数字、下划线、中划线、点号"
        )
    return len(errors) == 0, errors


def _add_history(order_id: int, action: str, operator: str, details: str,
                from_config: Optional[str] = None, to_config: Optional[str] = None) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO release_order_history
        (release_order_id, action, operator, details, from_config_json, to_config_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (order_id, action, operator, details, from_config, to_config,
          datetime.now().isoformat()))
    conn.commit()
    conn.close()


def _update_order_status(order_id: int, status: str, **kwargs) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    fields = ["status = ?"]
    params = [status]
    for key, value in kwargs.items():
        fields.append(f"{key} = ?")
        params.append(value)
    params.append(order_id)
    cursor.execute(
        f"UPDATE release_orders SET {', '.join(fields)} WHERE id = ?",
        tuple(params)
    )
    conn.commit()
    conn.close()


def create_release_order(name: str, source_config_name: str, target_config_name: str,
                      description: str = "", operator: str = "system") -> ReleaseOrder:
    """创建发布单。

    注意：创建发布单仅校验"发布单名称"和"源/目标配置合法性"，
    不做三类冲突拦截（目标配置已存在/激活配置漂移/规则版本落后）。
    这些冲突留到发布（publish_release_order）阶段统一拦截。
    导入（import_release_order）阶段则会执行完整的三类冲突拦截。
    """
    valid, name_errors = _validate_order_name(name)
    if not valid:
        log_operation(
            operation='release_order_create_failed',
            operator=operator,
            details=f"创建发布单失败：名称非法 - {'; '.join(name_errors)}",
            rule_version=0,
        )
        raise ValueError("; ".join(name_errors))

    if not ec.config_exists(source_config_name):
        log_operation(
            operation='release_order_create_failed',
            operator=operator,
            details=f"创建发布单失败：源配置 [{source_config_name}] 不存在",
            rule_version=0,
        )
        raise ValueError(f"源配置 [{source_config_name}] 不存在")

    valid, target_errors = ec.validate_config_name(target_config_name)
    if not valid:
        log_operation(
            operation='release_order_create_failed',
            operator=operator,
            details=f"创建发布单失败：目标配置名称非法 - {'; '.join(target_errors)}",
            rule_version=0,
        )
        raise ValueError("; ".join(target_errors))

    src_cfg, _ = ec.load_config(source_config_name)
    if src_cfg is None:
        raise ValueError(f"无法加载源配置 [{source_config_name}]")

    rule_version = _get_current_rule_version()

    ctx = ImportContext(
        raw_data={},
        order_name=name,
        source_config_name=source_config_name,
        target_config_name=target_config_name,
        description=description,
        rule_version=rule_version,
        config=src_cfg,
        config_json=src_cfg.to_json(),
        created_by=operator,
        created_at=datetime.now().isoformat(),
    )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM release_orders WHERE name = ?", (name,))
    if cursor.fetchone()[0] > 0:
        conn.close()
        log_operation(
            operation='release_order_create_failed',
            operator=operator,
            details=f"创建发布单失败：发布单 [{name}] 已存在",
            rule_version=0,
        )
        raise ValueError(f"发布单名称 [{name}] 已存在")
    conn.close()

    return _persist_imported_order(ctx, operator=operator, force=False, source_file=None)


def list_release_orders(status: Optional[str] = None) -> List[ReleaseOrder]:
    conn = get_connection()
    cursor = conn.cursor()
    if status:
        cursor.execute('''
            SELECT id, name, description, status, source_config_name, target_config_name,
                   config_json, rule_version, approver, created_by, created_at,
                   approved_at, published_at
            FROM release_orders WHERE status = ? ORDER BY id DESC
        ''', (status,))
    else:
        cursor.execute('''
            SELECT id, name, description, status, source_config_name, target_config_name,
                   config_json, rule_version, approver, created_by, created_at,
                   approved_at, published_at
            FROM release_orders ORDER BY id DESC
        ''')
    rows = cursor.fetchall()
    conn.close()
    return [ReleaseOrder.from_row(row) for row in rows]


def get_release_order(order_id: int) -> ReleaseOrder:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, description, status, source_config_name, target_config_name,
               config_json, rule_version, approver, created_by, created_at,
               approved_at, published_at
        FROM release_orders WHERE id = ?
    ''', (order_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"发布单 ID={order_id} 不存在")
    return ReleaseOrder.from_row(row)


def get_release_order_by_name(name: str) -> ReleaseOrder:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, description, status, source_config_name, target_config_name,
               config_json, rule_version, approver, created_by, created_at,
               approved_at, published_at
        FROM release_orders WHERE name = ?
    ''', (name,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"发布单 [{name}] 不存在")
    return ReleaseOrder.from_row(row)


def get_order_history(order_id: int) -> List[ReleaseOrderHistory]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, release_order_id, action, operator, details,
               from_config_json, to_config_json, created_at
        FROM release_order_history WHERE release_order_id = ? ORDER BY id DESC
    ''', (order_id,))
    rows = cursor.fetchall()
    conn.close()
    return [ReleaseOrderHistory.from_row(row) for row in rows]


def update_draft_config(order_id: int, field_policies: Optional[Dict[str, str]] = None,
                        format: Optional[str] = None,
                        include_review_summary: Optional[bool] = None,
                        operator: str = "system") -> ReleaseOrder:
    order = get_release_order(order_id)
    if order.status != "draft":
        raise ValueError(f"只有草稿状态的发布单才能修改配置，当前状态=[{order.status}]")

    try:
        cfg = ec.ExportConfig.from_json(order.config_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"发布单配置 JSON 解析失败: {e}")

    if field_policies is not None:
        for field, policy in field_policies.items():
            if field in ec.ALL_EXPORTABLE_FIELDS:
                cfg.field_policies[field] = policy

    if format is not None:
        cfg.format = format

    if include_review_summary is not None:
        cfg.include_review_summary = include_review_summary
        cfg.field_policies["review_summary"] = (
            ec.FieldPolicy.KEEP.value if include_review_summary
            else ec.FieldPolicy.DROP.value
        )

    valid, errors = cfg.validate()
    fatal = [e for e in errors if "安全提示" not in e and "兼容提示" not in e]
    if fatal:
        raise ValueError("配置验证失败: " + "; ".join(fatal))

    old_config = order.config_json
    new_config = cfg.to_json()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE release_orders SET config_json = ? WHERE id = ?",
        (new_config, order_id)
    )
    conn.commit()
    conn.close()

    diff = _compute_diff(old_config, new_config)
    if diff["has_diff"]:
        change_desc = []
        for c in diff["changes"]:
            change_desc.append(f"{c['field']}: {c['old_value']} -> {c['new_value']}")
        for a in diff["additions"]:
            change_desc.append(f"新增 {a['field']}: {a['new_value']}")
        for d in diff["deletions"]:
            change_desc.append(f"删除 {d['field']}")
        _add_history(
            order_id, "update_config", operator,
            f"修改草稿配置: {'; '.join(change_desc)}",
            from_config=old_config, to_config=new_config
        )

    log_operation(
        operation='release_order_update',
        operator=operator,
        details=f"修改发布单 [{order.name}] 草稿配置",
        rule_version=order.rule_version,
    )

    return get_release_order(order_id)


def get_config_diff(order_id: int) -> Dict[str, Any]:
    order = get_release_order(order_id)

    source_config, _ = _get_config_hash(order.source_config_name)
    if source_config is None:
        raise ValueError(f"源配置 [{order.source_config_name}] 不存在")

    return _compute_diff(source_config, order.config_json)


def lock_release_order(order_id: int, operator: str = "system") -> ReleaseOrder:
    order = get_release_order(order_id)
    if order.status != "draft":
        raise ValueError(f"只有草稿状态的发布单才能锁定，当前状态=[{order.status}]")

    _update_order_status(order_id, "locked")

    _add_history(order_id, "lock", operator, "锁定发布单，等待审批")

    log_operation(
        operation='release_order_lock',
        operator=operator,
        details=f"锁定发布单 [{order.name}]，等待审批",
        rule_version=order.rule_version,
    )

    return get_release_order(order_id)


def approve_release_order(order_id: int, operator: str = "system") -> ReleaseOrder:
    if not _is_admin(operator):
        log_operation(
            operation='release_order_approve_failed',
            operator=operator,
            details=f"审批发布单失败：操作人 [{operator}] 无管理员权限",
            rule_version=0,
        )
        raise ValueError(f"只有管理员才能审批发布单，当前操作人=[{operator}]")

    order = get_release_order(order_id)
    if order.status != "locked":
        raise ValueError(f"只有锁定状态的发布单才能审批，当前状态=[{order.status}]")

    _update_order_status(
        order_id, "approved",
        approver=operator,
        approved_at=datetime.now().isoformat()
    )

    _add_history(order_id, "approve", operator, "审批通过发布单")

    log_operation(
        operation='release_order_approve',
        operator=operator,
        details=f"审批通过发布单 [{order.name}]",
        rule_version=order.rule_version,
    )

    return get_release_order(order_id)


def _check_publish_conflicts(order: ReleaseOrder) -> List[str]:
    conflicts: List[str] = []

    if ec.config_exists(order.target_config_name):
        conflicts.append(
            f"目标配置 [{order.target_config_name}] 已存在，发布将覆盖已有配置"
        )

    active_name = ec.get_active_config_name()
    if active_name == order.target_config_name:
        current_config, current_updated = _get_config_hash(order.target_config_name)
        if current_config is not None:
            diff = _compute_diff(current_config, order.config_json)
            if diff["has_diff"]:
                conflicts.append(
                    f"当前激活配置 [{order.target_config_name}] 已被修改，与发布单配置存在差异"
                )

    current_rule_version = _get_current_rule_version()
    if current_rule_version != order.rule_version:
        conflicts.append(
            f"规则版本不一致：发布单创建时规则版本=v{order.rule_version}，"
            f"当前规则版本=v{current_rule_version}"
        )

    return conflicts


def publish_release_order(order_id: int, force: bool = False,
                       operator: str = "system") -> Dict[str, Any]:
    if not _is_admin(operator):
        log_operation(
            operation='release_order_publish_failed',
            operator=operator,
            details=f"发布失败：操作人 [{operator}] 无管理员权限",
            rule_version=0,
        )
        raise ValueError(f"只有管理员才能发布配置，当前操作人=[{operator}]")

    order = get_release_order(order_id)
    if order.status != "approved":
        raise ValueError(
            f"只有审批通过状态的发布单才能发布，当前状态=[{order.status}]"
        )

    conflicts = _check_publish_conflicts(order)

    rule_version_conflict = any("规则版本不一致" in c for c in conflicts)
    if rule_version_conflict:
        log_operation(
            operation='release_order_publish_failed',
            operator=operator,
            details=(
                f"发布 [{order.name}] 被规则版本冲突拦截（不可强制发布）："
                f"{'; '.join(c for c in conflicts if '规则版本不一致' in c)}"
            ),
            rule_version=order.rule_version,
        )
        raise ValueError(
            "发布被拦截，规则版本不一致（发布单创建后规则已变更），"
            "请重新创建发布单后再发布"
        )

    other_conflicts = [c for c in conflicts if "规则版本不一致" not in c]
    if other_conflicts and not force:
        log_operation(
            operation='release_order_publish_failed',
            operator=operator,
            details=(
                f"发布 [{order.name}] 被冲突拦截：{'; '.join(other_conflicts)}"
            ),
            rule_version=order.rule_version,
        )
        raise ValueError(
            "发布被拦截，存在以下冲突：\n  - " + "\n  - ".join(other_conflicts) +
            "\n如需强制发布，请指定 force=True"
        )

    try:
        cfg = ec.ExportConfig.from_json(order.config_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"发布单配置 JSON 解析失败: {e}")

    old_config_json, _ = _get_config_hash(order.target_config_name)

    ok, save_errors, save_warnings = ec.save_config(
        cfg, order.target_config_name, operator
    )
    if not ok:
        log_operation(
            operation='release_order_publish_failed',
            operator=operator,
            details=f"发布 [{order.name}] 保存配置失败: {'; '.join(save_errors)}",
            rule_version=order.rule_version,
        )
        raise ValueError("保存配置失败: " + "; ".join(save_errors))

    _update_order_status(
        order_id, "published",
        published_at=datetime.now().isoformat()
    )

    _add_history(
        order_id, "publish", operator,
        f"发布配置到 [{order.target_config_name}]",
        from_config=old_config_json,
        to_config=order.config_json
    )

    log_operation(
        operation='release_order_publish',
        operator=operator,
        details=(
            f"发布单 [{order.name}] 发布成功，"
            f"目标配置=[{order.target_config_name}]"
        ),
        rule_version=order.rule_version,
    )

    return {
        "order_name": order.name,
        "target_config": order.target_config_name,
        "warnings": save_warnings,
        "conflicts_force": force and conflicts,
    }


def revert_release_order(order_id: int, operator: str = "system") -> Dict[str, Any]:
    if not _is_admin(operator):
        log_operation(
            operation='release_order_revert_failed',
            operator=operator,
            details=f"撤销失败：操作人 [{operator}] 无管理员权限",
            rule_version=0,
        )
        raise ValueError(f"只有管理员才能撤销发布，当前操作人=[{operator}]")

    order = get_release_order(order_id)
    if order.status != "published":
        raise ValueError(
            f"只有已发布状态的发布单才能撤销，当前状态=[{order.status}]"
        )

    history = get_order_history(order_id)
    publish_records = [h for h in history if h.action == "publish"]
    if not publish_records:
        raise ValueError("未找到发布记录，无法撤销")

    last_publish = publish_records[0]
    old_config_json = last_publish.from_config_json

    if old_config_json is None:
        ec.delete_config(order.target_config_name, operator=operator)
        _update_order_status(order_id, "reverted")

        _add_history(
            order_id, "revert", operator,
            f"撤销发布，删除新创建的配置 [{order.target_config_name}]",
            from_config=order.config_json,
            to_config=None
        )

        log_operation(
            operation='release_order_revert',
            operator=operator,
            details=(
                f"撤销发布单 [{order.name}]，"
                f"删除新创建的配置 [{order.target_config_name}]"
            ),
            rule_version=order.rule_version,
        )

        return {
            "order_name": order.name,
            "target_config": order.target_config_name,
            "reverted_to": "配置已删除（发布前不存在）",
        }

    try:
        cfg = ec.ExportConfig.from_json(old_config_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"历史配置 JSON 解析失败: {e}")

    current_config_json, _ = _get_config_hash(order.target_config_name)
    if current_config_json is not None and current_config_json != order.config_json:
        diff = _compute_diff(current_config_json, order.config_json)
        if diff["has_diff"]:
            log_operation(
                operation='release_order_revert_failed',
                operator=operator,
                details=(
                    f"撤销 [{order.name}] 被拦截：目标配置已被其他人修改"
                ),
                rule_version=order.rule_version,
            )
            raise ValueError(
                "撤销被拦截：目标配置已被其他人修改，为避免数据丢失，请先确认变更"
            )

    ok, save_errors, _ = ec.save_config(
        cfg, order.target_config_name, operator
    )
    if not ok:
        log_operation(
            operation='release_order_revert_failed',
            operator=operator,
            details=f"撤销 [{order.name}] 恢复配置失败: {'; '.join(save_errors)}",
            rule_version=order.rule_version,
        )
        raise ValueError("恢复配置失败: " + "; ".join(save_errors))

    _update_order_status(order_id, "reverted")

    _add_history(
        order_id, "revert", operator,
        f"撤销发布，配置已回滚到发布前版本",
        from_config=order.config_json,
        to_config=old_config_json
    )

    log_operation(
        operation='release_order_revert',
        operator=operator,
        details=(
            f"撤销发布单 [{order.name}]，"
            f"目标配置=[{order.target_config_name}] 已回滚"
        ),
        rule_version=order.rule_version,
    )

    return {
        "order_name": order.name,
        "target_config": order.target_config_name,
        "reverted_to": "发布前版本",
    }


def export_release_order(order_id: int, file_path: str,
                        operator: str = "system") -> None:
    order = get_release_order(order_id)

    history = get_order_history(order_id)
    history_data = []
    for h in history:
        history_data.append({
            "action": h.action,
            "operator": h.operator,
            "details": h.details,
            "created_at": h.created_at,
        })

    export_data = {
        "schema_version": RELEASE_ORDER_SCHEMA_VERSION,
        "exported_at": datetime.now().isoformat(),
        "exported_by": operator,
        "order_info": {
            "name": order.name,
            "description": order.description,
            "status": order.status,
            "source_config_name": order.source_config_name,
            "target_config_name": order.target_config_name,
            "rule_version": order.rule_version,
            "approver": order.approver,
            "created_by": order.created_by,
            "created_at": order.created_at,
            "approved_at": order.approved_at,
            "published_at": order.published_at,
        },
        "config": json.loads(order.config_json),
        "history": history_data,
    }

    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    log_operation(
        operation='release_order_export',
        operator=operator,
        details=f"导出发布单 [{order.name}] 到 {os.path.basename(file_path)}",
        rule_version=order.rule_version,
    )


def _prepare_import_context(file_path: str, rename_to: Optional[str] = None,
                          operator: str = "system") -> ImportContext:
    """统一解析发布单文件，返回结构化 ImportContext。

    负责：
      - 文件存在性与编码检查
      - JSON 解析
      - schema 版本兼容性检查
      - 发布单名称、目标/源配置名称合法性
      - 配置数据解析与合法性校验
    不负责：
      - 与当前数据库状态相关的冲突校验（交给 _check_import_conflicts）
      - 落库与审计（交给 _persist_imported_order）
    """
    if not os.path.exists(file_path):
        raise ValueError(f"发布单文件不存在: {file_path}")

    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"发布单文件读取失败: {e}")

    schema_version = data.get("schema_version")
    if schema_version != RELEASE_ORDER_SCHEMA_VERSION:
        raise ValueError(
            f"发布单文件 schema 版本不兼容：文件版本={schema_version}，当前支持版本={RELEASE_ORDER_SCHEMA_VERSION}"
        )

    order_info = data.get("order_info", {})
    name = rename_to or order_info.get("name")
    if not name:
        raise ValueError("发布单文件缺少名称信息")

    valid, name_errors = _validate_order_name(name)
    if not valid:
        raise ValueError("; ".join(name_errors))

    target_config_name = order_info.get("target_config_name", "")
    valid, target_errors = ec.validate_config_name(target_config_name)
    if not valid:
        raise ValueError("; ".join(target_errors))

    source_config_name = order_info.get("source_config_name", "")
    if not ec.config_exists(source_config_name):
        raise ValueError(f"源配置 [{source_config_name}] 不存在")

    config_data = data.get("config")
    if config_data is None:
        raise ValueError("发布单文件缺少 config 数据")

    try:
        cfg = ec.ExportConfig.from_dict(config_data)
    except Exception as e:
        raise ValueError(f"配置数据解析失败: {e}")

    valid, errors = cfg.validate()
    fatal = [e for e in errors if "安全提示" not in e and "兼容提示" not in e]
    if fatal:
        raise ValueError("配置验证失败: " + "; ".join(fatal))

    rule_version = order_info.get("rule_version", _get_current_rule_version())
    config_json = cfg.to_json()

    history_data = data.get("history", []) or []

    return ImportContext(
        raw_data=data,
        order_name=name,
        source_config_name=source_config_name,
        target_config_name=target_config_name,
        description=order_info.get("description", ""),
        rule_version=rule_version,
        config=cfg,
        config_json=config_json,
        approver=order_info.get("approver"),
        created_by=order_info.get("created_by", operator),
        created_at=order_info.get("created_at", datetime.now().isoformat()),
        approved_at=order_info.get("approved_at"),
        published_at=order_info.get("published_at"),
        history_data=history_data,
    )


def _check_import_conflicts(ctx: ImportContext) -> List[str]:
    """三类冲突统一校验：同名发布单、目标配置已存在、激活配置漂移、规则版本落后。

    返回冲突描述列表。若列表非空，说明存在冲突，调用方应拒绝导入。

    注意：
      - 此函数只做纯校验，不修改任何状态。
      - "同名发布单"冲突允许调用方通过 force=True 绕过（由 _persist_imported_order 处理），
        但"目标配置已存在/激活配置漂移/规则版本落后"三类一律拒绝，不可强制。
    """
    conflicts: List[str] = []

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM release_orders WHERE name = ?", (ctx.order_name,))
    if cursor.fetchone()[0] > 0:
        conflicts.append(f"发布单 [{ctx.order_name}] 已存在")

    if ec.config_exists(ctx.target_config_name):
        conflicts.append(
            f"目标配置 [{ctx.target_config_name}] 已存在，"
            f"导入被拦截（不可强制，请更换目标配置名称）"
        )

    active_name = ec.get_active_config_name()
    if active_name == ctx.target_config_name:
        current_config, _ = _get_config_hash(ctx.target_config_name)
        if current_config is not None:
            diff = _compute_diff(current_config, ctx.config_json)
            if diff["has_diff"]:
                conflicts.append(
                    f"当前激活配置 [{ctx.target_config_name}] 已被修改，与发布单配置存在差异，"
                    f"导入被拦截（不可强制）"
                )

    current_rule_version = _get_current_rule_version()
    if ctx.rule_version < current_rule_version:
        conflicts.append(
            f"规则版本落后：发布单规则版本=v{ctx.rule_version}，"
            f"当前规则版本=v{current_rule_version}，"
            f"导入被拦截（不可强制，请重新导出发布单）"
        )

    conn.close()
    return conflicts


def _persist_imported_order(ctx: ImportContext, operator: str,
                           force: bool = False,
                           source_file: Optional[str] = None) -> ReleaseOrder:
    """统一落库入口：写入 release_orders、写入历史、记录审计日志。

    参数：
      ctx        - 由 _prepare_import_context 返回的结构化上下文
      operator   - 当前操作人
      force      - 是否允许覆盖同名发布单（仅对"同名"冲突生效；其他三类冲突
                   必须已由 _check_import_conflicts 排除）
      source_file- 源文件名，用于审计日志（可选）
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM release_orders WHERE name = ?", (ctx.order_name,))
    name_exists = cursor.fetchone()[0] > 0

    if name_exists and not force:
        conn.close()
        raise ValueError(
            f"发布单 [{ctx.order_name}] 已存在，如需覆盖请指定 force=True"
        )

    if force:
        cursor.execute("DELETE FROM release_orders WHERE name = ?", (ctx.order_name,))

    cursor.execute('''
        INSERT INTO release_orders
        (name, description, status, source_config_name, target_config_name,
         config_json, rule_version, approver, created_by, created_at,
         approved_at, published_at)
        VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        ctx.order_name,
        ctx.description,
        ctx.source_config_name,
        ctx.target_config_name,
        ctx.config_json,
        ctx.rule_version,
        ctx.approver,
        ctx.created_by,
        ctx.created_at,
        ctx.approved_at,
        ctx.published_at,
    ))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()

    file_desc = f"从文件 {os.path.basename(source_file)} 导入发布单" if source_file else "创建发布单"
    _add_history(
        order_id, "import" if source_file else "create", operator,
        f"{file_desc}，源配置=[{ctx.source_config_name}], 目标配置=[{ctx.target_config_name}]",
        to_config=ctx.config_json
    )

    log_operation(
        operation='release_order_import' if source_file else 'release_order_create',
        operator=operator,
        details=(
            f"{'导入' if source_file else '创建'}发布单 [{ctx.order_name}]："
            f"源配置=[{ctx.source_config_name}], "
            f"目标配置=[{ctx.target_config_name}]"
        ),
        rule_version=ctx.rule_version,
    )

    return get_release_order(order_id)


def import_release_order(file_path: str, operator: str = "system",
                     rename_to: Optional[str] = None,
                     force: bool = False) -> ReleaseOrder:
    """对外统一导入入口：串起解析 → 冲突校验 → 落库。

    冲突策略：
      - 同名发布单：force=True 可覆盖
      - 目标配置已存在：一律拒绝
      - 激活配置被他人改动：一律拒绝
      - 规则版本落后：一律拒绝
    被拒绝时不会落库任何 draft 数据。
    """
    ctx = _prepare_import_context(file_path, rename_to=rename_to, operator=operator)

    conflicts = _check_import_conflicts(ctx)

    hard_conflicts = [c for c in conflicts if "已存在" not in c or "目标配置" in c]
    name_conflict = any(c.startswith(f"发布单 [{ctx.order_name}] 已存在") for c in conflicts)

    if name_conflict and force:
        hard_conflicts = [c for c in hard_conflicts if not c.startswith(f"发布单 [{ctx.order_name}] 已存在")]

    if hard_conflicts:
        log_operation(
            operation='release_order_import_failed',
            operator=operator,
            details=(
                f"导入发布单 [{ctx.order_name}] 被冲突拦截：{'; '.join(hard_conflicts)}"
            ),
            rule_version=ctx.rule_version,
        )
        raise ValueError(
            "导入被拦截，存在以下冲突：\n  - " + "\n  - ".join(hard_conflicts)
        )

    return _persist_imported_order(ctx, operator=operator, force=force, source_file=file_path)


def delete_release_order(order_id: int, operator: str = "system") -> None:
    order = get_release_order(order_id)

    if order.status in ("approved", "published"):
        raise ValueError(
            f"不能删除 [{order.status}] 状态的发布单，"
            f"请先撤销或等待发布完成"
        )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM release_orders WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    _add_history(order_id, "delete", operator, "删除发布单")

    log_operation(
        operation='release_order_delete',
        operator=operator,
        details=f"删除发布单 [{order.name}]",
        rule_version=order.rule_version,
    )
