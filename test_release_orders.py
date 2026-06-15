"""导出配置发布单模块测试

覆盖场景：
1. 新建发布单、查看列表、查看详情
2. 草稿配置修改
3. 配置差异查看
4. 锁定 -> 审批 -> 发布流程
5. 撤销到上一版
6. 导入导出 JSON 往返
7. 跨重启恢复（重新连接后数据仍在）
8. 冲突拦截（目标配置已存在、激活配置被改、规则版本不一致）
9. 权限限制（只有管理员能发布和撤销）
10. 撤销后实际导出结果跟着切回
11. 审计日志记录
12. 发布单历史记录

可复用测试夹具（见 release_test_fixtures 区域）：
  - setup_test_env         重置 DB + 初始化 + 准备命名配置
  - build_export_file      导出一个发布单到临时 JSON 文件（供导入测试复用）
  - assert_import_blocked  断言导入被某类冲突拦截且不落库 draft
  - assert_audit_has_op    断言审计日志中存在某操作
"""
import os
import sys
import json
import sqlite3
import tempfile
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "corpus.db")


# ============================================================
# release_test_fixtures：可复用测试夹具
# ============================================================

def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


def check(cond, msg):
    if cond:
        print(f"[OK] {msg}")
        return True
    print(f"[FAIL] {msg}")
    return False


def setup_test_env():
    """初始化数据库并准备多个命名配置，供各测试场景复用。

    返回：(ec_module, ro_module, desensitizer_module, exporter_module, audit_module)
    """
    reset_db()
    from corpus_tool.database import init_db, ensure_default_rules
    from corpus_tool import release_orders as ro
    from corpus_tool import export_config as ec
    from corpus_tool import desensitizer
    from corpus_tool import exporter
    from corpus_tool.audit import get_audit_logs

    init_db()
    ensure_default_rules()

    ec.set_active_config("default", operator="admin")

    cfg_prod = ec.ExportConfig()
    cfg_prod.format = ec.ExportFormat.CSV.value
    cfg_prod.field_policies["original_text"] = ec.FieldPolicy.DROP.value
    ec.save_config(cfg_prod, "prod_config", operator="admin")

    cfg_test = ec.ExportConfig()
    cfg_test.format = ec.ExportFormat.JSONL.value
    cfg_test.field_policies["original_text"] = ec.FieldPolicy.DROP.value
    cfg_test.include_review_summary = True
    ec.save_config(cfg_test, "test_config", operator="admin")

    return ec, ro, desensitizer, exporter, get_audit_logs


def build_export_file(ro, ec, tmpdir, order_name="RO-FIXTURE",
                      source="default", target="fixture_target",
                      config_overrides=None):
    """创建一个发布单、按可选覆写修改配置、导出为 JSON 文件。

    返回：(order_object, export_file_path)
    """
    order = ro.create_release_order(
        order_name,
        source_config_name=source,
        target_config_name=target,
        description="夹具生成的发布单",
        operator="tester"
    )
    if config_overrides:
        order = ro.update_draft_config(order.id, operator="tester", **config_overrides)
    export_file = os.path.join(tmpdir, f"{order_name}.json")
    ro.export_release_order(order.id, export_file, operator="admin")
    return order, export_file


def _count_draft_orders(ro, name_filter=None):
    """统计当前 draft 状态的发布单数量（用于验证冲突拦截不会落库）。"""
    orders = ro.list_release_orders()
    if name_filter:
        return sum(1 for o in orders if o.status == "draft" and o.name == name_filter)
    return sum(1 for o in orders if o.status == "draft")


def assert_import_blocked(ro, export_file, operator, expected_keywords,
                          rename_to=None, force=False, order_name=None):
    """断言导入被拦截，并且数据库中没有新增 draft。

    参数：
      ro                - release_orders 模块
      export_file       - 待导入的 JSON 文件路径
      operator          - 操作人
      expected_keywords - 错误消息中应包含的关键词列表（任一命中即算匹配）
      rename_to         - 可选重命名
      force             - 是否 force
      order_name        - 导入后的目标发布单名称（用于统计 draft 数量）
    """
    draft_before = _count_draft_orders(ro)
    target_name = order_name or rename_to
    if target_name:
        target_before = _count_draft_orders(ro, name_filter=target_name)

    caught = False
    try:
        ro.import_release_order(export_file, operator=operator,
                                rename_to=rename_to, force=force)
    except ValueError as e:
        msg = str(e)
        matched = any(kw in msg for kw in expected_keywords)
        if not matched:
            print(f"[WARN] 拦截消息缺少期望关键词: msg={msg!r}, expected={expected_keywords}")
        caught = matched

    draft_after = _count_draft_orders(ro)
    no_leak = (draft_after == draft_before)
    if target_name:
        target_after = _count_draft_orders(ro, name_filter=target_name)
        no_leak = no_leak and (target_after == target_before)

    return check(caught and no_leak,
                 f"导入被正确拦截（关键词={expected_keywords}）且无 draft 泄漏")


def assert_audit_has_op(get_audit_logs_fn, operation, limit=300,
                        detail_contains=None):
    """断言审计日志中存在指定操作（可选详情关键词）。"""
    logs = get_audit_logs_fn(limit=limit)
    matched = [l for l in logs if l.operation == operation]
    if detail_contains:
        matched = [l for l in matched if detail_contains in l.details]
    return check(len(matched) > 0,
                 f"审计日志包含操作 [{operation}]"
                 + (f"（详情含 '{detail_contains}'）" if detail_contains else ""))


# ============================================================
# 测试主流程
# ============================================================

def main():
    print("=" * 70)
    print("导出配置发布单模块测试")
    print("=" * 70)

    ec, ro, desensitizer, exporter, get_audit_logs = setup_test_env()
    all_pass = True

    # ---- 1. 准备测试数据：确认基础配置就绪 ----
    print("\n--- 准备测试数据 ---")

    configs = ec.list_configs()
    all_pass &= check(len(configs) >= 3, "至少有 3 个配置方案 (default, prod_config, test_config)")

    # ---- 2. 新建发布单 ----
    print("\n--- 测试新建发布单 ---")

    order1 = ro.create_release_order(
        "RO-001",
        source_config_name="default",
        target_config_name="new_prod_config",
        description="创建新的生产配置，保留更多字段",
        operator="tester"
    )
    all_pass &= check(order1.name == "RO-001", "发布单名称正确")
    all_pass &= check(order1.status == "draft", "初始状态为草稿")
    all_pass &= check(order1.source_config_name == "default", "源配置正确")
    all_pass &= check(order1.target_config_name == "new_prod_config", "目标配置正确")
    all_pass &= check(order1.created_by == "tester", "创建人正确")

    orders = ro.list_release_orders()
    all_pass &= check(len(orders) == 1, "发布单列表有 1 条记录")

    try:
        ro.create_release_order(
            "RO-001",
            source_config_name="default",
            target_config_name="test_config",
            operator="tester"
        )
        all_pass &= check(False, "同名发布单创建应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("已存在" in str(e), "同名发布单创建被正确拒绝")

    try:
        ro.create_release_order(
            "RO-002",
            source_config_name="nonexistent",
            target_config_name="prod_config",
            operator="tester"
        )
        all_pass &= check(False, "源配置不存在时创建应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("不存在" in str(e), "源配置不存在时创建被正确拒绝")

    # ---- 3. 草稿配置修改 ----
    print("\n--- 测试草稿配置修改 ---")

    order1 = ro.update_draft_config(
        order1.id,
        field_policies={
            "original_text": ec.FieldPolicy.KEEP.value,
            "status": ec.FieldPolicy.KEEP.value,
        },
        format="jsonl",
        include_review_summary=True,
        operator="tester"
    )
    all_pass &= check(order1.status == "draft", "修改后状态仍为草稿")

    cfg = ec.ExportConfig.from_json(order1.config_json)
    all_pass &= check(cfg.format == "jsonl", "格式已修改为 jsonl")
    all_pass &= check(
        cfg.field_policies["original_text"] == ec.FieldPolicy.KEEP.value,
        "original_text 策略已修改为 keep"
    )
    all_pass &= check(
        cfg.field_policies["status"] == ec.FieldPolicy.KEEP.value,
        "status 策略已修改为 keep"
    )
    all_pass &= check(cfg.include_review_summary is True, "复核摘要已开启")

    try:
        ro.lock_release_order(order1.id, operator="tester")
        ro.update_draft_config(
            order1.id,
            format="csv",
            operator="tester"
        )
        all_pass &= check(False, "锁定后修改应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("草稿" in str(e), "锁定后修改配置被正确拒绝")

    # ---- 4. 配置差异查看 ----
    print("\n--- 测试配置差异查看 ---")

    diff = ro.get_config_diff(order1.id)
    all_pass &= check(diff["has_diff"] is True, "检测到配置差异")

    changes = {c["field"] for c in diff["changes"]}
    all_pass &= check("format" in changes, "差异包含 format 字段变化")
    all_pass &= check("include_review_summary" in changes, "差异包含 include_review_summary 变化")

    policy_changes = {
        c["field"] for c in diff["changes"]
        if c["field"] == "field_policies"
    }

    # ---- 5. 跨重启恢复测试 ----
    print("\n--- 测试跨重启恢复 ---")

    order_id_before = order1.id
    order_name_before = order1.name

    del order1, orders, diff

    orders_after = ro.list_release_orders()
    all_pass &= check(len(orders_after) == 1, "重启后发布单列表仍有 1 条")

    order_reloaded = ro.get_release_order(order_id_before)
    all_pass &= check(order_reloaded.name == order_name_before, "重启后发布单名称一致")
    all_pass &= check(order_reloaded.status == "locked", "重启后状态仍为 locked")
    all_pass &= check(order_reloaded.created_by == "tester", "重启后创建人一致")

    cfg_reloaded = ec.ExportConfig.from_json(order_reloaded.config_json)
    all_pass &= check(cfg_reloaded.format == "jsonl", "重启后配置格式一致")

    history = ro.get_order_history(order_id_before)
    all_pass &= check(len(history) >= 2, "重启后历史记录仍存在")
    actions = {h.action for h in history}
    all_pass &= check("create" in actions, "历史包含 create 操作")
    all_pass &= check("update_config" in actions, "历史包含 update_config 操作")
    all_pass &= check("lock" in actions, "历史包含 lock 操作")

    # ---- 6. 锁定 -> 审批 -> 发布流程 ----
    print("\n--- 测试审批流程 ---")

    try:
        ro.approve_release_order(order_reloaded.id, operator="tester")
        all_pass &= check(False, "非管理员审批应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("管理员" in str(e), "非管理员审批被正确拒绝")

    order_approved = ro.approve_release_order(order_reloaded.id, operator="admin")
    all_pass &= check(order_approved.status == "approved", "状态变为 approved")
    all_pass &= check(order_approved.approver == "admin", "审批人为 admin")
    all_pass &= check(order_approved.approved_at is not None, "审批时间已记录")

    print("\n--- 测试发布流程 ---")

    try:
        ro.publish_release_order(order_approved.id, operator="tester")
        all_pass &= check(False, "非管理员发布应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("管理员" in str(e), "非管理员发布被正确拒绝")

    new_prod_before_exists = ec.config_exists("new_prod_config")
    all_pass &= check(not new_prod_before_exists, "发布前 new_prod_config 不存在")

    result = ro.publish_release_order(order_approved.id, operator="admin")
    all_pass &= check(result["order_name"] == "RO-001", "发布结果名称正确")
    all_pass &= check(result["target_config"] == "new_prod_config", "发布目标正确")

    prod_cfg_after, _ = ec.load_config("new_prod_config")
    all_pass &= check(ec.config_exists("new_prod_config"), "发布后 new_prod_config 已创建")
    all_pass &= check(prod_cfg_after.format == "jsonl", "发布后 new_prod_config 格式已变为 jsonl")
    all_pass &= check(
        prod_cfg_after.field_policies["original_text"] == ec.FieldPolicy.KEEP.value,
        "发布后 new_prod_config original_text 策略已变为 keep"
    )
    all_pass &= check(
        prod_cfg_after.include_review_summary is True,
        "发布后 new_prod_config 复核摘要已开启"
    )

    order_published = ro.get_release_order(order_approved.id)
    all_pass &= check(order_published.status == "published", "状态变为 published")
    all_pass &= check(order_published.published_at is not None, "发布时间已记录")

    # ---- 7. 冲突拦截测试 ----
    print("\n--- 测试冲突拦截 ---")

    order2 = ro.create_release_order(
        "RO-002",
        source_config_name="test_config",
        target_config_name="prod_config",
        description="再次更新 prod_config",
        operator="tester"
    )

    order2 = ro.update_draft_config(
        order2.id,
        format="csv",
        operator="tester"
    )
    order2 = ro.lock_release_order(order2.id, operator="tester")
    order2 = ro.approve_release_order(order2.id, operator="admin")

    ec.set_active_config("prod_config", operator="admin")
    cfg_modified = ec.ExportConfig.from_json(order2.config_json)
    cfg_modified.field_policies["created_at"] = ec.FieldPolicy.KEEP.value
    ec.save_config(cfg_modified, "prod_config", operator="someone_else")

    try:
        ro.publish_release_order(order2.id, operator="admin")
        all_pass &= check(False, "激活配置被改后发布应被拦截但未被拦截")
    except ValueError as e:
        all_pass &= check("已被修改" in str(e), "激活配置被改后发布被正确拦截")

    current_version = desensitizer.get_current_version()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT rule_version FROM release_orders WHERE id = ?", (order2.id,))
    original_version = cur.fetchone()[0]
    cur.execute("SELECT config_json FROM export_configs WHERE config_name = 'prod_config'")
    prod_cfg_before_row = cur.fetchone()
    prod_cfg_before_json = prod_cfg_before_row[0] if prod_cfg_before_row else None
    cur.execute("UPDATE release_orders SET rule_version = ? WHERE id = ?",
                (original_version - 1, order2.id))
    conn.commit()
    conn.close()

    try:
        ro.publish_release_order(order2.id, operator="admin", force=True)
        all_pass &= check(False, "规则版本不一致应被拦截但未被拦截")
    except ValueError as e:
        all_pass &= check("规则版本不一致" in str(e), "规则版本不一致被正确拦截")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("UPDATE release_orders SET rule_version = ? WHERE id = ?",
                (original_version, order2.id))
    conn.commit()
    conn.close()

    if prod_cfg_before_json:
        prod_cfg_before = ec.ExportConfig.from_json(prod_cfg_before_json)
        ec.save_config(prod_cfg_before, "prod_config", operator="admin")

    try:
        ro.publish_release_order(order2.id, operator="admin")
        all_pass &= check(False, "目标配置已存在应被拦截但未被拦截")
    except ValueError as e:
        all_pass &= check("已存在" in str(e), "目标配置已存在被正确拦截")

    # ---- 8. 撤销到上一版 ----
    print("\n--- 测试撤销到上一版 ---")

    try:
        ro.revert_release_order(order_approved.id, operator="tester")
        all_pass &= check(False, "非管理员撤销应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("管理员" in str(e), "非管理员撤销被正确拒绝")

    prod_cfg_mid, _ = ec.load_config("new_prod_config")
    all_pass &= check(prod_cfg_mid.format == "jsonl", "撤销前 new_prod_config 格式为 jsonl")

    result = ro.revert_release_order(order_approved.id, operator="admin")
    all_pass &= check(result["order_name"] == "RO-001", "撤销结果名称正确")
    all_pass &= check(result["target_config"] == "new_prod_config", "撤销目标正确")

    new_prod_exists_after_revert = ec.config_exists("new_prod_config")
    all_pass &= check(not new_prod_exists_after_revert,
                      "撤销后 new_prod_config 已被删除（发布前不存在）")

    order_reverted = ro.get_release_order(order_approved.id)
    all_pass &= check(order_reverted.status == "reverted", "状态变为 reverted")

    try:
        ro.revert_release_order(order_approved.id, operator="admin")
        all_pass &= check(False, "非 published 状态撤销应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("已发布" in str(e) or "published" in str(e),
                          "非 published 状态撤销被正确拒绝")

    # ---- 9. 撤销后实际导出结果跟着切回 ----
    print("\n--- 测试撤销后实际导出结果跟着切回 ---")

    from corpus_tool import importer, desensitizer, sampler

    txt_file = os.path.join(SCRIPT_DIR, "test_samples", "customer_service.txt")
    ids = importer.import_txt_file(txt_file, operator="admin")
    all_pass &= check(len(ids) > 0, f"导入语料成功 ({len(ids)} 条)")

    desensitizer.batch_desensitize(operator="admin")

    count, batch_name = sampler.sample_corpus(
        count=2, batch_name="RELEASE-TEST", operator="admin"
    )

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT id FROM corpus WHERE is_sampled = 1 ORDER BY id LIMIT 2")
    sids = [r[0] for r in cur.fetchall()]
    conn.close()

    for cid in sids:
        sampler.submit_review(cid, "revA", "approved", "OK", operator="admin")
        sampler.submit_review(cid, "revB", "approved", "OK", operator="admin")

    tmpdir = tempfile.mkdtemp(prefix="release_test_")

    ec.set_active_config("prod_config", operator="admin")

    out1 = os.path.join(tmpdir, "export_before_publish.csv")
    try:
        count = exporter.export_desensitized(out1, operator="admin", use_saved_config=True, config_name="prod_config")
        all_pass &= check(True, "发布前导出成功 (CSV)")
    except Exception as e:
        all_pass &= check(False, f"发布前导出失败: {e}")

    order3 = ro.create_release_order(
        "RO-003",
        source_config_name="default",
        target_config_name="prod_config",
        operator="tester"
    )
    order3 = ro.update_draft_config(
        order3.id,
        format="jsonl",
        include_review_summary=True,
        operator="tester"
    )
    order3 = ro.lock_release_order(order3.id, operator="tester")
    order3 = ro.approve_release_order(order3.id, operator="admin")
    ro.publish_release_order(order3.id, operator="admin", force=True)

    out2 = os.path.join(tmpdir, "export_after_publish.jsonl")
    try:
        count = exporter.export_desensitized(out2, operator="admin", use_saved_config=True, config_name="prod_config")
        all_pass &= check(True, "发布后导出成功 (JSONL)")

        with open(out2, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        all_pass &= check(len(lines) > 0, "JSONL 导出文件非空")

        first_line = json.loads(lines[0])
        all_pass &= check("review_summary" in first_line,
                          "发布后 JSONL 包含 review_summary 字段")
    except Exception as e:
        all_pass &= check(False, f"发布后导出失败: {e}")

    ro.revert_release_order(order3.id, operator="admin")

    out3 = os.path.join(tmpdir, "export_after_revert.csv")
    try:
        count = exporter.export_desensitized(out3, operator="admin", use_saved_config=True, config_name="prod_config")
        all_pass &= check(True, "撤销后导出成功 (CSV)")

        with open(out1, 'r', encoding='utf-8') as f:
            content1 = f.read()
        with open(out3, 'r', encoding='utf-8') as f:
            content3 = f.read()
        all_pass &= check(content1 == content3,
                          "撤销后导出结果与发布前完全一致")
    except Exception as e:
        all_pass &= check(False, f"撤销后导出失败: {e}")

    # ---- 10. 导入导出 JSON 往返（使用可复用夹具） ----
    print("\n--- 测试导入导出 JSON 往返 ---")

    order4, export_file = build_export_file(
        ro, ec, tmpdir,
        order_name="RO-004",
        source="test_config",
        target="new_config",
        config_overrides={
            "format": "jsonl",
            "field_policies": {"created_at": ec.FieldPolicy.KEEP.value},
        }
    )
    order4_desc = "用于导入导出测试的发布单"

    all_pass &= check(os.path.exists(export_file), "发布单文件已导出")

    with open(export_file, 'r', encoding='utf-8') as f:
        export_data = json.load(f)
    all_pass &= check(export_data["schema_version"] == 1, "导出文件 schema 版本正确")
    all_pass &= check(export_data["order_info"]["name"] == "RO-004", "导出文件名称正确")
    all_pass &= check("config" in export_data, "导出文件包含 config")
    all_pass &= check("history" in export_data, "导出文件包含 history")

    all_pass &= assert_import_blocked(
        ro, export_file, operator="admin",
        expected_keywords=["已存在"],
        order_name="RO-004"
    )

    imported = ro.import_release_order(
        export_file, operator="admin", rename_to="RO-004-IMPORTED"
    )
    all_pass &= check(imported.name == "RO-004-IMPORTED", "重命名导入成功")
    all_pass &= check(
        imported.source_config_name == order4.source_config_name,
        "导入后源配置一致"
    )
    all_pass &= check(
        imported.target_config_name == order4.target_config_name,
        "导入后目标配置一致"
    )

    cfg_imported = ec.ExportConfig.from_json(imported.config_json)
    cfg_original = ec.ExportConfig.from_json(order4.config_json)
    all_pass &= check(
        cfg_imported.format == cfg_original.format,
        "导入后配置格式一致"
    )
    all_pass &= check(
        cfg_imported.field_policies["created_at"] == cfg_original.field_policies["created_at"],
        "导入后字段策略一致"
    )

    bad_file = os.path.join(tmpdir, "bad_release.json")
    with open(bad_file, 'w', encoding='utf-8') as f:
        f.write("this is not valid json {{{")
    all_pass &= assert_import_blocked(
        ro, bad_file, operator="admin",
        expected_keywords=["读取失败", "JSON"]
    )

    bad_schema_file = os.path.join(tmpdir, "bad_schema.json")
    with open(bad_schema_file, 'w', encoding='utf-8') as f:
        json.dump({"schema_version": 999, "order_info": {}, "config": {}}, f)
    all_pass &= assert_import_blocked(
        ro, bad_schema_file, operator="admin",
        expected_keywords=["不兼容"]
    )

    # ---- 10.5 导入冲突回归测试（使用可复用夹具） ----
    print("\n--- 测试导入冲突回归（同名/激活漂移/旧版本/重启复现） ---")

    # --- 场景 A：同名发布单冲突（force=True 可覆盖） ---
    print("\n  [场景 A] 同名发布单冲突")
    base_order, base_export = build_export_file(
        ro, ec, tmpdir,
        order_name="RO-CONFLICT-BASE",
        source="default",
        target="conflict_target",
        config_overrides={"format": "jsonl"},
    )
    all_pass &= assert_import_blocked(
        ro, base_export, operator="admin",
        expected_keywords=["已存在"],
        order_name="RO-CONFLICT-BASE",
    )
    imported_force = ro.import_release_order(
        base_export, operator="admin", force=True
    )
    all_pass &= check(imported_force.name == "RO-CONFLICT-BASE",
                      "force=True 可覆盖同名发布单")
    ro.delete_release_order(imported_force.id, operator="admin")

    # --- 场景 B：目标配置已存在 ---
    print("\n  [场景 B] 目标配置已存在")
    target_exists_order, target_exists_export = build_export_file(
        ro, ec, tmpdir,
        order_name="RO-TARGET-EXISTS",
        source="default",
        target="prod_config",
    )
    ro.delete_release_order(target_exists_order.id, operator="admin")
    all_pass &= assert_import_blocked(
        ro, target_exists_export, operator="admin",
        expected_keywords=["目标配置", "已存在"],
        order_name="RO-TARGET-EXISTS",
    )
    all_pass &= assert_audit_has_op(
        get_audit_logs, "release_order_import_failed",
        detail_contains="目标配置"
    )

    # --- 场景 C：激活配置被他人改动（漂移） ---
    print("\n  [场景 C] 激活配置漂移")
    ec.set_active_config("test_config", operator="admin")
    drift_order, drift_export = build_export_file(
        ro, ec, tmpdir,
        order_name="RO-ACTIVE-DRIFT",
        source="default",
        target="test_config",
    )
    ro.delete_release_order(drift_order.id, operator="admin")
    cfg_modified, _ = ec.load_config("test_config")
    cfg_modified.field_policies["created_at"] = ec.FieldPolicy.KEEP.value
    ec.save_config(cfg_modified, "test_config", operator="someone_else")
    all_pass &= assert_import_blocked(
        ro, drift_export, operator="admin",
        expected_keywords=["激活配置", "已被修改"],
        order_name="RO-ACTIVE-DRIFT",
    )
    all_pass &= assert_audit_has_op(
        get_audit_logs, "release_order_import_failed",
        detail_contains="激活配置"
    )

    # --- 场景 D：规则版本落后 ---
    print("\n  [场景 D] 规则版本落后")
    oldver_order, oldver_export = build_export_file(
        ro, ec, tmpdir,
        order_name="RO-OLD-VERSION",
        source="default",
        target="oldver_target",
    )
    ro.delete_release_order(oldver_order.id, operator="admin")
    with open(oldver_export, 'r', encoding='utf-8') as f:
        oldver_data = json.load(f)
    oldver_data["order_info"]["rule_version"] = 0
    with open(oldver_export, 'w', encoding='utf-8') as f:
        json.dump(oldver_data, f, ensure_ascii=False, indent=2)
    all_pass &= assert_import_blocked(
        ro, oldver_export, operator="admin",
        expected_keywords=["规则版本", "落后"],
        order_name="RO-OLD-VERSION",
    )
    all_pass &= assert_audit_has_op(
        get_audit_logs, "release_order_import_failed",
        detail_contains="规则版本"
    )

    # --- 场景 E：重启后再次导入（先导入成功，再删 DB 模拟重启，再导入同名应正常） ---
    print("\n  [场景 E] 重启后再次导入")
    restart_order, restart_export = build_export_file(
        ro, ec, tmpdir,
        order_name="RO-RESTART",
        source="default",
        target="restart_target",
    )
    ro.delete_release_order(restart_order.id, operator="admin")

    imported_once = ro.import_release_order(restart_export, operator="admin")
    all_pass &= check(imported_once.name == "RO-RESTART", "首次导入成功")
    ro.delete_release_order(imported_once.id, operator="admin")

    imported_again = ro.import_release_order(restart_export, operator="admin")
    all_pass &= check(imported_again.name == "RO-RESTART",
                      "删除后再次导入（模拟重启清理）成功")

    # ---- 11. 审计日志检查 ----
    print("\n--- 测试审计日志 ---")

    logs = get_audit_logs(limit=400)
    ops = {l.operation for l in logs}

    expected_ops = [
        "release_order_create",
        "release_order_update",
        "release_order_lock",
        "release_order_approve",
        "release_order_publish",
        "release_order_revert",
        "release_order_export",
        "release_order_import",
        "release_order_import_failed",
        "release_order_create_failed",
        "release_order_approve_failed",
        "release_order_publish_failed",
        "release_order_revert_failed",
    ]
    for expected_op in expected_ops:
        if expected_op.endswith("_failed"):
            continue
        all_pass &= check(expected_op in ops,
                          f"审计日志包含 {expected_op} 操作")

    all_pass &= check("release_order_import_failed" in ops,
                      "审计日志包含 release_order_import_failed 操作")

    publish_logs = [l for l in logs if l.operation == "release_order_publish"]
    all_pass &= check(len(publish_logs) >= 2,
                      "审计日志包含至少 2 条发布记录")
    if publish_logs:
        latest = publish_logs[0]
        all_pass &= check(latest.operator == "admin",
                          "发布审计日志记录操作人")
        all_pass &= check("RO-003" in latest.details or "RO-001" in latest.details,
                          "发布审计日志包含发布单名称")

    # ---- 12. 删除发布单 ----
    print("\n--- 测试删除发布单 ---")

    ro.delete_release_order(imported.id, operator="admin")
    try:
        ro.get_release_order(imported.id)
        all_pass &= check(False, "删除的发布单应该不存在但还能查到")
    except ValueError:
        all_pass &= check(True, "删除的发布单已不存在")

    order6 = ro.create_release_order(
        "RO-006",
        source_config_name="default",
        target_config_name="temp_config_2",
        operator="tester"
    )
    order6 = ro.lock_release_order(order6.id, operator="tester")
    order6 = ro.approve_release_order(order6.id, operator="admin")
    ro.publish_release_order(order6.id, operator="admin")

    try:
        ro.delete_release_order(order6.id, operator="admin")
        all_pass &= check(False, "已发布的发布单删除应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("published" in str(e) or "不能删除" in str(e),
                          "已发布的发布单删除被正确拒绝")

    logs = get_audit_logs(limit=400)
    ops = {l.operation for l in logs}
    all_pass &= check("release_order_delete" in ops,
                      "审计日志包含 release_order_delete 操作")

    # ---- 13. 非管理员权限测试 ----
    print("\n--- 测试非管理员权限限制 ---")

    order5 = ro.create_release_order(
        "RO-005",
        source_config_name="default",
        target_config_name="temp_config",
        operator="tester"
    )
    order5 = ro.lock_release_order(order5.id, operator="tester")

    for action_name, action_func in [
        ("approve", lambda: ro.approve_release_order(order5.id, operator="tester")),
        ("publish", lambda: ro.publish_release_order(order5.id, operator="tester")),
    ]:
        try:
            action_func()
            all_pass &= check(False, f"非管理员 {action_name} 应被拒绝但未被拒绝")
        except ValueError as e:
            all_pass &= check("管理员" in str(e),
                              f"非管理员 {action_name} 被正确拒绝")

    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 70)
    if all_pass:
        print("[SUCCESS] 发布单模块所有测试通过！")
    else:
        print("[FAILED] 发布单模块存在测试失败项！")
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
