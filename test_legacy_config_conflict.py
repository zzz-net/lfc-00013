"""专项测试：旧配置导入时 keep/drop 同字段冲突的拦截

覆盖：
1. 无 BOM JSON 文件中 include_fields ∩ exclude_fields 非空 → 导入失败
2. 带 BOM JSON 文件中 include_fields ∩ exclude_fields 非空 → 导入失败
3. include_original 与 include_fields/exclude_fields 矛盾 → 导入失败
4. 正常旧配置（无冲突）迁移成功，status 等字段可筛选
5. 失败日志正确写入 config_import_failed
6. 重启后配置不写入（冲突配置被拦截，库内仍是默认或之前的有效配置）
7. 正常迁移 + 导出链路，字段和行数与配置一致
"""
import glob
import json
import os
import sys
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

TEST_OUTPUT = os.path.join(SCRIPT_DIR, "test_output")
os.makedirs(TEST_OUTPUT, exist_ok=True)

from corpus_tool.database import init_db, ensure_default_rules, get_connection, DB_PATH
from corpus_tool.importer import import_txt_file
from corpus_tool import desensitizer, sampler, exporter, audit
from corpus_tool import export_config as ec
from corpus_tool.export_config import (
    ExportConfig, FieldPolicy, ExportFormat,
    import_config_from_file, save_config, load_config,
    _detect_legacy_field_conflicts,
)


def reset_database():
    for f in glob.glob(os.path.join(os.path.dirname(DB_PATH), "corpus.db*")):
        try:
            os.remove(f)
        except Exception:
            pass
    init_db()
    ensure_default_rules()


def check(cond, msg):
    if cond:
        print(f"  [PASS] {msg}")
    else:
        print(f"  [FAIL] {msg}")
    return cond


def write_json(path, data, with_bom=False):
    encoding = "utf-8-sig" if with_bom else "utf-8"
    with open(path, "w", encoding=encoding) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_audit_logs(operation=None):
    conn = get_connection()
    cursor = conn.cursor()
    if operation:
        cursor.execute(
            "SELECT operation, details, operator FROM audit_logs WHERE operation = ? ORDER BY id DESC",
            (operation,)
        )
    else:
        cursor.execute("SELECT operation, details, operator FROM audit_logs ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def main():
    all_pass = True
    print("=" * 70)
    print("专项测试：旧配置 keep/drop 同字段冲突拦截")
    print("=" * 70)

    # ------------------------------------------------------------------
    # 场景 1/2：include_fields ∩ exclude_fields 同时命中，无 BOM / 带 BOM
    # ------------------------------------------------------------------
    print("\n【场景1】无 BOM 文件：include_fields 和 exclude_fields 同时含 status/sample_batch → 导入失败")
    reset_database()

    conflict_cfg = {
        "config_version": 1,
        "format": "csv",
        "include_fields": ["id", "desensitized_text", "status", "sample_batch", "source_file"],
        "exclude_fields": ["status", "sample_batch", "created_at"],
    }
    p_no_bom = os.path.join(TEST_OUTPUT, "legacy_conflict_no_bom.json")
    write_json(p_no_bom, conflict_cfg, with_bom=False)

    ok, errs, warns = import_config_from_file(p_no_bom, "default", "tester")
    all_pass &= check(ok is False, f"导入失败 (ok={ok})")
    all_pass &= check(len(errs) == 1, f"只报告一次冲突错误 (实际 {len(errs)} 条 errs={errs})")
    all_pass &= check("status" in errs[0] and "sample_batch" in errs[0],
                      f"错误包含冲突字段 status/sample_batch (实际 errs={errs})")
    all_pass &= check("同时出现在 include_fields" in errs[0],
                      f"错误说明明确提到了 include_fields/exclude_fields 冲突")

    logs = read_audit_logs("config_import_failed")
    all_pass &= check(len(logs) >= 1, f"存在 config_import_failed 审计日志 (共 {len(logs)} 条)")
    if logs:
        all_pass &= check("status" in logs[0][1] or "sample_batch" in logs[0][1],
                          f"日志 details 包含冲突字段信息: {logs[0][1][:80]}")

    print("\n【场景2】带 BOM 文件：同样的冲突内容，UTF-8 BOM → 导入失败（同场景1同一套判断）")
    reset_database()
    p_bom = os.path.join(TEST_OUTPUT, "legacy_conflict_with_bom.json")
    write_json(p_bom, conflict_cfg, with_bom=True)

    with open(p_bom, "rb") as f:
        starts_with_bom = f.read(3) == b"\xef\xbb\xbf"
    all_pass &= check(starts_with_bom, "测试文件确实带 BOM 头")

    ok2, errs2, warns2 = import_config_from_file(p_bom, "default", "tester")
    all_pass &= check(ok2 is False, f"BOM 文件也被正确识别并导入失败 (ok={ok2})")
    all_pass &= check(len(errs2) == 1, f"BOM 错误也只报告一次 (实际 {len(errs2)} 条)")
    all_pass &= check("status" in errs2[0] and "sample_batch" in errs2[0],
                      f"BOM 文件冲突错误同样包含 status/sample_batch (实际 errs={errs2})")

    logs2 = read_audit_logs("config_import_failed")
    all_pass &= check(len(logs2) >= 1, f"BOM 失败也写入了 config_import_failed 日志")

    # ------------------------------------------------------------------
    # 场景 3：include_original 与 include_fields/exclude_fields 矛盾
    # ------------------------------------------------------------------
    print("\n【场景3】include_original=True 但 original_text 在 exclude_fields 中 → 导入失败")
    reset_database()
    conflict3 = {
        "config_version": 1,
        "include_original": True,
        "include_fields": ["id", "desensitized_text"],
        "exclude_fields": ["original_text", "status"],
    }
    p3 = os.path.join(TEST_OUTPUT, "legacy_conflict_original.json")
    write_json(p3, conflict3)

    ok3, errs3, _ = import_config_from_file(p3, "default", "tester")
    all_pass &= check(ok3 is False, f"导入失败 (ok={ok3})")
    all_pass &= check(len(errs3) == 1, f"错误只报告一次 (实际 {len(errs3)} 条)")
    all_pass &= check("include_original" in errs3[0],
                      f"错误说明提到 include_original 矛盾 (errs={errs3})")

    # ------------------------------------------------------------------
    # 场景 4/5：正常旧配置（无冲突）迁移成功，status 等字段可筛选；日志类型正确
    # ------------------------------------------------------------------
    print("\n【场景4】正常旧配置迁移成功：保留 status/final_conclusion，删除 source_file")
    reset_database()
    normal_legacy = {
        "config_version": 1,
        "format": "csv",
        "include_original": False,
        "include_fields": ["id", "desensitized_text", "status", "final_conclusion", "sample_batch"],
        "exclude_fields": ["source_file", "rule_version"],
    }
    p_normal = os.path.join(TEST_OUTPUT, "legacy_normal.json")
    write_json(p_normal, normal_legacy)

    ok4, errs4, warns4 = import_config_from_file(p_normal, "default", "tester")
    all_pass &= check(ok4 is True, f"正常旧配置导入成功 (ok={ok4}, errs={errs4})")
    cfg4, _ = load_config("default")
    all_pass &= check(cfg4.field_policies["status"] == FieldPolicy.KEEP.value,
                      "include_fields 里的 status 迁移为 keep")
    all_pass &= check(cfg4.field_policies["sample_batch"] == FieldPolicy.KEEP.value,
                      "include_fields 里的 sample_batch 迁移为 keep")
    all_pass &= check(cfg4.field_policies["source_file"] == FieldPolicy.DROP.value,
                      "exclude_fields 里的 source_file 迁移为 drop")
    all_pass &= check(cfg4.field_policies["rule_version"] == FieldPolicy.DROP.value,
                      "exclude_fields 里的 rule_version 迁移为 drop")

    logs_save = read_audit_logs("config_save")
    all_pass &= check(len(logs_save) >= 1, f"成功导入写入 config_save 日志 (共 {len(logs_save)} 条)")
    logs_failed = read_audit_logs("config_import_failed")
    all_pass &= check(len(logs_failed) == 0,
                      f"正常导入无 config_import_failed 日志 (实际 {len(logs_failed)} 条)")

    # ------------------------------------------------------------------
    # 场景 5：冲突拦截后，重启 DB，配置不变为默认（即冲突配置没有污染 DB）
    # ------------------------------------------------------------------
    print("\n【场景5】冲突导入失败后，库内不会写入冲突配置 → 默认配置仍生效")
    reset_database()

    default_before, _ = load_config("default")
    ef_before = default_before.get_effective_fields()

    p_conflict5 = os.path.join(TEST_OUTPUT, "legacy_conflict5.json")
    write_json(p_conflict5, conflict_cfg)
    ok5, _, _ = import_config_from_file(p_conflict5, "default", "tester")
    all_pass &= check(ok5 is False, "冲突导入失败")

    del default_before
    cfg5, warns5 = load_config("default")
    ef_after = cfg5.get_effective_fields()
    all_pass &= check(ef_before == ef_after,
                      f"冲突导入被拦截，保留字段与默认一致: {ef_after}")
    all_pass &= check(any("未找到已保存的配置" in w for w in warns5),
                      f"仍然提示使用默认配置 (warns={warns5})")

    # ------------------------------------------------------------------
    # 场景 6：正常迁移后配置完整链路：导入语料 → 脱敏 → 抽检 5 → 双人复核 → 导出，
    #         验证导出的 status 字段可在文件中筛出，行数、复核统计对得上
    # ------------------------------------------------------------------
    print("\n【场景6】正常迁移配置 → 完整导出链路：导出字段、行数、复核统计核对")
    reset_database()

    cfg6 = ExportConfig()
    cfg6.format = ExportFormat.CSV.value
    cfg6.include_review_summary = True
    cfg6.field_policies["status"] = FieldPolicy.KEEP.value
    cfg6.field_policies["sample_batch"] = FieldPolicy.KEEP.value
    cfg6.field_policies["final_conclusion"] = FieldPolicy.KEEP.value
    cfg6.field_policies["source_file"] = FieldPolicy.DROP.value
    cfg6.field_policies["rule_version"] = FieldPolicy.DROP.value
    ok_save, errs_save, _ = save_config(cfg6, "default", "admin")
    all_pass &= check(ok_save, f"保存 v2 配置成功 (errs={errs_save})")

    txt_file = os.path.join(SCRIPT_DIR, "test_samples", "customer_service.txt")
    ids = import_txt_file(txt_file, operator="admin")
    all_pass &= check(len(ids) == 15, f"导入 15 条语料 (实际 {len(ids)})")

    desensitizer.batch_desensitize(operator="admin")
    sample_count, batch = sampler.sample_corpus(count=5, batch_name="BATCH-LEGACY", operator="admin")
    all_pass &= check(sample_count == 5, f"抽检 5 条 (实际 {sample_count})")

    pending = sampler.list_pending_reviews()
    for c in pending:
        sampler.submit_review(c.id, "revA", "approved", operator="admin")
        sampler.submit_review(c.id, "revB", "approved", operator="admin")

    out_csv = os.path.join(TEST_OUTPUT, "legacy_export_check.csv")
    rows_exported = exporter.export_desensitized(out_csv, use_saved_config=True, operator="admin")
    all_pass &= check(rows_exported == 15, f"导出 15 行 (实际 {rows_exported})")

    import csv
    with open(out_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        rows = list(reader)
    all_pass &= check("status" in headers, f"导出表头包含 status: {headers}")
    all_pass &= check("sample_batch" in headers, "导出表头包含 sample_batch")
    all_pass &= check("review_summary" in headers, "导出表头包含 review_summary（复核摘要开启）")
    all_pass &= check("source_file" not in headers, f"source_file 已被 drop，不应出现在表头")
    all_pass &= check("rule_version" not in headers, "rule_version 已被 drop")
    all_pass &= check(len(rows) == 15, f"CSV 实际 {len(rows)} 行，与导出接口一致")

    sampled_in_file = [r for r in rows if r.get("sample_batch") == "BATCH-LEGACY"]
    all_pass &= check(len(sampled_in_file) == 5,
                      f"按 sample_batch 筛选出 5 条抽检样本 (实际 {len(sampled_in_file)})")
    all_pass &= check(all(r.get("status") == "reviewed" for r in sampled_in_file),
                      "5 条抽检样本 status 全部为 reviewed")
    all_pass &= check(all(r.get("final_conclusion") == "approved" for r in sampled_in_file),
                      "5 条抽检样本 final_conclusion 全部为 approved")
    all_pass &= check(all("revA" in r.get("review_summary", "") for r in sampled_in_file),
                      "review_summary 中包含 revA")

    stats = exporter.build_review_stats()
    all_pass &= check(stats["total_corpus"] == 15, f"复核统计 total_corpus=15 (实际 {stats['total_corpus']})")
    all_pass &= check(stats["by_final_conclusion"].get("approved", 0) == 5,
                      f"复核统计 approved=5 (实际 {stats['by_final_conclusion'].get('approved', 0)})")

    # ------------------------------------------------------------------
    # 场景 7：重启 DB 连接，配置行为与保存时一致
    # ------------------------------------------------------------------
    print("\n【场景7】重启后（关闭+重连）配置仍为 CSV + status/sample_batch 保留 + source_file 删除")
    import importlib
    import corpus_tool.database as db_mod
    importlib.reload(db_mod)
    init_db()
    ensure_default_rules()

    cfg7, _ = load_config("default")
    all_pass &= check(cfg7.format == ExportFormat.CSV.value,
                      f"重启后格式仍为 CSV (实际 {cfg7.format})")
    all_pass &= check(cfg7.include_review_summary is True, "重启后复核摘要开关仍为开启")
    all_pass &= check(cfg7.field_policies["status"] == FieldPolicy.KEEP.value,
                      "重启后 status 仍为 keep")
    all_pass &= check(cfg7.field_policies["source_file"] == FieldPolicy.DROP.value,
                      "重启后 source_file 仍为 drop")
    effective = cfg7.get_effective_fields()
    all_pass &= check("source_file" not in effective and "rule_version" not in effective,
                      "重启后有效字段不包含被 drop 的字段")

    # ------------------------------------------------------------------
    # 场景 8：直接调用 _detect_legacy_field_conflicts 覆盖边界
    # ------------------------------------------------------------------
    print("\n【场景8】边界：include_fields/exclude_fields 为空 / 非列表 / 含未知字段 不报错")
    reset_database()
    no_conflict_cases = [
        {"include_fields": ["id", "status"], "exclude_fields": ["source_file"]},
        {"include_fields": None, "exclude_fields": None},
        {"include_fields": "not_a_list", "exclude_fields": ["id"]},
        {"include_fields": ["UNKNOWN_FIELD_XX"], "exclude_fields": ["UNKNOWN_FIELD_XX"]},
        {"fields": ["status"], "exclude_fields": ["source_file"]},
    ]
    for i, case in enumerate(no_conflict_cases):
        errs = _detect_legacy_field_conflicts(case)
        all_pass &= check(len(errs) == 0, f"case {i + 1}: 无冲突检测 false positive (errs={errs})")

    real_conflict = {"include_fields": ["status", "source_file"], "exclude_fields": ["status"]}
    errs_real = _detect_legacy_field_conflicts(real_conflict)
    all_pass &= check(len(errs_real) >= 1 and "status" in errs_real[0],
                      f"真实交集被正确检测: {errs_real}")

    print()
    print("=" * 70)
    if all_pass:
        print("[SUCCESS] 所有 legacy 冲突拦截专项测试通过")
        sys.exit(0)
    else:
        print("[FAILED] 存在未通过的测试用例")
        sys.exit(1)


if __name__ == "__main__":
    main()
