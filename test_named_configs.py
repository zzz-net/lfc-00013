"""专项测试：命名配置方案（多方案管理）

覆盖：
1. 多方案保存与列表展示
2. 激活方案切换（use）
3. 复制方案（copy）
4. 重命名方案（rename）
5. 删除方案（delete）
6. 非法名称拦截
7. 删除/重命名 default 方案拦截
8. 导入为新方案 / 覆盖已有方案
9. 失败场景不污染原方案
10. 审计日志完整记录
11. 重启后方案及激活状态恢复
12. 切换方案后实际导出结果变化
"""
import os
import sys
import json
import glob
import sqlite3
import tempfile

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
    save_config, load_config, list_configs,
    get_active_config_name, set_active_config,
    copy_config, rename_config, delete_config,
    validate_config_name, config_exists,
    import_config_from_file, export_config_to_file,
    load_active_config,
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
    print("专项测试：命名配置方案（多方案管理）")
    print("=" * 70)

    # ------------------------------------------------------------------
    # 场景 1：名称合法性校验
    # ------------------------------------------------------------------
    print("\n【场景1】配置名称合法性校验")
    reset_database()

    valid_cases = [
        "default", "my-config", "config_v2", "prod.export", "123",
    ]
    for name in valid_cases:
        ok, errs = validate_config_name(name)
        all_pass &= check(ok, f"合法名称 [{name}] 通过校验 (errs={errs})")

    invalid_cases = [
        ("", "空名称"),
        ("  ", "空白名称"),
        (None, "None"),
        ("my config", "含空格"),
        ("my/config", "含斜杠"),
        ("my*config", "含星号"),
        (".hidden", "点号开头"),
        ("a" * 65, "超长（65字符）"),
    ]
    for name, desc in invalid_cases:
        ok, errs = validate_config_name(name)
        all_pass &= check(not ok and len(errs) > 0, f"非法名称 [{desc}] 被拦截")

    # ------------------------------------------------------------------
    # 场景 2：保存多个配置方案
    # ------------------------------------------------------------------
    print("\n【场景2】保存多个配置方案，列表展示，激活状态正确")
    reset_database()

    cfg1 = ExportConfig()
    cfg1.format = ExportFormat.CSV.value
    cfg1.field_policies["status"] = FieldPolicy.KEEP.value
    ok1, errs1, _ = save_config(cfg1, "csv-full", "admin")
    all_pass &= check(ok1, f"保存 csv-full 方案成功 (errs={errs1})")

    cfg2 = ExportConfig()
    cfg2.format = ExportFormat.JSONL.value
    cfg2.include_review_summary = True
    cfg2.field_policies["sample_batch"] = FieldPolicy.KEEP.value
    ok2, errs2, _ = save_config(cfg2, "jsonl-summary", "admin")
    all_pass &= check(ok2, f"保存 jsonl-summary 方案成功 (errs={errs2})")

    cfg3 = ExportConfig()
    cfg3.format = ExportFormat.CSV.value
    cfg3.field_policies["source_file"] = FieldPolicy.DROP.value
    cfg3.field_policies["rule_version"] = FieldPolicy.DROP.value
    ok3, errs3, _ = save_config(cfg3, "csv-minimal", "admin")
    all_pass &= check(ok3, f"保存 csv-minimal 方案成功 (errs={errs3})")

    configs = list_configs()
    names = [c["name"] for c in configs]
    all_pass &= check(len(configs) == 4, f"共 4 个方案（default + 3个新建）(实际 {len(configs)} 个: {names})")

    active = [c for c in configs if c["is_active"]]
    all_pass &= check(len(active) == 1 and active[0]["name"] == "default",
                      f"default 方案为激活方案 (实际激活: {active[0]['name'] if active else '无'})")

    active_name = get_active_config_name()
    all_pass &= check(active_name == "default", f"get_active_config_name 返回 default")

    # ------------------------------------------------------------------
    # 场景 3：切换激活方案
    # ------------------------------------------------------------------
    print("\n【场景3】切换激活方案（use）")
    reset_database()

    save_config(ExportConfig(), "scheme-a", "admin")
    save_config(ExportConfig(), "scheme-b", "admin")

    ok, errs = set_active_config("scheme-a", "admin")
    all_pass &= check(ok, f"切换到 scheme-a 成功 (errs={errs})")
    all_pass &= check(get_active_config_name() == "scheme-a", "当前激活方案为 scheme-a")

    ok2, errs2 = set_active_config("scheme-b", "admin")
    all_pass &= check(ok2, f"切换到 scheme-b 成功 (errs={errs2})")
    all_pass &= check(get_active_config_name() == "scheme-b", "当前激活方案为 scheme-b")

    ok3, errs3 = set_active_config("nonexistent", "admin")
    all_pass &= check(not ok3 and len(errs3) > 0, "切换到不存在的方案失败")
    all_pass &= check("不存在" in errs3[0], "错误信息包含'不存在'")

    all_pass &= check(get_active_config_name() == "scheme-b",
                      "切换失败后激活方案保持不变（仍是 scheme-b）")

    logs = read_audit_logs("config_activate")
    all_pass &= check(len(logs) >= 2, f"config_activate 审计日志至少 2 条 (实际 {len(logs)})")
    logs_fail = read_audit_logs("config_activate_failed")
    all_pass &= check(len(logs_fail) >= 1, "config_activate_failed 审计日志存在")

    # ------------------------------------------------------------------
    # 场景 4：复制方案
    # ------------------------------------------------------------------
    print("\n【场景4】复制配置方案（copy）")
    reset_database()

    src = ExportConfig()
    src.format = ExportFormat.JSONL.value
    src.include_review_summary = True
    src.field_policies["status"] = FieldPolicy.KEEP.value
    src.field_policies["sample_batch"] = FieldPolicy.KEEP.value
    save_config(src, "source-cfg", "admin")

    ok, errs, warns = copy_config("source-cfg", "target-cfg", "admin")
    all_pass &= check(ok, f"复制 source-cfg -> target-cfg 成功 (errs={errs})")

    target_cfg, _ = load_config("target-cfg")
    all_pass &= check(target_cfg.format == ExportFormat.JSONL.value, "复制后格式一致")
    all_pass &= check(target_cfg.include_review_summary is True, "复制后摘要开关一致")
    all_pass &= check(target_cfg.field_policies["status"] == FieldPolicy.KEEP.value,
                      "复制后字段策略 status=keep")
    all_pass &= check(target_cfg.field_policies["sample_batch"] == FieldPolicy.KEEP.value,
                      "复制后字段策略 sample_batch=keep")

    target_info = [c for c in list_configs() if c["name"] == "target-cfg"][0]
    all_pass &= check(target_info["is_active"] is False, "新复制的方案默认不激活")

    ok2, errs2, _ = copy_config("source-cfg", "target-cfg", "admin")
    all_pass &= check(not ok2 and len(errs2) > 0, "复制到已存在的目标失败（不允许覆盖")
    all_pass &= check("已存在" in errs2[0], "错误信息包含'已存在'")

    ok3, errs3, _ = copy_config("nonexistent", "new-cfg", "admin")
    all_pass &= check(not ok3 and len(errs3) > 0, "从不存在的源复制失败")

    ok4, errs4, _ = copy_config("source-cfg", "bad name!", "admin")
    all_pass &= check(not ok4 and len(errs4) > 0, "目标名称非法时复制失败")

    logs = read_audit_logs("config_copy")
    all_pass &= check(len(logs) >= 1, "config_copy 审计日志存在")
    logs_fail = read_audit_logs("config_copy_failed")
    all_pass &= check(len(logs_fail) >= 1, "config_copy_failed 审计日志存在")

    # ------------------------------------------------------------------
    # 场景 5：重命名方案
    # ------------------------------------------------------------------
    print("\n【场景5】重命名配置方案（rename）")
    reset_database()

    cfg = ExportConfig()
    cfg.format = ExportFormat.JSONL.value
    save_config(cfg, "old-name", "admin")
    set_active_config("old-name", "admin")

    ok, errs = rename_config("old-name", "new-name", "admin")
    all_pass &= check(ok, f"重命名 old-name -> new-name 成功 (errs={errs})")

    all_pass &= check(not config_exists("old-name"), "旧名称不再存在")
    all_pass &= check(config_exists("new-name"), "新名称存在")

    renamed_cfg, _ = load_config("new-name")
    all_pass &= check(renamed_cfg.format == ExportFormat.JSONL.value, "重命名后配置内容不变")

    all_pass &= check(get_active_config_name() == "new-name",
                      "重命名激活方案后，激活状态跟随新名称")

    ok2, errs2 = rename_config("default", "something", "admin")
    all_pass &= check(not ok2 and len(errs2) > 0, "不能重命名 default 方案")
    all_pass &= check("default" in errs2[0], "错误信息提到 default")

    save_config(ExportConfig(), "exists-cfg", "admin")
    ok3, errs3 = rename_config("new-name", "exists-cfg", "admin")
    all_pass &= check(not ok3 and len(errs3) > 0, "重命名到已存在的名称失败")

    ok4, errs4 = rename_config("nonexistent", "xxx", "admin")
    all_pass &= check(not ok4 and len(errs4) > 0, "重命名不存在的方案失败")

    ok5, errs5 = rename_config("exists-cfg", "bad name!", "admin")
    all_pass &= check(not ok5 and len(errs5) > 0, "新名称非法时重命名失败")

    logs = read_audit_logs("config_rename")
    all_pass &= check(len(logs) >= 1, "config_rename 审计日志存在")
    logs_fail = read_audit_logs("config_rename_failed")
    all_pass &= check(len(logs_fail) >= 1, "config_rename_failed 审计日志存在")

    # ------------------------------------------------------------------
    # 场景 6：删除方案
    # ------------------------------------------------------------------
    print("\n【场景6】删除配置方案（delete）")
    reset_database()

    save_config(ExportConfig(), "to-delete", "admin")
    all_pass &= check(config_exists("to-delete"), "删除前方案存在")

    ok, errs = delete_config("to-delete", "admin")
    all_pass &= check(ok, f"删除 to-delete 成功 (errs={errs})")
    all_pass &= check(not config_exists("to-delete"), "删除后方案不存在")

    ok2, errs2 = delete_config("default", "admin")
    all_pass &= check(not ok2 and len(errs2) > 0, "不能删除 default 方案")
    all_pass &= check("default" in errs2[0], "错误信息提到 default")
    all_pass &= check(config_exists("default"), "default 方案仍然存在")

    ok3, errs3 = delete_config("nonexistent", "admin")
    all_pass &= check(not ok3 and len(errs3) > 0, "删除不存在的方案失败")

    save_config(ExportConfig(), "active-to-delete", "admin")
    set_active_config("active-to-delete", "admin")
    all_pass &= check(get_active_config_name() == "active-to-delete", "激活方案设为 active-to-delete")

    ok4, _ = delete_config("active-to-delete", "admin")
    all_pass &= check(ok4, "删除激活方案成功")
    all_pass &= check(get_active_config_name() == "default",
                      "删除激活方案后，自动切回 default")

    logs = read_audit_logs("config_delete")
    all_pass &= check(len(logs) >= 1, "config_delete 审计日志存在")
    logs_fail = read_audit_logs("config_delete_failed")
    all_pass &= check(len(logs_fail) >= 1, "config_delete_failed 审计日志存在")

    # ------------------------------------------------------------------
    # 场景 7：导入为新方案 / 覆盖已有方案
    # ------------------------------------------------------------------
    print("\n【场景7】从文件导入：新方案 / 覆盖方案 / 冲突不污染")
    reset_database()

    good_cfg = ExportConfig()
    good_cfg.format = ExportFormat.JSONL.value
    good_cfg.include_review_summary = True
    good_cfg.field_policies["status"] = FieldPolicy.KEEP.value
    save_config(good_cfg, "original", "admin")
    set_active_config("original", "admin")

    import_file = os.path.join(TEST_OUTPUT, "import_test.json")
    with open(import_file, "w", encoding="utf-8") as f:
        json.dump({
            "config_version": 2,
            "format": "csv",
            "include_review_summary": False,
            "field_policies": {
                "id": "keep",
                "desensitized_text": "keep",
                "source_file": "drop",
                "status": "drop",
                "sample_batch": "keep",
            }
        }, f, ensure_ascii=False)

    ok_new, errs_new, _ = import_config_from_file(import_file, "imported-new", "admin", overwrite=False)
    all_pass &= check(ok_new, f"导入为新方案 imported-new 成功 (errs={errs_new})")
    all_pass &= check(config_exists("imported-new"), "新方案已创建")
    imported_cfg, _ = load_config("imported-new")
    all_pass &= check(imported_cfg.format == ExportFormat.CSV.value, "导入的新方案格式为 CSV")

    ok_exist_no_overwrite, errs_exist, _ = import_config_from_file(
        import_file, "imported-new", "admin", overwrite=False)
    all_pass &= check(not ok_exist_no_overwrite and len(errs_exist) > 0,
                      "不允许覆盖时导入到已存在方案失败（保护已有方案")

    ok_overwrite, errs_overwrite, _ = import_config_from_file(
        import_file, "original", "admin", overwrite=True)
    all_pass &= check(ok_overwrite, f"覆盖导入到 original 方案成功（显式允许覆盖时成功")

    conflict_file = os.path.join(TEST_OUTPUT, "conflict_import.json")
    with open(conflict_file, "w", encoding="utf-8") as f:
        json.dump({
            "config_version": 1,
            "include_fields": ["id", "status", "sample_batch"],
            "exclude_fields": ["status", "sample_batch"],
        }, f, ensure_ascii=False)

    original_before, _ = load_config("original")
    ok_conflict, errs_conflict, _ = import_config_from_file(
        conflict_file, "original", "admin", overwrite=True)
    all_pass &= check(not ok_conflict, "冲突配置覆盖导入失败")
    all_pass &= check(len(errs_conflict) > 0 and "同时出现在" in errs_conflict[0],
                      "错误信息包含冲突说明")

    original_after, _ = load_config("original")
    all_pass &= check(original_after.format == original_before.format,
                      "冲突导入失败后，原方案格式不变（不被污染")
    all_pass &= check(original_after.include_review_summary == original_before.include_review_summary,
                      "冲突导入失败后，原方案摘要开关不变")

    logs_fail = read_audit_logs("config_import_failed")
    all_pass &= check(len(logs_fail) >= 1, "config_import_failed 日志存在")
    logs_overwrite = read_audit_logs("config_import_overwrite")
    all_pass &= check(len(logs_overwrite) >= 1, "config_import_overwrite 日志存在")

    # ------------------------------------------------------------------
    # 场景 8：重启后方案及激活状态恢复（持久化验证）
    # ------------------------------------------------------------------
    print("\n【场景8】重启后方案列表及激活状态恢复")
    reset_database()

    cfg_a = ExportConfig()
    cfg_a.format = ExportFormat.JSONL.value
    cfg_a.field_policies["status"] = FieldPolicy.KEEP.value
    save_config(cfg_a, "scheme-a", "admin")

    cfg_b = ExportConfig()
    cfg_b.format = ExportFormat.CSV.value
    cfg_b.field_policies["source_file"] = FieldPolicy.DROP.value
    save_config(cfg_b, "scheme-b", "admin")

    set_active_config("scheme-b", "admin")

    import importlib
    import corpus_tool.export_config as ec_mod
    importlib.reload(ec_mod)

    configs_after = ec_mod.list_configs()
    all_pass &= check(len(configs_after) >= 3,
                      "重启后仍有 3 个以上方案（default + 2 个新建）")

    active_after = ec_mod.get_active_config_name()
    all_pass &= check(active_after == "scheme-b",
                      f"重启后激活方案仍是 scheme-b (实际 {active_after})")

    loaded_cfg, _ = ec_mod.load_config("scheme-a")
    all_pass &= check(loaded_cfg.format == ExportFormat.JSONL.value,
                      "重启后 scheme-a 格式仍为 JSONL")
    all_pass &= check(loaded_cfg.field_policies["status"] == FieldPolicy.KEEP.value,
                      "重启后 scheme-a 的 status=keep 策略保留")

    loaded_cfg_b, _ = ec_mod.load_config("scheme-b")
    all_pass &= check(loaded_cfg_b.format == ExportFormat.CSV.value,
                      "重启后 scheme-b 格式仍为 CSV")
    all_pass &= check(loaded_cfg_b.field_policies["source_file"] == FieldPolicy.DROP.value,
                      "重启后 scheme-b 的 source_file=drop 策略保留")

    active_cfg, warns = load_active_config()
    all_pass &= check(active_cfg.format == ExportFormat.CSV.value,
                      "load_active_config 返回 scheme-b 的 CSV 配置")
    all_pass &= check(active_cfg.field_policies["source_file"] == FieldPolicy.DROP.value,
                      "load_active_config 返回的 source_file=drop 策略")

    # ------------------------------------------------------------------
    # 场景 9：切换方案后实际导出结果变化
    # ------------------------------------------------------------------
    print("\n【场景9】切换方案切换后，实际导出文件字段/格式变化")
    reset_database()

    txt_file = os.path.join(SCRIPT_DIR, "test_samples", "customer_service.txt")
    ids = import_txt_file(txt_file, operator="admin")
    desensitizer.batch_desensitize(operator="admin")
    sample_count, batch = sampler.sample_corpus(count=5, batch_name="BATCH-NAMED", operator="admin")

    pending = sampler.list_pending_reviews()
    for c in pending:
        sampler.submit_review(c.id, "revA", "approved", operator="admin")
        sampler.submit_review(c.id, "revB", "approved", operator="admin")

    cfg_csv = ExportConfig()
    cfg_csv.format = ExportFormat.CSV.value
    cfg_csv.field_policies["status"] = FieldPolicy.KEEP.value
    cfg_csv.field_policies["sample_batch"] = FieldPolicy.KEEP.value
    cfg_csv.field_policies["source_file"] = FieldPolicy.DROP.value
    save_config(cfg_csv, "csv-slim", "admin")

    cfg_jsonl = ExportConfig()
    cfg_jsonl.format = ExportFormat.JSONL.value
    cfg_jsonl.include_review_summary = True
    cfg_jsonl.field_policies["status"] = FieldPolicy.KEEP.value
    cfg_jsonl.field_policies["final_conclusion"] = FieldPolicy.KEEP.value
    cfg_jsonl.field_policies["rule_version"] = FieldPolicy.DROP.value
    save_config(cfg_jsonl, "jsonl-full", "admin")

    set_active_config("csv-slim", "admin")
    out_csv = os.path.join(TEST_OUTPUT, "named_export_csv.csv")
    rows_csv = exporter.export_desensitized(
        out_csv, use_saved_config=True, config_name="csv-slim", operator="admin")
    all_pass &= check(rows_csv > 0, "CSV 方案导出成功")

    import csv
    with open(out_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers_csv = reader.fieldnames
        rows_csv_data = list(reader)
    all_pass &= check("status" in headers_csv, "CSV 导出含 status 字段")
    all_pass &= check("sample_batch" in headers_csv, "CSV 导出含 sample_batch 字段")
    all_pass &= check("source_file" not in headers_csv, "CSV 导出不含 source_file（已 drop")
    all_pass &= check("review_summary" not in headers_csv, "CSV 导出不含 review_summary（摘要未开）")

    set_active_config("jsonl-full", "admin")
    out_jsonl = os.path.join(TEST_OUTPUT, "named_export_jsonl.jsonl")
    rows_jsonl = exporter.export_desensitized(
        out_jsonl, use_saved_config=True, config_name="jsonl-full", operator="admin")
    all_pass &= check(rows_jsonl > 0, "JSONL 方案导出成功")

    with open(out_jsonl, "r", encoding="utf-8") as f:
        first_line = json.loads(f.readline())
    all_pass &= check("review_summary" in first_line, "JSONL 导出含 review_summary 字段（摘要开启）")
    all_pass &= check("status" in first_line, "JSONL 导出含 status 字段")
    all_pass &= check("rule_version" not in first_line, "JSONL 导出不含 rule_version（已 drop）")

    all_pass &= check(rows_csv == rows_jsonl,
                      f"两种方案导出的语料条数一致（{rows_csv} == {rows_jsonl}）")

    # ------------------------------------------------------------------
    # 场景 10：load_active_config 正常工作
    # ------------------------------------------------------------------
    print("\n【场景10】load_active_config 与默认方案回退")
    reset_database()

    active_cfg, warns = load_active_config()
    all_pass &= check(active_cfg is not None, "load_active_config 返回配置对象")
    all_pass &= check(active_cfg.format == ExportFormat.CSV.value,
                      "默认激活的是 default 方案（CSV 格式）")

    set_active_config("default", "admin")
    active_cfg2, _ = load_active_config()
    all_pass &= check(active_cfg2 is not None, "显式激活 default 后 load_active_config 正常")

    # ------------------------------------------------------------------
    # 场景 11：保存非法名称的配置被拦截
    # ------------------------------------------------------------------
    print("\n【场景11】save_config 时非法名称被拦截")
    reset_database()

    bad_cfg = ExportConfig()
    ok, errs, _ = save_config(bad_cfg, "bad name!", "admin")
    all_pass &= check(not ok and len(errs) > 0, "保存非法名称的配置失败")
    all_pass &= check("非法字符" in errs[0], "错误信息提到非法字符")

    logs_fail = read_audit_logs("config_save_failed")
    all_pass &= check(len(logs_fail) >= 1, "config_save_failed 日志包含名称非法记录")

    print()
    print("=" * 70)
    if all_pass:
        print("[SUCCESS] 所有命名配置方案专项测试通过")
        sys.exit(0)
    else:
        print("[FAILED] 存在未通过的测试用例")
        sys.exit(1)


if __name__ == "__main__":
    main()
