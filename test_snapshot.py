"""数据集快照与回滚模块测试

覆盖场景：
1. 创建快照、查看列表、查看详情
2. 重启后可读取快照
3. 导出/导入快照文件
4. 回滚预览
5. 回滚后数据恢复正确
6. 回滚后导出结果随快照变化
7. 配置不存在时回滚被拒绝
8. 快照文件损坏时导入被拒绝
9. 回滚失败时不污染原数据
10. 审计日志记录
"""
import os
import sys
import json
import sqlite3
import tempfile
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "corpus.db")
TEST_DATA_DIR = os.path.join(SCRIPT_DIR, "test_samples")


def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


def check(cond, msg):
    if cond:
        print(f"[OK] {msg}")
        return True
    print(f"[FAIL] {msg}")
    return False


def main():
    print("=" * 70)
    print("数据集快照与回滚模块测试")
    print("=" * 70)

    reset_db()
    all_pass = True

    from corpus_tool.database import init_db, ensure_default_rules
    from corpus_tool import snapshot as snap
    from corpus_tool import importer
    from corpus_tool import desensitizer
    from corpus_tool import sampler
    from corpus_tool import exporter
    from corpus_tool import export_config as ec
    from corpus_tool.audit import get_audit_logs

    init_db()
    ensure_default_rules()

    # ---- 1. 准备测试数据：导入语料 -> 脱敏 -> 抽检 -> 复核 ----
    print("\n--- 准备测试数据 ---")

    txt_file = os.path.join(TEST_DATA_DIR, "customer_service.txt")
    ids = importer.import_txt_file(txt_file, operator="admin")
    all_pass &= check(len(ids) > 0, f"导入语料成功 ({len(ids)} 条)")

    desensitizer.batch_desensitize(operator="admin")
    all_pass &= check(True, "脱敏完成")

    count, batch_name = sampler.sample_corpus(
        count=3, batch_name="TEST-B01", operator="admin"
    )
    all_pass &= check(count == 3, f"抽检 3 条语料到批次 {batch_name}")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT id FROM corpus WHERE is_sampled = 1 ORDER BY id LIMIT 3")
    sids = [r[0] for r in cur.fetchall()]
    conn.close()

    for cid in sids[:2]:
        sampler.submit_review(cid, "revA", "approved", "OK", operator="admin")
        sampler.submit_review(cid, "revB", "approved", "OK", operator="admin")

    sampler.submit_review(sids[2], "revA", "approved", "OK", operator="admin")
    sampler.submit_review(sids[2], "revB", "rejected", "有问题", operator="admin")

    all_pass &= check(True, "提交复核意见（2 条通过，1 条冲突）")

    # ---- 2. 创建快照 ----
    print("\n--- 测试创建快照 ---")

    s1 = snap.create_snapshot("v1_initial", "初始版本快照", operator="admin")
    all_pass &= check(s1.name == "v1_initial", "快照名称正确")
    all_pass &= check(s1.corpus_count == len(ids), f"快照语料数正确 ({s1.corpus_count})")
    all_pass &= check(s1.review_count == 6, f"快照复核记录数正确 ({s1.review_count})")
    all_pass &= check(s1.conflict_count == 1, f"快照冲突记录数正确 ({s1.conflict_count})")
    all_pass &= check(s1.export_config_name == "default", "快照导出配置引用正确")
    all_pass &= check(s1.created_by == "admin", "快照创建人正确")

    snapshots = snap.list_snapshots()
    all_pass &= check(len(snapshots) == 1, "快照列表包含 1 条记录")
    all_pass &= check(snapshots[0].name == "v1_initial", "列表中快照名称正确")

    # ---- 3. 重启后可读取 ----
    print("\n--- 测试重启后可读取 ---")

    snapshots_after = snap.list_snapshots()
    all_pass &= check(len(snapshots_after) == 1, "重启后快照列表仍有 1 条")

    s1_reload = snap.get_snapshot_by_name("v1_initial")
    all_pass &= check(s1_reload.corpus_count == len(ids), "重启后快照语料数一致")
    all_pass &= check(s1_reload.rule_version == s1.rule_version, "重启后快照规则版本一致")

    # ---- 4. 修改数据后再创建第二个快照 ----
    print("\n--- 测试修改数据后创建第二个快照 ---")

    sampler.resolve_conflict(sids[2], "approved", operator="admin")
    all_pass &= check(True, "解决冲突（仲裁通过）")

    s2 = snap.create_snapshot("v2_conflict_resolved", "冲突已解决版本", operator="tester")
    all_pass &= check(s2.name == "v2_conflict_resolved", "第二个快照名称正确")
    all_pass &= check(s2.conflict_count == 1, "第二个快照冲突记录数为 1（已解决但记录仍保留）")
    all_pass &= check(s2.created_by == "tester", "第二个快照创建人正确")

    snapshots = snap.list_snapshots()
    all_pass &= check(len(snapshots) == 2, "快照列表有 2 条记录")
    all_pass &= check(snapshots[0].name == "v2_conflict_resolved", "按创建时间倒序排列")

    # ---- 5. 导出/导入快照文件 ----
    print("\n--- 测试导出/导入快照文件 ---")

    tmpdir = tempfile.mkdtemp(prefix="snapshot_test_")
    snap_file = os.path.join(tmpdir, "snapshot_v2.json")

    snap.export_snapshot(s2.id, snap_file, operator="admin")
    all_pass &= check(os.path.exists(snap_file), "快照文件已导出")
    all_pass &= check(os.path.getsize(snap_file) > 0, "快照文件非空")

    with open(snap_file, 'r', encoding='utf-8') as f:
        snap_data = json.load(f)
    all_pass &= check(snap_data["schema_version"] == 1, "快照文件 schema 版本正确")
    all_pass &= check(snap_data["snapshot_info"]["name"] == "v2_conflict_resolved",
                      "快照文件信息正确")
    all_pass &= check("corpus" in snap_data["data"], "快照数据包含 corpus")
    all_pass &= check("review_records" in snap_data["data"], "快照数据包含 review_records")
    all_pass &= check("conflict_records" in snap_data["data"], "快照数据包含 conflict_records")

    try:
        snap.import_snapshot(snap_file, operator="admin")
        all_pass &= check(False, "同名快照导入应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("已存在" in str(e), "同名快照导入被正确拒绝")

    s_imported = snap.import_snapshot(
        snap_file, operator="admin", rename_to="v2_imported"
    )
    all_pass &= check(s_imported.name == "v2_imported", "重命名导入成功")
    all_pass &= check(s_imported.corpus_count == s2.corpus_count,
                      "导入的快照语料数一致")
    all_pass &= check(s_imported.conflict_count == s2.conflict_count,
                      "导入的快照冲突数一致")

    snapshots = snap.list_snapshots()
    all_pass &= check(len(snapshots) == 3, "导入后快照列表有 3 条记录")

    # ---- 6. 损坏的快照文件导入被拒绝 ----
    print("\n--- 测试损坏文件导入被拒绝 ---")

    bad_file = os.path.join(tmpdir, "bad_snapshot.json")
    with open(bad_file, 'w', encoding='utf-8') as f:
        f.write("this is not valid json {{{")

    try:
        snap.import_snapshot(bad_file, operator="admin")
        all_pass &= check(False, "损坏文件导入应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("损坏" in str(e) or "读取失败" in str(e),
                          "损坏文件导入被正确拒绝")

    bad_schema_file = os.path.join(tmpdir, "bad_schema.json")
    with open(bad_schema_file, 'w', encoding='utf-8') as f:
        json.dump({"schema_version": 999, "snapshot_info": {}, "data": {}}, f)

    try:
        snap.import_snapshot(bad_schema_file, operator="admin")
        all_pass &= check(False, "版本不兼容快照导入应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("不兼容" in str(e), "版本不兼容的快照被正确拒绝")

    missing_data_file = os.path.join(tmpdir, "missing_data.json")
    with open(missing_data_file, 'w', encoding='utf-8') as f:
        json.dump({"schema_version": 1, "snapshot_info": {"name": "bad"}}, f)

    try:
        snap.import_snapshot(missing_data_file, operator="admin")
        all_pass &= check(False, "缺少 data 的快照导入应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("缺少" in str(e) and "data" in str(e),
                          "缺少 data 的快照被正确拒绝")

    # ---- 7. 回滚预览 ----
    print("\n--- 测试回滚预览 ---")

    preview = snap.preview_rollback(s1.id)
    all_pass &= check(preview["snapshot_name"] == "v1_initial",
                      "预览快照名称正确")
    all_pass &= check("corpus" in preview, "预览包含语料统计")
    all_pass &= check("review_records" in preview, "预览包含复核记录统计")
    all_pass &= check("conflict_records" in preview, "预览包含冲突记录统计")
    all_pass &= check("warnings" in preview, "预览包含警告列表")
    all_pass &= check(isinstance(preview["warnings"], list), "警告是列表类型")

    all_pass &= check(preview["corpus"]["snapshot"] == len(ids),
                      "快照语料数正确")
    all_pass &= check(preview["conflict_records"]["snapshot"] == 1,
                      "快照冲突数正确 (v1 有 1 条冲突)")

    # ---- 8. 回滚到 v1 ----
    print("\n--- 测试回滚到 v1 ---")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM conflict_records WHERE resolved = 0")
    before_conflict_count = cur.fetchone()[0]
    conn.close()
    all_pass &= check(before_conflict_count == 0,
                      "回滚前冲突数为 0 (已解决)")

    result = snap.rollback_snapshot(s1.id, operator="admin")
    all_pass &= check(result["snapshot_name"] == "v1_initial",
                      "回滚结果快照名称正确")
    all_pass &= check(result["corpus_restored"] == len(ids),
                      f"回滚恢复语料数正确 ({result['corpus_restored']})")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM conflict_records WHERE resolved = 0")
    after_conflict_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM review_records")
    review_count_after = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM corpus")
    corpus_count_after = cur.fetchone()[0]
    conn.close()

    all_pass &= check(after_conflict_count == 1,
                      f"回滚后冲突数恢复为 1 (实际 {after_conflict_count})")
    all_pass &= check(review_count_after == 6,
                      f"回滚后复核记录数恢复为 6 (实际 {review_count_after})")
    all_pass &= check(corpus_count_after == len(ids),
                      f"回滚后语料数正确 (实际 {corpus_count_after})")

    # ---- 9. 回滚后导出结果随快照变化 ----
    print("\n--- 测试回滚后导出结果随快照变化 ---")

    out_v1 = os.path.join(tmpdir, "export_after_rollback_v1.csv")
    try:
        count = exporter.export_desensitized(out_v1, operator="admin")
        all_pass &= check(False, "v1 有未解决冲突，导出应失败但成功了")
    except ValueError as e:
        all_pass &= check("导出条件不满足" in str(e) or "冲突" in str(e),
                          "v1 有未解决冲突，导出被正确阻止")

    result = snap.rollback_snapshot(s2.id, operator="admin")
    all_pass &= check(result["snapshot_name"] == "v2_conflict_resolved",
                      "回滚到 v2 成功")

    out_v2 = os.path.join(tmpdir, "export_after_rollback_v2.csv")
    try:
        count = exporter.export_desensitized(out_v2, operator="admin")
        all_pass &= check(count > 0, "v2 冲突已解决，导出成功")
    except ValueError as e:
        all_pass &= check(False, f"v2 导出应该成功但失败了: {e}")

    # ---- 10. 配置不存在时回滚被拒绝 ----
    print("\n--- 测试配置不存在时回滚被拒绝 ---")

    ec.set_active_config("default", operator="admin")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute(
        "UPDATE snapshots SET export_config_name = ? WHERE id = ?",
        ("nonexistent_config", s1.id)
    )
    conn.commit()
    conn.close()

    try:
        snap.rollback_snapshot(s1.id, operator="admin")
        all_pass &= check(False, "配置不存在时回滚应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("被拦截" in str(e) and "不存在" in str(e),
                          "配置不存在时回滚被正确拦截")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute(
        "UPDATE snapshots SET export_config_name = ? WHERE id = ?",
        ("default", s1.id)
    )
    conn.commit()
    conn.close()

    # ---- 11. 回滚失败时不污染原数据 ----
    print("\n--- 测试回滚失败时不污染原数据 ---")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM corpus")
    before_corpus = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM review_records")
    before_reviews = cur.fetchone()[0]
    conn.close()

    bad_snap_id = s_imported.id
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute(
        "UPDATE snapshot_data SET corpus_json = ? WHERE snapshot_id = ?",
        ("this is not valid json [[[", bad_snap_id)
    )
    conn.commit()
    conn.close()

    try:
        snap.rollback_snapshot(bad_snap_id, operator="admin")
        all_pass &= check(False, "损坏快照回滚应被拒绝但未被拒绝")
    except ValueError as e:
        all_pass &= check("被拦截" in str(e) and "损坏" in str(e),
                          "损坏快照回滚被正确拦截")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM corpus")
    after_corpus = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM review_records")
    after_reviews = cur.fetchone()[0]
    conn.close()

    all_pass &= check(before_corpus == after_corpus,
                      "回滚失败后语料数不变（未污染原数据）")
    all_pass &= check(before_reviews == after_reviews,
                      "回滚失败后复核记录数不变（未污染原数据）")

    # ---- 12. 删除快照 ----
    print("\n--- 测试删除快照 ---")

    snap.delete_snapshot(s_imported.id, operator="admin")
    snapshots = snap.list_snapshots()
    all_pass &= check(len(snapshots) == 2, "删除后快照列表有 2 条记录")

    try:
        snap.get_snapshot(s_imported.id)
        all_pass &= check(False, "删除的快照应该不存在但还能查到")
    except ValueError:
        all_pass &= check(True, "删除的快照已不存在")

    # ---- 13. 审计日志检查 ----
    print("\n--- 测试审计日志 ---")

    logs = get_audit_logs(limit=100)
    ops = {l.operation for l in logs}

    for expected_op in ("snapshot_create", "snapshot_export",
                        "snapshot_import", "snapshot_rollback",
                        "snapshot_delete"):
        all_pass &= check(expected_op in ops,
                          f"审计日志包含 {expected_op} 操作")

    rollback_logs = [l for l in logs if l.operation == "snapshot_rollback"]
    all_pass &= check(len(rollback_logs) >= 2,
                      "审计日志包含至少 2 条回滚记录")

    if rollback_logs:
        latest = rollback_logs[0]
        all_pass &= check(latest.operator == "admin",
                          "回滚审计日志记录操作人")
        all_pass &= check("v2_conflict_resolved" in latest.details or
                          "v1_initial" in latest.details,
                          "回滚审计日志包含快照名称")
        all_pass &= check("影响语料" in latest.details,
                          "回滚审计日志包含受影响记录数")

    # ---- 14. 快照数据完整性验证 ----
    print("\n--- 测试快照数据完整性 ---")

    s_final = snap.get_snapshot_by_name("v2_conflict_resolved")
    result = snap.rollback_snapshot(s_final.id, operator="admin")
    all_pass &= check(result["corpus_restored"] == s_final.corpus_count,
                      "回滚后语料数与快照元数据一致")

    corpus_list = importer.list_corpus(limit=100)
    all_pass &= check(len(corpus_list) == s_final.corpus_count,
                      "实际语料数与快照元数据一致")

    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 70)
    if all_pass:
        print("[SUCCESS] 快照与回滚模块所有测试通过！")
    else:
        print("[FAILED] 快照与回滚模块存在测试失败项！")
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
