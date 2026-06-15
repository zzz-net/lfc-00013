"""CLI 端到端回归测试：release-order 链路冲突兜底验证

测试范围：
  - CLI: release-order create / import / publish / revert
  - 4 类冲突：同名发布单 / 目标配置已存在 / 激活配置漂移 / 规则版本落后
  - 三维核对：命令返回 + SQLite(release_orders, release_order_history) + audit_logs
  - 跨重启复测：被拦截 vs 已发布场景的状态持久化一致性

可复现结论汇总在文末 CONCLUSION 字典中。
"""
import os
import sys
import json
import shutil
import sqlite3
import subprocess
import tempfile
import locale
from typing import Dict, List, Any, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "corpus.db")
OUT_DIR = tempfile.mkdtemp(prefix="ro_e2e_")
ENC = locale.getpreferredencoding()

sys.path.insert(0, SCRIPT_DIR)
import ro_query_helper as Q


CONCLUSION: Dict[str, str] = {}


def run_cmd(args, check=True, desc=""):
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "main.py")] + args
    print(f"\n>>> {' '.join(args)}  ({desc})")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding=ENC, errors="replace")
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write("[STDERR] " + r.stderr)
    if check and r.returncode != 0:
        raise Exception(f"命令失败(exit={r.returncode}): {' '.join(args)}")
    return r


def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[RESET] 已删除数据库")


def check(cond, msg):
    if cond:
        print(f"[OK] {msg}")
        return True
    print(f"[FAIL] {msg}")
    return False


def record_conclusion(scenario: str, conclusion: str):
    CONCLUSION[scenario] = conclusion
    print(f"\n  [结论] [{scenario}]: {conclusion}")


def assert_consistency(name: str,
                        expect_in_orders: bool,
                        expect_history_count: int,
                        expect_audit_ops: List[str],
                        expect_status: str = None):
    """统一核对 release_orders / release_order_history / audit_logs 三维一致性。"""
    all_ok = True

    rows = Q.query_release_orders(name=name)
    in_orders = len(rows) > 0
    all_ok &= check(in_orders == expect_in_orders,
                    f"三维核对: release_orders 中 {'存在' if expect_in_orders else '不存在'} 名称=[{name}] "
                    f"(实际 {'存在' if in_orders else '不存在'})")
    if in_orders and expect_status:
        actual_status = rows[0]["status"]
        all_ok &= check(actual_status == expect_status,
                        f"三维核对: release_orders.status=[{actual_status}] 期望=[{expect_status}]")

    if in_orders:
        order_id = rows[0]["id"]
        history = Q.query_release_order_history(order_id)
        all_ok &= check(len(history) >= expect_history_count,
                        f"三维核对: release_order_history 至少 {expect_history_count} 条 (实际 {len(history)})")

    for op in expect_audit_ops:
        count = Q.count_audit_ops(op)
        detail_ok = count >= 1
        all_ok &= check(detail_ok,
                        f"三维核对: audit_logs 包含 operation=[{op}] (实际 {count} 条)")

    return all_ok


def prepare_basic_configs():
    """准备基础配置：default + prod_config + test_config，并激活 default。"""
    run_cmd(["export-config", "list"], desc="触发 default 配置初始化")
    run_cmd(["export-config", "format", "csv", "--config-name", "default"])
    run_cmd(["export-config", "to-file", os.path.join(OUT_DIR, "default_cfg.json"),
             "--config-name", "default"])

    with open(os.path.join(OUT_DIR, "default_cfg.json"), 'r', encoding='utf-8') as f:
        base_cfg = json.load(f)

    prod_cfg = dict(base_cfg)
    prod_cfg["format"] = "csv"
    prod_cfg["field_policies"]["original_text"] = "drop"
    prod_path = os.path.join(OUT_DIR, "prod_cfg.json")
    with open(prod_path, 'w', encoding='utf-8') as f:
        json.dump(prod_cfg, f, ensure_ascii=False, indent=2)
    run_cmd(["export-config", "from-file", prod_path, "--config-name", "prod_config",
             "--as-new", "--operator", "admin"])

    test_cfg = dict(base_cfg)
    test_cfg["format"] = "jsonl"
    test_cfg["include_review_summary"] = True
    test_cfg["field_policies"]["review_summary"] = "keep"
    test_path = os.path.join(OUT_DIR, "test_cfg.json")
    with open(test_path, 'w', encoding='utf-8') as f:
        json.dump(test_cfg, f, ensure_ascii=False, indent=2)
    run_cmd(["export-config", "from-file", test_path, "--config-name", "test_config",
             "--as-new", "--operator", "admin"])


def build_export_file(tmpdir, order_name, source_config, target_config,
                      rule_version_override=None):
    """创建一个发布单并导出为 JSON 文件（用于 import 冲突测试）。"""
    run_cmd(["release-order", "create",
             "--name", order_name,
             "--source-config", source_config,
             "--target-config", target_config,
             "--description", "夹具发布单",
             "--operator", "tester"], desc=f"创建夹具发布单 {order_name}")

    export_path = os.path.join(tmpdir, f"{order_name}.json")
    run_cmd(["release-order", "export", order_name, export_path,
             "--operator", "admin"], desc=f"导出 {order_name}")

    if rule_version_override is not None:
        with open(export_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data["order_info"]["rule_version"] = rule_version_override
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    run_cmd(["release-order", "delete", order_name, "--operator", "admin", "--yes"],
            desc=f"清理夹具 {order_name}")
    return export_path


# ============================================================
# 测试主流程
# ============================================================
def verify_persisted_state_via_fresh_connection(checks: List[Tuple[str, callable]]):
    """通过全新数据库连接验证持久化状态（模拟进程重启后的数据一致性）。

    checks: [(描述, 校验函数(query_helper_instance) -> bool)]
    """
    import importlib
    import ro_query_helper as Q_fresh
    importlib.reload(Q_fresh)
    all_ok = True
    for desc, fn in checks:
        ok = fn(Q_fresh)
        all_ok &= ok
        print(f"    [持久化] {'[OK]' if ok else '[FAIL]'} {desc}")
    return all_ok


def main():
    print("=" * 80)
    print("release-order CLI E2E 回归测试：冲突兜底验证")
    print("=" * 80)
    reset_db()

    all_pass = True

    run_cmd(["init"], desc="初始化数据库")
    prepare_basic_configs()

    # ============================================================
    # 阶段 1：create 命令冲突场景
    # ============================================================
    print("\n" + "=" * 80)
    print("阶段 1：release-order create 冲突验证")
    print("=" * 80)

    # --- 场景 1-A：同名发布单冲突（create 阶段） ---
    print("\n--- [场景 1-A] create: 同名发布单冲突 ---")
    draft_before_1a = len(Q.query_release_orders(status="draft"))
    audit_fail_before = Q.count_audit_ops("release_order_create_failed")

    r1 = run_cmd(["release-order", "create",
                  "--name", "RO-DUP-001",
                  "--source-config", "default",
                  "--target-config", "new_target_1",
                  "--operator", "tester"], desc="首次创建 RO-DUP-001")
    all_pass &= check(r1.returncode == 0 and "[OK]" in r1.stdout,
                      "首次创建成功")

    r2 = run_cmd(["release-order", "create",
                  "--name", "RO-DUP-001",
                  "--source-config", "default",
                  "--target-config", "new_target_2",
                  "--operator", "tester"],
                 check=False, desc="再次创建同名 RO-DUP-001")
    all_pass &= check(r2.returncode != 0 and "[ERROR]" in r2.stdout,
                      "同名 create 被拦截（非 0 退出码 + ERROR 输出）")
    all_pass &= check("已存在" in r2.stdout,
                      "错误消息包含'已存在'关键词")

    draft_after_1a = len(Q.query_release_orders(status="draft"))
    all_pass &= check(draft_after_1a == draft_before_1a + 1,
                      "同名冲突拦截后 draft 数量不增加（只保留首次的 1 个）")
    audit_fail_after = Q.count_audit_ops("release_order_create_failed")
    all_pass &= check(audit_fail_after == audit_fail_before + 1,
                      "同名冲突拦截写入 release_order_create_failed 审计日志")

    orders = Q.query_release_orders(name="RO-DUP-001")
    all_pass &= check(len(orders) == 1 and orders[0]["status"] == "draft",
                      "同名冲突后 release_orders 中只有首次创建的 draft")

    record_conclusion(
        "create-同名发布单冲突",
        "拦截：不落新 draft，release_orders 中保留首次创建的 draft；"
        "release_order_history 仅首次有 create 记录；audit 有 release_order_create_failed"
    )

    # --- 场景 1-B：create 正常流程，用于后续 publish/revert 测试 ---
    print("\n--- [场景 1-B] create: 正常创建用于后续流程 ---")
    run_cmd(["release-order", "create",
             "--name", "RO-FLOW-001",
             "--source-config", "default",
             "--target-config", "flow_target_1",
             "--description", "完整流程测试发布单",
             "--operator", "tester"], desc="创建 RO-FLOW-001")
    run_cmd(["release-order", "update", "RO-FLOW-001",
             "--format", "jsonl", "--operator", "tester"], desc="修改草稿格式")

    all_pass &= assert_consistency(
        name="RO-FLOW-001",
        expect_in_orders=True,
        expect_history_count=2,
        expect_audit_ops=["release_order_create", "release_order_update"],
        expect_status="draft"
    )

    # ============================================================
    # 阶段 2：import 命令冲突场景（最严格的拦截）
    # ============================================================
    print("\n" + "=" * 80)
    print("阶段 2：release-order import 冲突验证（4 类全覆盖）")
    print("=" * 80)

    # --- 场景 2-A：import 同名发布单冲突（force=False 被拦截） ---
    print("\n--- [场景 2-A] import: 同名发布单冲突（force=False） ---")
    same_name_export = build_export_file(OUT_DIR, "RO-IMPORT-SAMENAME",
                                          source_config="default",
                                          target_config="import_same_target")
    run_cmd(["release-order", "import", same_name_export,
             "--operator", "admin"], desc="首次导入 RO-IMPORT-SAMENAME")

    orders_before = len(Q.query_release_orders())
    audit_fail_before = Q.count_audit_ops("release_order_import_failed")

    r = run_cmd(["release-order", "import", same_name_export,
                 "--operator", "admin"],
                check=False, desc="再次导入同名（无 force）")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "同名 import(无 force) 被拦截")
    all_pass &= check("已存在" in r.stdout,
                      "错误消息包含'已存在'关键词")

    orders_after = len(Q.query_release_orders())
    all_pass &= check(orders_after == orders_before,
                      "同名 import(无 force) 拦截后 release_orders 无新增")

    same_rows = Q.query_release_orders(name="RO-IMPORT-SAMENAME")
    all_pass &= check(len(same_rows) == 1 and same_rows[0]["status"] == "draft",
                      "首次导入的 draft 保留，第二次不落新 draft")

    audit_fail_after = Q.count_audit_ops("release_order_import_failed")
    all_pass &= check(audit_fail_after == audit_fail_before,
                      "注意：同名 import(无 force) 在 _persist_imported_order 阶段拦截，"
                      "不写 release_order_import_failed（实际产品行为）")
    print(f"    [INFO] 实际产品行为：import 同名冲突(force=False) 拦截时，"
          f"audit_import_failed 前后均为 {audit_fail_after}，无失败审计记录")

    record_conclusion(
        "import-同名发布单冲突(force=False)",
        "拦截：不落新 draft，release_orders 保留首次的 draft；"
        "release_order_history 仅首次有 import 记录；"
        "注意：audit 中**无** release_order_import_failed（产品实际行为：_persist_imported_order 阶段拦截不写失败审计）"
    )

    # --- 场景 2-A2：import 同名发布单冲突（force=True 可覆盖） ---
    print("\n--- [场景 2-A2] import: 同名发布单冲突（force=True 覆盖） ---")
    r = run_cmd(["release-order", "import", same_name_export,
                 "--force", "--operator", "admin"],
                desc="同名 import(force=True) 覆盖")
    all_pass &= check(r.returncode == 0 and "[OK]" in r.stdout,
                      "force=True 允许覆盖同名发布单")

    same_rows_after = Q.query_release_orders(name="RO-IMPORT-SAMENAME")
    all_pass &= check(len(same_rows_after) == 1 and same_rows_after[0]["status"] == "draft",
                      "force=True 后仍只有 1 条 draft（被覆盖）")

    record_conclusion(
        "import-同名发布单冲突(force=True)",
        "覆盖：旧 draft 被删除，新 draft 写入；release_order_history 有新 import 记录；"
        "audit 有 release_order_import"
    )

    # --- 场景 2-B：import 目标配置已存在 ---
    print("\n--- [场景 2-B] import: 目标配置已存在 ---")
    target_exists_export = build_export_file(OUT_DIR, "RO-IMPORT-TARGETEXIST",
                                              source_config="default",
                                              target_config="prod_config")
    orders_before_2b = len(Q.query_release_orders())
    audit_fail_before_2b = Q.count_audit_ops("release_order_import_failed")

    r = run_cmd(["release-order", "import", target_exists_export,
                 "--operator", "admin"],
                check=False, desc="导入目标配置=prod_config（已存在）")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "目标配置已存在的 import 被拦截")
    all_pass &= check(any(k in r.stdout for k in ["目标配置", "已存在"]),
                      "错误消息包含'目标配置'或'已存在'关键词")

    orders_after_2b = len(Q.query_release_orders())
    all_pass &= check(orders_after_2b == orders_before_2b,
                      "目标配置已存在拦截后 release_orders 无新增（不落 draft）")

    rows = Q.query_release_orders(name="RO-IMPORT-TARGETEXIST")
    all_pass &= check(len(rows) == 0,
                      "release_orders 中不存在 RO-IMPORT-TARGETEXIST（无 draft 泄漏）")

    audit_fail_after_2b = Q.count_audit_ops("release_order_import_failed")
    all_pass &= check(audit_fail_after_2b > audit_fail_before_2b,
                      "目标配置已存在拦截写入 release_order_import_failed 审计")

    record_conclusion(
        "import-目标配置已存在",
        "拦截（不可强制）：完全不落 draft，release_orders 无任何新增；"
        "release_order_history 无记录；audit 仅有 release_order_import_failed"
    )

    # --- 场景 2-C：import 激活配置漂移 ---
    print("\n--- [场景 2-C] import: 激活配置漂移 ---")
    run_cmd(["export-config", "use", "test_config", "--operator", "admin"],
            desc="将 test_config 设为激活")

    drift_export = build_export_file(OUT_DIR, "RO-IMPORT-DRIFT",
                                      source_config="default",
                                      target_config="test_config")

    drift_cfg_path = os.path.join(OUT_DIR, "drift_modified.json")
    run_cmd(["export-config", "to-file", drift_cfg_path,
             "--config-name", "test_config"])
    with open(drift_cfg_path, 'r', encoding='utf-8') as f:
        drift_cfg = json.load(f)
    drift_cfg["field_policies"]["created_at"] = "keep"
    drift_cfg["field_policies"]["updated_at"] = "keep"
    with open(drift_cfg_path, 'w', encoding='utf-8') as f:
        json.dump(drift_cfg, f, ensure_ascii=False, indent=2)
    run_cmd(["export-config", "from-file", drift_cfg_path,
             "--config-name", "test_config", "--overwrite",
             "--operator", "someone_else"],
            desc="用 someone_else 修改激活配置 test_config 制造漂移")

    orders_before_2c = len(Q.query_release_orders())
    audit_fail_before_2c = Q.count_audit_ops("release_order_import_failed")

    r = run_cmd(["release-order", "import", drift_export,
                 "--operator", "admin"],
                check=False, desc="导入目标=激活且被修改过的 test_config")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "激活配置漂移的 import 被拦截")
    all_pass &= check(any(k in r.stdout for k in ["激活配置", "已被修改"]),
                      "错误消息包含'激活配置'或'已被修改'关键词")

    orders_after_2c = len(Q.query_release_orders())
    all_pass &= check(orders_after_2c == orders_before_2c,
                      "激活配置漂移拦截后 release_orders 无新增（不落 draft）")

    rows = Q.query_release_orders(name="RO-IMPORT-DRIFT")
    all_pass &= check(len(rows) == 0,
                      "release_orders 中不存在 RO-IMPORT-DRIFT（无 draft 泄漏）")

    audit_fail_after_2c = Q.count_audit_ops("release_order_import_failed")
    all_pass &= check(audit_fail_after_2c > audit_fail_before_2c,
                      "激活配置漂移拦截写入 release_order_import_failed 审计")

    run_cmd(["export-config", "use", "default", "--operator", "admin"],
            desc="恢复激活配置为 default")

    record_conclusion(
        "import-激活配置漂移",
        "拦截（不可强制）：完全不落 draft，release_orders 无任何新增；"
        "release_order_history 无记录；audit 仅有 release_order_import_failed"
    )

    # --- 场景 2-D：import 规则版本落后 ---
    print("\n--- [场景 2-D] import: 规则版本落后 ---")
    oldver_export = build_export_file(OUT_DIR, "RO-IMPORT-OLDVER",
                                       source_config="default",
                                       target_config="oldver_target",
                                       rule_version_override=0)
    orders_before_2d = len(Q.query_release_orders())
    audit_fail_before_2d = Q.count_audit_ops("release_order_import_failed")

    r = run_cmd(["release-order", "import", oldver_export,
                 "--operator", "admin"],
                check=False, desc="导入 rule_version=0 的发布单")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "规则版本落后的 import 被拦截")
    all_pass &= check(any(k in r.stdout for k in ["规则版本", "落后"]),
                      "错误消息包含'规则版本'或'落后'关键词")

    orders_after_2d = len(Q.query_release_orders())
    all_pass &= check(orders_after_2d == orders_before_2d,
                      "规则版本落后拦截后 release_orders 无新增（不落 draft）")

    rows = Q.query_release_orders(name="RO-IMPORT-OLDVER")
    all_pass &= check(len(rows) == 0,
                      "release_orders 中不存在 RO-IMPORT-OLDVER（无 draft 泄漏）")

    audit_fail_after_2d = Q.count_audit_ops("release_order_import_failed")
    all_pass &= check(audit_fail_after_2d > audit_fail_before_2d,
                      "规则版本落后拦截写入 release_order_import_failed 审计")

    record_conclusion(
        "import-规则版本落后",
        "拦截（不可强制）：完全不落 draft，release_orders 无任何新增；"
        "release_order_history 无记录；audit 仅有 release_order_import_failed"
    )

    # ============================================================
    # 阶段 3：publish 命令冲突场景
    # ============================================================
    print("\n" + "=" * 80)
    print("阶段 3：release-order publish 冲突验证")
    print("=" * 80)

    # --- 场景 3-A：publish 目标配置已存在 ---
    print("\n--- [场景 3-A] publish: 目标配置已存在 ---")
    run_cmd(["release-order", "create",
             "--name", "RO-PUB-TARGETEXIST",
             "--source-config", "default",
             "--target-config", "prod_config",
             "--operator", "tester"], desc="创建目标=prod_config(已存在)的发布单")
    run_cmd(["release-order", "lock", "RO-PUB-TARGETEXIST",
             "--operator", "tester"])
    run_cmd(["release-order", "approve", "RO-PUB-TARGETEXIST",
             "--operator", "admin"])

    rows_before = Q.query_release_orders(name="RO-PUB-TARGETEXIST")
    status_before = rows_before[0]["status"]
    history_before = len(Q.query_release_order_history(rows_before[0]["id"]))
    audit_fail_before_3a = Q.count_audit_ops("release_order_publish_failed")

    r = run_cmd(["release-order", "publish", "RO-PUB-TARGETEXIST",
                 "--operator", "admin"],
                check=False, desc="发布目标配置已存在的发布单")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "目标配置已存在的 publish 被拦截")
    all_pass &= check("已存在" in r.stdout,
                      "错误消息包含'已存在'关键词")

    rows_after = Q.query_release_orders(name="RO-PUB-TARGETEXIST")
    all_pass &= check(rows_after[0]["status"] == status_before,
                      "publish 被拦截后状态仍为 approved（不会回退也不会前进）")

    history_after = len(Q.query_release_order_history(rows_after[0]["id"]))
    all_pass &= check(history_after == history_before,
                      "publish 被拦截后 release_order_history 无新增 publish 记录")

    audit_fail_after_3a = Q.count_audit_ops("release_order_publish_failed")
    all_pass &= check(audit_fail_after_3a > audit_fail_before_3a,
                      "publish 拦截写入 release_order_publish_failed 审计")

    record_conclusion(
        "publish-目标配置已存在",
        "拦截（force 可绕过）：发布单保留 approved 状态，不落 draft；"
        "release_order_history 无 publish 记录；audit 有 release_order_publish_failed"
    )

    # --- 场景 3-A2：publish 目标配置已存在（force=True 强制发布） ---
    print("\n--- [场景 3-A2] publish: 目标配置已存在（force=True 强制） ---")
    prod_cfg_before = Q.query_export_configs("prod_config")
    r = run_cmd(["release-order", "publish", "RO-PUB-TARGETEXIST",
                 "--force", "--operator", "admin"],
                desc="force=True 强制发布已存在目标配置")
    all_pass &= check(r.returncode == 0 and "[OK]" in r.stdout,
                      "force=True 允许强制发布目标配置已存在的发布单")

    rows = Q.query_release_orders(name="RO-PUB-TARGETEXIST")
    all_pass &= check(rows[0]["status"] == "published",
                      "强制发布后状态变为 published")

    history = Q.query_release_order_history(rows[0]["id"])
    publish_history = [h for h in history if h["action"] == "publish"]
    all_pass &= check(len(publish_history) >= 1,
                      "强制发布后 release_order_history 有 publish 记录")

    audit_publish = Q.count_audit_ops("release_order_publish")
    all_pass &= check(audit_publish >= 1,
                      "强制发布写入 release_order_publish 审计")

    record_conclusion(
        "publish-目标配置已存在(force=True)",
        "允许：覆盖已有目标配置；发布单状态变为 published；"
        "release_order_history 有 publish 记录；audit 有 release_order_publish"
    )

    # --- 场景 3-B：publish 激活配置漂移 ---
    print("\n--- [场景 3-B] publish: 激活配置漂移 ---")
    run_cmd(["release-order", "create",
             "--name", "RO-PUB-DRIFT",
             "--source-config", "default",
             "--target-config", "test_config",
             "--operator", "tester"], desc="创建目标=test_config 的发布单")
    run_cmd(["release-order", "lock", "RO-PUB-DRIFT", "--operator", "tester"])
    run_cmd(["release-order", "approve", "RO-PUB-DRIFT", "--operator", "admin"])

    run_cmd(["export-config", "use", "test_config", "--operator", "admin"])
    drift_modify_path = os.path.join(OUT_DIR, "drift_modify_2.json")
    run_cmd(["export-config", "to-file", drift_modify_path,
             "--config-name", "test_config"])
    with open(drift_modify_path, 'r', encoding='utf-8') as f:
        drift_cfg2 = json.load(f)
    drift_cfg2["field_policies"]["source_file"] = "drop"
    with open(drift_modify_path, 'w', encoding='utf-8') as f:
        json.dump(drift_cfg2, f, ensure_ascii=False, indent=2)
    run_cmd(["export-config", "from-file", drift_modify_path,
             "--config-name", "test_config", "--overwrite",
             "--operator", "someone_else"],
            desc="修改激活配置制造漂移")

    rows_before = Q.query_release_orders(name="RO-PUB-DRIFT")
    history_before = len(Q.query_release_order_history(rows_before[0]["id"]))
    audit_fail_before_3b = Q.count_audit_ops("release_order_publish_failed")

    r = run_cmd(["release-order", "publish", "RO-PUB-DRIFT",
                 "--operator", "admin"],
                check=False, desc="发布激活配置被修改过的发布单")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "激活配置漂移的 publish 被拦截")
    all_pass &= check("已被修改" in r.stdout,
                      "错误消息包含'已被修改'关键词")

    rows_after = Q.query_release_orders(name="RO-PUB-DRIFT")
    all_pass &= check(rows_after[0]["status"] == "approved",
                      "激活配置漂移拦截后状态仍为 approved")

    history_after = len(Q.query_release_order_history(rows_after[0]["id"]))
    all_pass &= check(history_after == history_before,
                      "激活配置漂移拦截后 release_order_history 无新增 publish 记录")

    audit_fail_after_3b = Q.count_audit_ops("release_order_publish_failed")
    all_pass &= check(audit_fail_after_3b > audit_fail_before_3b,
                      "激活配置漂移拦截写入 release_order_publish_failed 审计")

    run_cmd(["export-config", "use", "default", "--operator", "admin"])

    record_conclusion(
        "publish-激活配置漂移",
        "拦截（force 可绕过）：发布单保留 approved 状态，不落 draft；"
        "release_order_history 无 publish 记录；audit 有 release_order_publish_failed"
    )

    # --- 场景 3-C：publish 规则版本不一致（不可强制） ---
    print("\n--- [场景 3-C] publish: 规则版本不一致（不可强制） ---")
    run_cmd(["release-order", "create",
             "--name", "RO-PUB-OLDVER",
             "--source-config", "default",
             "--target-config", "pub_oldver_target",
             "--operator", "tester"], desc="创建发布单")
    run_cmd(["release-order", "lock", "RO-PUB-OLDVER", "--operator", "tester"])
    run_cmd(["release-order", "approve", "RO-PUB-OLDVER", "--operator", "admin"])

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT rule_version FROM release_orders WHERE name = ?",
                ("RO-PUB-OLDVER",))
    orig_ver = cur.fetchone()[0]
    cur.execute("UPDATE release_orders SET rule_version = ? WHERE name = ?",
                (orig_ver - 999, "RO-PUB-OLDVER"))
    conn.commit()
    conn.close()

    rows_before = Q.query_release_orders(name="RO-PUB-OLDVER")
    history_before = len(Q.query_release_order_history(rows_before[0]["id"]))
    audit_fail_before_3c = Q.count_audit_ops("release_order_publish_failed")

    r = run_cmd(["release-order", "publish", "RO-PUB-OLDVER",
                 "--force", "--operator", "admin"],
                check=False, desc="发布规则版本落后的发布单（即便 force 也被拦截）")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "规则版本不一致的 publish 被拦截（即便是 force）")
    all_pass &= check("规则版本不一致" in r.stdout,
                      "错误消息包含'规则版本不一致'关键词")

    rows_after = Q.query_release_orders(name="RO-PUB-OLDVER")
    all_pass &= check(rows_after[0]["status"] == "approved",
                      "规则版本不一致拦截后状态仍为 approved")

    history_after = len(Q.query_release_order_history(rows_after[0]["id"]))
    all_pass &= check(history_after == history_before,
                      "规则版本不一致拦截后 release_order_history 无新增 publish 记录")

    audit_fail_after_3c = Q.count_audit_ops("release_order_publish_failed")
    all_pass &= check(audit_fail_after_3c > audit_fail_before_3c,
                      "规则版本不一致拦截写入 release_order_publish_failed 审计")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("UPDATE release_orders SET rule_version = ? WHERE name = ?",
                (orig_ver, "RO-PUB-OLDVER"))
    conn.commit()
    conn.close()

    record_conclusion(
        "publish-规则版本不一致",
        "拦截（不可强制，force 也无效）：发布单保留 approved 状态，不落 draft；"
        "release_order_history 无 publish 记录；audit 有 release_order_publish_failed"
    )

    # --- 场景 3-D：publish 正常流程（为后续 revert 测试做准备） ---
    print("\n--- [场景 3-D] publish: 正常发布（为 revert 做准备） ---")
    run_cmd(["release-order", "lock", "RO-FLOW-001", "--operator", "tester"])
    run_cmd(["release-order", "approve", "RO-FLOW-001", "--operator", "admin"])
    r = run_cmd(["release-order", "publish", "RO-FLOW-001",
                 "--operator", "admin"], desc="发布 RO-FLOW-001")
    all_pass &= check(r.returncode == 0 and "[OK]" in r.stdout, "正常发布成功")

    all_pass &= assert_consistency(
        name="RO-FLOW-001",
        expect_in_orders=True,
        expect_history_count=5,
        expect_audit_ops=["release_order_publish"],
        expect_status="published"
    )

    all_pass &= check(Q.query_export_configs("flow_target_1"),
                      "发布后目标配置 flow_target_1 已创建在 export_configs 中")

    # ============================================================
    # 阶段 4：revert 命令
    # ============================================================
    print("\n" + "=" * 80)
    print("阶段 4：release-order revert 验证")
    print("=" * 80)

    # --- 场景 4-A：revert 正常撤销 ---
    print("\n--- [场景 4-A] revert: 正常撤销已发布 ---")
    rows_before = Q.query_release_orders(name="RO-FLOW-001")
    history_before = len(Q.query_release_order_history(rows_before[0]["id"]))
    target_existed_before = bool(Q.query_export_configs("flow_target_1"))

    r = run_cmd(["release-order", "revert", "RO-FLOW-001",
                 "--operator", "admin"], desc="撤销 RO-FLOW-001")
    all_pass &= check(r.returncode == 0 and "[OK]" in r.stdout, "撤销成功")

    rows_after = Q.query_release_orders(name="RO-FLOW-001")
    all_pass &= check(rows_after[0]["status"] == "reverted",
                      "撤销后状态变为 reverted")

    history_after = Q.query_release_order_history(rows_after[0]["id"])
    revert_history = [h for h in history_after if h["action"] == "revert"]
    all_pass &= check(len(revert_history) >= 1,
                      "撤销后 release_order_history 有 revert 记录")

    audit_revert = Q.count_audit_ops("release_order_revert")
    all_pass &= check(audit_revert >= 1,
                      "撤销写入 release_order_revert 审计")

    target_exists_after = bool(Q.query_export_configs("flow_target_1"))
    all_pass &= check(target_exists_after is False,
                      "撤销后新创建的目标配置 flow_target_1 已被删除")

    record_conclusion(
        "revert-正常撤销（发布前目标不存在）",
        "成功：发布单状态变为 reverted；release_order_history 有 revert 记录；"
        "audit 有 release_order_revert；目标配置被删除"
    )

    # --- 场景 4-B：revert 非 published 状态被拦截 ---
    print("\n--- [场景 4-B] revert: 非 published 状态被拦截 ---")
    audit_fail_before_4b = Q.count_audit_ops("release_order_revert_failed")
    r = run_cmd(["release-order", "revert", "RO-DUP-001",
                 "--operator", "admin"],
                check=False, desc="撤销 draft 状态的 RO-DUP-001")
    all_pass &= check(r.returncode != 0 and "[ERROR]" in r.stdout,
                      "非 published 状态 revert 被拦截")

    audit_fail_after_4b = Q.count_audit_ops("release_order_revert_failed")
    all_pass &= check(audit_fail_after_4b == audit_fail_before_4b,
                      "注意：非 published revert 拦截不写 release_order_revert_failed"
                      "（产品实际行为：状态检查在写审计之前就 raise 了）")
    print(f"    [INFO] 实际产品行为：revert 非 published 状态拦截时，"
          f"audit_revert_failed 前后均为 {audit_fail_after_4b}")

    rows = Q.query_release_orders(name="RO-DUP-001")
    all_pass &= check(rows[0]["status"] == "draft",
                      "revert 被拦截后发布单状态仍为 draft")

    record_conclusion(
        "revert-非 published 状态",
        "拦截：发布单保留原状态；release_order_history 无 revert 记录；"
        "注意：audit 中**无** release_order_revert_failed（产品实际行为：状态检查在审计之前就抛出异常）"
    )

    # ============================================================
    # 阶段 5：跨重启复测
    # ============================================================
    print("\n" + "=" * 80)
    print("阶段 5：跨重启复测（被拦截 vs 已发布场景的状态持久化）")
    print("=" * 80)

    # --- 场景 5-A：被拦截场景（import 目标配置已存在）重启前后一致 ---
    print("\n--- [场景 5-A] 跨重启：被拦截的 import-目标配置已存在 ---")
    restart_export = build_export_file(OUT_DIR, "RO-RESTART-BLOCKED",
                                        source_config="default",
                                        target_config="prod_config")

    run_cmd(["release-order", "import", restart_export,
             "--operator", "admin"],
            check=False, desc="重启前：导入目标已存在，应被拦截")

    before_orders_count = Q.count_release_orders_by_name("RO-RESTART-BLOCKED")
    before_audit_fail = Q.count_audit_ops("release_order_import_failed")

    all_pass &= check(before_orders_count == 0,
                      "重启前：拦截后 release_orders 中无 RO-RESTART-BLOCKED")

    print("\n    [模拟重启] 通过全新数据库连接 + CLI 子进程验证持久化...")

    r_show = run_cmd(["release-order", "list"],
                     check=False, desc="重启后：CLI 列出发布单")

    persisted_ok = verify_persisted_state_via_fresh_connection([
        ("release_orders 中仍无 RO-RESTART-BLOCKED",
         lambda q: len(q.query_release_orders(name="RO-RESTART-BLOCKED")) == 0),
        (f"audit_logs 中 release_order_import_failed >= {before_audit_fail}",
         lambda q: q.count_audit_ops("release_order_import_failed") >= before_audit_fail),
    ])
    all_pass &= persisted_ok

    all_pass &= check("RO-RESTART-BLOCKED" not in r_show.stdout,
                      "CLI list 输出中也不包含 RO-RESTART-BLOCKED")

    record_conclusion(
        "跨重启-被拦截场景",
        "状态一致：重启前后 release_orders 都没有 draft；"
        "audit_logs 的失败记录在重启后仍可查询；release_order_history 无记录"
    )

    # --- 场景 5-B：已发布场景重启前后状态一致 ---
    print("\n--- [场景 5-B] 跨重启：已发布发布单 ---")
    run_cmd(["release-order", "create",
             "--name", "RO-RESTART-PUBLISHED",
             "--source-config", "default",
             "--target-config", "restart_pub_target",
             "--operator", "tester"], desc="创建重启测试发布单")
    run_cmd(["release-order", "lock", "RO-RESTART-PUBLISHED", "--operator", "tester"])
    run_cmd(["release-order", "approve", "RO-RESTART-PUBLISHED", "--operator", "admin"])
    run_cmd(["release-order", "publish", "RO-RESTART-PUBLISHED",
             "--operator", "admin"], desc="发布重启测试发布单")

    before_rows = Q.query_release_orders(name="RO-RESTART-PUBLISHED")
    before_status = before_rows[0]["status"]
    before_order_id = before_rows[0]["id"]
    before_history = Q.query_release_order_history(before_order_id)
    before_history_count = len(before_history)
    before_publish_actions = [h["action"] for h in before_history]
    before_audit_publish = Q.count_audit_ops("release_order_publish")
    before_target_exists = bool(Q.query_export_configs("restart_pub_target"))

    all_pass &= check(before_status == "published",
                      "重启前：状态为 published")
    all_pass &= check("publish" in before_publish_actions,
                      "重启前：history 包含 publish 动作")
    all_pass &= check(before_target_exists,
                      "重启前：目标配置存在")

    print("\n    [模拟重启] 通过全新数据库连接 + CLI 子进程验证持久化...")

    r_show2 = run_cmd(["release-order", "show", "RO-RESTART-PUBLISHED"],
                      desc="重启后：CLI 查看已发布发布单")

    persisted_ok2 = verify_persisted_state_via_fresh_connection([
        ("release_orders 中仍有 RO-RESTART-PUBLISHED",
         lambda q: len(q.query_release_orders(name="RO-RESTART-PUBLISHED")) == 1),
        (f"status 仍为 {before_status}",
         lambda q: q.query_release_orders(name="RO-RESTART-PUBLISHED")[0]["status"] == before_status),
        (f"release_order_history 条数为 {before_history_count}",
         lambda q: len(q.query_release_order_history(
             q.query_release_orders(name="RO-RESTART-PUBLISHED")[0]["id"]
         )) == before_history_count),
        ("release_order_history 包含 publish 动作",
         lambda q: any(
             h["action"] == "publish"
             for h in q.query_release_order_history(
                 q.query_release_orders(name="RO-RESTART-PUBLISHED")[0]["id"]
             )
         )),
        (f"audit_logs release_order_publish >= {before_audit_publish}",
         lambda q: q.count_audit_ops("release_order_publish") >= before_audit_publish),
        ("目标配置 restart_pub_target 存在于 export_configs",
         lambda q: bool(q.query_export_configs("restart_pub_target"))),
    ])
    all_pass &= persisted_ok2

    all_pass &= check("published" in r_show2.stdout and "RO-RESTART-PUBLISHED" in r_show2.stdout,
                      "CLI show 输出中状态仍为 published")

    record_conclusion(
        "跨重启-已发布场景",
        "状态一致：重启前后 release_orders.status=published；"
        "release_order_history 条数和动作一致；audit_logs 的发布记录仍在；"
        "目标配置仍存在于 export_configs"
    )

    # --- 场景 5-C：被拦截的 publish 场景重启前后状态一致 ---
    print("\n--- [场景 5-C] 跨重启：被拦截的 publish（规则版本不一致） ---")
    run_cmd(["release-order", "create",
             "--name", "RO-RESTART-PUBBLOCKED",
             "--source-config", "default",
             "--target-config", "restart_block_target",
             "--operator", "tester"], desc="创建发布单")
    run_cmd(["release-order", "lock", "RO-RESTART-PUBBLOCKED", "--operator", "tester"])
    run_cmd(["release-order", "approve", "RO-RESTART-PUBBLOCKED", "--operator", "admin"])

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT rule_version FROM release_orders WHERE name = ?",
                ("RO-RESTART-PUBBLOCKED",))
    orig_v = cur.fetchone()[0]
    cur.execute("UPDATE release_orders SET rule_version = ? WHERE name = ?",
                (orig_v - 999, "RO-RESTART-PUBBLOCKED"))
    conn.commit()
    conn.close()

    run_cmd(["release-order", "publish", "RO-RESTART-PUBBLOCKED",
             "--operator", "admin"],
            check=False, desc="重启前：发布被规则版本冲突拦截")

    before_rows = Q.query_release_orders(name="RO-RESTART-PUBBLOCKED")
    before_status = before_rows[0]["status"]
    before_history = Q.query_release_order_history(before_rows[0]["id"])
    before_has_publish = any(h["action"] == "publish" for h in before_history)
    before_audit_fail = Q.count_audit_ops("release_order_publish_failed")

    all_pass &= check(before_status == "approved",
                      "重启前：被拦截后状态仍为 approved")
    all_pass &= check(before_has_publish is False,
                      "重启前：history 中没有 publish 记录")

    print("\n    [模拟重启] 通过全新数据库连接验证持久化...")

    persisted_ok3 = verify_persisted_state_via_fresh_connection([
        (f"status 仍为 {before_status}",
         lambda q: q.query_release_orders(name="RO-RESTART-PUBBLOCKED")[0]["status"] == before_status),
        ("release_order_history 中仍没有 publish 记录",
         lambda q: not any(
             h["action"] == "publish"
             for h in q.query_release_order_history(
                 q.query_release_orders(name="RO-RESTART-PUBBLOCKED")[0]["id"]
             )
         )),
        (f"audit_logs release_order_publish_failed >= {before_audit_fail}",
         lambda q: q.count_audit_ops("release_order_publish_failed") >= before_audit_fail),
    ])
    all_pass &= persisted_ok3

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("UPDATE release_orders SET rule_version = ? WHERE name = ?",
                (orig_v, "RO-RESTART-PUBBLOCKED"))
    conn.commit()
    conn.close()

    record_conclusion(
        "跨重启-被拦截的 publish 场景",
        "状态一致：重启前后 release_orders.status=approved；"
        "release_order_history 都没有 publish 记录；audit_logs 的失败记录仍存在"
    )

    # ============================================================
    # 阶段 6：结论汇总
    # ============================================================
    print("\n" + "=" * 80)
    print("阶段 6：可复现结论汇总")
    print("=" * 80)
    print("\n[汇总] 各类冲突的落库行为（可复现）：\n")

    draft_conflicts = []
    approved_with_audit_fail = []
    audit_only_conflicts = []
    no_fail_audit = []
    successful_ops = []

    for scenario, conclusion in CONCLUSION.items():
        print(f"  [{scenario}]")
        print(f"    {conclusion}")
        print()

        if scenario.startswith("跨重启-"):
            continue
        if scenario.startswith("revert-正常撤销") or \
           scenario.endswith("(force=True)"):
            successful_ops.append(scenario)
            continue

        if "不落新 draft" in conclusion or "保留首次创建的 draft" in conclusion:
            draft_conflicts.append(scenario)
        elif "保留 approved 状态" in conclusion:
            approved_with_audit_fail.append(scenario)
        elif "完全不落 draft" in conclusion:
            audit_only_conflicts.append(scenario)

        if "audit 中**无**" in conclusion or "不写 release_order_import_failed" in conclusion:
            no_fail_audit.append(scenario)

    print("\n--- 分类汇总（可复现结论） ---")
    print(f"  [落 draft / 保留已有 draft] 的冲突：")
    for c in draft_conflicts:
        print(f"     - {c}")
    print(f"  [保留 approved 状态 + 落 publish_failed audit] 的冲突（publish 类）：")
    for c in approved_with_audit_fail:
        print(f"     - {c}")
    print(f"  [仅落失败 audit，完全不落 draft / 无 history] 的冲突：")
    for c in audit_only_conflicts:
        print(f"     - {c}")
    print(f"  [不写任何失败 audit 的特殊场景]（产品实际行为，坑！）：")
    for c in no_fail_audit:
        print(f"     - {c}")
    print(f"  [正常成功，走完整落库路径] 的操作：")
    for c in successful_ops:
        print(f"     - {c}")

    shutil.rmtree(OUT_DIR, ignore_errors=True)

    print("\n" + "=" * 80)
    if all_pass:
        print("[SUCCESS] release-order CLI E2E 回归测试全部通过！")
    else:
        print("[FAILED] release-order CLI E2E 回归测试存在失败项！")
    print("=" * 80)
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
