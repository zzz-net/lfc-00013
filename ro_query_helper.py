"""只读查询辅助脚本：release_orders / release_order_history / audit_logs

不修改任何状态，仅用于 E2E 测试中核对 SQLite 数据一致性。
所有函数都是纯查询，不做 INSERT/UPDATE/DELETE。
"""
import os
import sqlite3
from typing import List, Dict, Any, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "corpus.db")


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def query_release_orders(name: Optional[str] = None,
                          order_id: Optional[int] = None,
                          status: Optional[str] = None) -> List[Dict[str, Any]]:
    """只读查询 release_orders 表。"""
    conn = _connect()
    cur = conn.cursor()

    sql = "SELECT * FROM release_orders WHERE 1=1"
    params = []
    if name is not None:
        sql += " AND name = ?"
        params.append(name)
    if order_id is not None:
        sql += " AND id = ?"
        params.append(order_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC"

    cur.execute(sql, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def query_release_order_history(order_id: int) -> List[Dict[str, Any]]:
    """只读查询 release_order_history 表（按指定 order_id）。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM release_order_history WHERE release_order_id = ? ORDER BY id DESC",
        (order_id,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def query_audit_logs(operation: Optional[str] = None,
                     detail_contains: Optional[str] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
    """只读查询 audit_logs 表。"""
    conn = _connect()
    cur = conn.cursor()

    sql = "SELECT * FROM audit_logs WHERE 1=1"
    params = []
    if operation is not None:
        sql += " AND operation = ?"
        params.append(operation)
    if detail_contains is not None:
        sql += " AND details LIKE ?"
        params.append(f"%{detail_contains}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cur.execute(sql, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def query_export_configs(config_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """只读查询 export_configs 表。"""
    conn = _connect()
    cur = conn.cursor()
    if config_name:
        cur.execute(
            "SELECT * FROM export_configs WHERE config_name = ?",
            (config_name,)
        )
    else:
        cur.execute("SELECT * FROM export_configs ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def query_rule_versions() -> List[Dict[str, Any]]:
    """只读查询 rule_versions 表。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rule_versions ORDER BY version DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def count_release_orders_by_name(name: str) -> int:
    """统计同名发布单数量（只读）。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM release_orders WHERE name = ?", (name,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def count_audit_ops(operation: str) -> int:
    """统计指定操作的审计日志条数（只读）。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM audit_logs WHERE operation = ?", (operation,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def latest_audit_log(operation: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """获取最新一条审计日志（只读）。"""
    logs = query_audit_logs(operation=operation, limit=1)
    return logs[0] if logs else None
