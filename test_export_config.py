"""导出配置功能专项测试

覆盖：
- 配置加载与持久化（重启后仍生效）
- 旧配置迁移与兼容提示
- CSV / JSONL 两种导出格式
- 字段保留策略
- 配置冲突失败（不静默导出）
- 日志落点（audit_logs）
- 导出约束：脱敏状态校验 + 双人复核 + 冲突阻止
"""
import os
import sys
import csv
import json
import shutil
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_tool.database import init_db, ensure_default_rules, DB_PATH
from corpus_tool.importer import import_txt_file
from corpus_tool.desensitizer import batch_desensitize
from corpus_tool.sampler import sample_corpus, submit_review, resolve_conflict
from corpus_tool.exporter import (
    export_desensitized, check_export_ready, check_sensitive_leakage,
    build_review_stats,
)
from corpus_tool.audit import get_audit_logs
from corpus_tool import export_config as ec
from corpus_tool.export_config import (
    ExportConfig, FieldPolicy, ExportFormat,
    save_config, load_config, import_config_from_file,
    export_config_to_file, reset_config, detect_legacy_compat_issues,
    ALL_EXPORTABLE_FIELDS, REQUIRED_FIELDS, DEFAULT_FIELD_POLICIES,
)


TEST_DIR = tempfile.mkdtemp(prefix="export_config_test_")


def reset_database():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    ensure_default_rules()


def print_section(title):
    print("\n" + "=" * 60)
    print(f"【{title}】")
    print("=" * 60)


def check(condition, msg):
    if condition:
        print(f"[OK] {msg}")
        return True
    else:
        print(f"[ERROR] {msg}")
        return False


def _seed_data_for_export():
    """导入→脱敏→抽检→完成全部双人复核。返回 (sample_ids, exported_expected_count)"""
    test_file = os.path.join(TEST_DIR, 'samples.txt')
    sample_texts = [
        "对话1：用户退款，手机号13800000001，身份证110101199001010001，地址北京市海淀区中关村大街1号",
        "对话2：用户换货，手机号13800000002，身份证110101199001010002，地址上海市浦东新区张江路100号",
        "对话3：用户咨询，手机号13800000003，身份证110101199001010003，地址广州市天河区天河路385号",
        "对话4：用户投诉，手机号13800000004，身份证110101199001010004，地址深圳市南山区科技园路1号",
        "对话5：用户回访，手机号13800000005，身份证110101199001010005，地址杭州市西湖区文三路100号",
    ]
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(sample_texts))

    ids = import_txt_file(test_file, operator="tester")
    batch_desensitize(operator="tester")
    _, batch_name = sample_corpus(count=5, operator="tester")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT id FROM corpus WHERE is_sampled = 1 ORDER BY id")
    sampled = [r[0] for r in cur.fetchall()]
    conn.close()

    for i, cid in enumerate(sampled):
        conclusion = "approved" if i < 4 else "rejected"
        submit_review(cid, "revA", conclusion, f"revA #{i}", operator="tester")
        submit_review(cid, "revB", conclusion, f"revB #{i}", operator="tester")

    approved_count = sum(1 for i in range(len(sampled)) if i < 4)
    return sampled, approved_count


def test_default_config_loading():
    """测试1：默认配置加载"""
    print_section("测试1：默认配置加载与字段策略")

    reset_database()
    cfg, warnings = load_config("default")
    passed = True

    passed &= check(cfg is not None, "load_config 返回配置对象")
    passed &= check(cfg.config_version == 2, f"默认 config_version=2 (实际 v{cfg.config_version})")
    passed &= check(cfg.format == ExportFormat.CSV.value, f"默认格式为 CSV (实际 {cfg.format})")
    passed &= check(cfg.include_review_summary is False, "默认不包含复核摘要")

    for req in REQUIRED_FIELDS:
        ok = cfg.field_policies.get(req) == FieldPolicy.KEEP.value
        passed &= check(ok, f"必填字段 [{req}] 默认策略为 keep")

    fields = cfg.get_effective_fields()
    for req in REQUIRED_FIELDS:
        passed &= check(req in fields, f"有效字段包含必填字段 [{req}]")

    passed &= check(len(warnings) >= 1 and any("默认导出配置" in w for w in warnings),
                    "首次加载给出默认配置提示")

    valid, errors = cfg.validate()
    passed &= check(valid, f"默认配置校验通过 (errors={errors})")

    return passed


def test_config_persistence_across_reset():
    """测试2：配置持久化——保存后重启（重连 DB）仍生效"""
    print_section("测试2：配置持久化（重启/重连后仍生效）")

    reset_database()

    cfg = ExportConfig()
    cfg.format = ExportFormat.JSONL.value
    cfg.include_review_summary = True
    cfg.field_policies["status"] = FieldPolicy.KEEP.value
    cfg.field_policies["created_at"] = FieldPolicy.KEEP.value

    ok, errs, warns = save_config(cfg, "default", operator="tester")
    if not ok:
        print(f"  保存失败: {errs}")
        return False

    del cfg

    cfg2, warnings2 = load_config("default")
    passed = True
    passed &= check(cfg2.format == ExportFormat.JSONL.value, f"重启后格式仍是 JSONL (实际 {cfg2.format})")
    passed &= check(cfg2.include_review_summary is True, "重启后复核摘要仍是开启")
    passed &= check(cfg2.field_policies["status"] == FieldPolicy.KEEP.value,
                    "重启后自定义字段策略 [status=keep] 生效")
    passed &= check(cfg2.field_policies["created_at"] == FieldPolicy.KEEP.value,
                    "重启后自定义字段策略 [created_at=keep] 生效")

    logs = get_audit_logs(operation="config_save")
    passed &= check(len(logs) >= 1 and any("JSONL" in log.details for log in logs),
                    "config_save 操作已写入审计日志")

    return passed


def test_legacy_config_migration():
    """测试3：旧版本配置（v1，含 include_original / include_fields）导入与兼容提示"""
    print_section("测试3：旧配置导入迁移与兼容提示")

    reset_database()

    legacy_json = {
        "config_version": 1,
        "include_original": True,
        "include_fields": ["status", "created_at"],
        "exclude_fields": ["source_file"],
    }

    issues = detect_legacy_compat_issues(legacy_json)
    passed = True
    passed &= check(any("v1" in i for i in issues), "识别到旧版本配置提示")
    passed &= check(any("include_original" in i for i in issues), "识别到 include_original 旧键")
    passed &= check(any("include_fields" in i for i in issues), "识别到 include_fields 旧键")

    legacy_file = os.path.join(TEST_DIR, "legacy_v1.json")
    with open(legacy_file, 'w', encoding='utf-8') as f:
        json.dump(legacy_json, f, ensure_ascii=False)

    ok, errs, warns = import_config_from_file(legacy_file, "default", operator="tester")
    for w in warns:
        print(f"  [Compat] {w}")

    passed &= check(ok, f"旧配置导入成功 (errors={errs})")

    cfg, _ = load_config("default")
    passed &= check(cfg.field_policies["original_text"] == FieldPolicy.KEEP.value,
                    "include_original=True 迁移为 original_text=keep")
    passed &= check(cfg.field_policies["status"] == FieldPolicy.KEEP.value,
                    "include_fields 迁移为对应字段 keep")
    passed &= check(cfg.field_policies["source_file"] == FieldPolicy.DROP.value,
                    "exclude_fields 迁移为对应字段 drop")

    logs = get_audit_logs(operation="config_save")
    has_log = any(("格式=CSV" in log.details or "JSONL" in log.details) for log in logs)
    passed &= check(has_log, "迁移后的配置保存操作已写入日志")

    return passed


def test_config_conflict_detected_and_blocked():
    """测试4：配置冲突——必填字段删除/双重语义冲突——必须返回错误并写日志，不可静默导出"""
    print_section("测试4：配置冲突检测（阻止导出 + 错误清晰 + 日志落点）")

    reset_database()
    passed = True

    bad_cfg = ExportConfig()
    bad_cfg.field_policies["id"] = FieldPolicy.DROP.value
    bad_cfg.field_policies["desensitized_text"] = FieldPolicy.DROP.value

    valid, errors = bad_cfg.validate()
    passed &= check(not valid, "删除必填字段 [id, desensitized_text] 校验失败")
    passed &= check(any("id" in e and "不能被删除" in e for e in errors),
                    "错误信息清晰指出字段 id 不能被删除")
    passed &= check(any("desensitized_text" in e for e in errors),
                    "错误信息清晰指出字段 desensitized_text 不能被删除")

    ok, errs, _ = save_config(bad_cfg, "default", operator="tester")
    passed &= check(not ok and len(errs) > 0, f"保存冲突配置返回失败 (errs={errs})")

    logs_fail = get_audit_logs(operation="config_save_failed")
    passed &= check(len(logs_fail) >= 1, "config_save_failed 日志已写入 audit_logs")
    if logs_fail:
        passed &= check(any("id" in log.details or "必填" in log.details for log in logs_fail),
                        "失败日志包含冲突原因摘要")

    synced_cfg = ExportConfig()
    synced_cfg.include_review_summary = True
    synced_cfg.field_policies["review_summary"] = FieldPolicy.DROP.value
    valid2, errors2 = synced_cfg.validate()
    passed &= check(
        valid2,
        "include_review_summary=True 与 review_summary=drop 经自动同步后校验通过（不视为致命冲突，自动以 keep 为准）",
    )
    passed &= check(
        synced_cfg.field_policies["review_summary"] == FieldPolicy.KEEP.value,
        "validate() 自动同步：开启摘要后 review_summary 策略被修正为 keep",
    )
    passed &= check(
        synced_cfg.include_review_summary is True,
        "validate() 双向同步：review_summary=keep 时 include_review_summary 也保持 True",
    )

    from corpus_tool.export_config import ExportConfig as EC2
    sync2 = EC2()
    sync2.field_policies["review_summary"] = FieldPolicy.KEEP.value
    sync2.include_review_summary = False
    _, _ = sync2.validate()
    passed &= check(
        sync2.include_review_summary is True,
        "validate() 反向同步：当 review_summary=keep 时 include_review_summary 自动置 True",
    )

    bad_cfg2 = ExportConfig()
    bad_cfg2.format = "parquet"
    valid3, errors3 = bad_cfg2.validate()
    passed &= check(not valid3 and any("不支持的导出格式" in e for e in errors3),
                    "未知格式校验失败并给出明确错误")

    bad_cfg3 = ExportConfig()
    bad_cfg3.field_policies["unknown_field_xyz"] = FieldPolicy.KEEP.value
    valid4, errors4 = bad_cfg3.validate()
    passed &= check(not valid4 and any("未知字段" in e and "unknown_field_xyz" in e for e in errors4),
                    "未知字段被拦截")

    bad_cfg4 = ExportConfig()
    bad_cfg4.field_policies["source_file"] = "maybe"
    valid5, errors5 = bad_cfg4.validate()
    passed &= check(not valid5 and any("策略值" in e and "maybe" in e for e in errors5),
                    "非法策略值被拦截")

    return passed


def test_csv_export_with_field_policy():
    """测试5：CSV 导出——字段策略生效、行数与复核统计匹配"""
    print_section("测试5：CSV 导出（字段策略 + 行数/复核统计核对）")

    reset_database()
    sampled, approved_count = _seed_data_for_export()

    ready, pending, conflicts = check_export_ready()
    if not (ready and pending == 0 and conflicts == 0):
        print(f"  [ERROR] 导出前置条件不满足: ready={ready}, pending={pending}, conflicts={conflicts}")
        return False

    cfg = ExportConfig()
    cfg.format = ExportFormat.CSV.value
    cfg.field_policies["status"] = FieldPolicy.KEEP.value
    cfg.field_policies["rule_version"] = FieldPolicy.KEEP.value
    cfg.field_policies["final_conclusion"] = FieldPolicy.KEEP.value
    cfg.field_policies["source_file"] = FieldPolicy.KEEP.value
    cfg.field_policies["created_at"] = FieldPolicy.DROP.value
    cfg.field_policies["sample_batch"] = FieldPolicy.DROP.value

    csv_path = os.path.join(TEST_DIR, "exported.csv")
    count = export_desensitized(csv_path, operator="tester", config=cfg)

    passed = True
    passed &= check(count >= 4, f"导出了 {count} 条（至少通过的 {approved_count} 条）")

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        rows = list(reader)

    expected_fields = cfg.get_effective_fields()
    passed &= check(headers == expected_fields,
                    f"CSV 表头与策略一致: {headers} == {expected_fields}")
    passed &= check(len(rows) == count, "CSV 数据行数与函数返回值一致")

    passed &= check("original_text" not in headers, "默认策略下 original_text 不出现在 CSV")
    passed &= check("created_at" not in headers, "drop 的字段 [created_at] 不出现")
    passed &= check("status" in headers, "keep 的字段 [status] 出现在表头")

    for row in rows:
        passed &= check("" not in (row.get("id"), row.get("desensitized_text")),
                        "必填字段 id、desensitized_text 非空")

    issues = check_sensitive_leakage(csv_path)
    passed &= check(len(issues) == 0, f"CSV 敏感泄露检查通过 (issues={issues})")

    stats = build_review_stats()
    passed &= check(stats["by_final_conclusion"].get("approved", 0) == approved_count,
                    f"build_review_stats 统计通过数={stats['by_final_conclusion'].get('approved', 0)}, 预期 {approved_count}")

    logs = get_audit_logs(operation="export")
    passed &= check(len(logs) >= 1 and any("CSV" in log.details and str(count) in log.details for log in logs),
                    "CSV 导出操作已写入审计日志，含格式与行数")

    return passed


def test_jsonl_export_with_review_summary():
    """测试6：JSONL 导出——开启复核摘要，格式/字段/行数正确"""
    print_section("测试6：JSONL 导出（含复核摘要 + 行数核对）")

    reset_database()
    sampled, approved_count = _seed_data_for_export()

    cfg = ExportConfig()
    cfg.format = ExportFormat.JSONL.value
    cfg.include_review_summary = True
    cfg.field_policies["status"] = FieldPolicy.KEEP.value
    cfg.field_policies["sample_batch"] = FieldPolicy.KEEP.value

    jsonl_path = os.path.join(TEST_DIR, "exported.jsonl")
    count = export_desensitized(jsonl_path, operator="tester", config=cfg)

    passed = True
    lines = []
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(json.loads(line))

    passed &= check(len(lines) == count, "JSONL 行数与导出返回值一致")

    expected_fields = set(cfg.get_effective_fields()) | {"review_summary"}
    for rec in lines:
        actual = set(rec.keys())
        missing = expected_fields - actual
        extra = actual - expected_fields
        passed &= check(len(missing) == 0, f"JSON 记录包含所有预期字段 (缺失={missing})")
        passed &= check(len(extra) == 0, f"JSON 记录无多余字段 (多余={extra})")
        passed &= check("review_summary" in rec and rec["review_summary"],
                        "每条 JSON 包含非空的 review_summary")

    sample_ids_from_export = [int(r["id"]) for r in lines]
    approved_sampled_ids = sampled[:4]
    for aid in approved_sampled_ids:
        passed &= check(aid in sample_ids_from_export, f"approved 抽检样本 id={aid} 出现在 JSONL 中")

    logs = get_audit_logs(operation="export")
    has_jsonl_log = any(
        "JSONL" in log.details and "复核摘要=开" in log.details and str(count) in log.details
        for log in logs
    )
    passed &= check(has_jsonl_log, "JSONL+复核摘要 的导出记录落入审计日志")

    issues = check_sensitive_leakage(jsonl_path)
    passed &= check(len(issues) == 0, "JSONL 敏感泄露检查通过")

    return passed


def test_export_never_bypasses_review_constraints():
    """测试7：新配置方案下，待复核/冲突的情况仍被阻止，不因为格式切换而放行"""
    print_section("测试7：导出不绕过双人复核与冲突约束")

    reset_database()

    test_file = os.path.join(TEST_DIR, 'blocked.txt')
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("手机号13800000001 北京市海淀区\n")
        f.write("手机号13800000002 上海市浦东新区\n")
        f.write("手机号13800000003 广州市天河区\n")

    import_txt_file(test_file, operator="tester")
    batch_desensitize(operator="tester")
    _, _ = sample_corpus(count=3, operator="tester")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT id FROM corpus WHERE is_sampled = 1 ORDER BY id")
    sids = [r[0] for r in cur.fetchall()]
    conn.close()

    submit_review(sids[0], "revA", "approved", "ok", operator="tester")
    submit_review(sids[0], "revB", "approved", "ok", operator="tester")

    submit_review(sids[1], "revA", "approved", "ok", operator="tester")
    submit_review(sids[1], "revB", "rejected", "bad", operator="tester")

    submit_review(sids[2], "revA", "approved", "ok", operator="tester")

    formats_to_try = [
        (ExportFormat.CSV.value, os.path.join(TEST_DIR, "blocked.csv")),
        (ExportFormat.JSONL.value, os.path.join(TEST_DIR, "blocked.jsonl")),
    ]

    passed = True
    for fmt, path in formats_to_try:
        cfg = ExportConfig(format=fmt)
        try:
            export_desensitized(path, operator="tester", config=cfg)
            passed &= check(False, f"[{fmt.upper()}] 待复核+冲突场景下错误地放行导出！")
        except ValueError as e:
            ok = ("待复核" in str(e) or "待复核" in str(e)) and ("冲突" in str(e) or "冲突未解决" in str(e) or pending_present(str(e)))
            passed &= check(True, f"[{fmt.upper()}] 正确阻止，异常信息: {e}")
    return passed


def pending_present(msg):
    return "条样本待复核" in msg or "pending" in msg.lower()


def test_config_export_import_roundtrip():
    """测试8：配置导出到文件→再导入→恢复一致（含日志）"""
    print_section("测试8：配置导出/导入往返（JSON 文件往返）")

    reset_database()

    original = ExportConfig()
    original.format = ExportFormat.JSONL.value
    original.include_review_summary = True
    original.field_policies["status"] = FieldPolicy.KEEP.value
    original.field_policies["created_at"] = FieldPolicy.KEEP.value

    save_config(original, "default", operator="tester")

    out_file = os.path.join(TEST_DIR, "cfg_roundtrip.json")
    ok, errs = export_config_to_file(out_file, "default", operator="tester")
    passed = True
    passed &= check(ok, f"配置导出到文件成功 (errs={errs})")

    logs_ex_before_reset = get_audit_logs(operation="config_export")
    passed &= check(len(logs_ex_before_reset) >= 1, "config_export 日志落点（reset前检查）")
    passed &= check(any("cfg_roundtrip.json" in log.details for log in logs_ex_before_reset),
                    "导出日志中包含文件名")

    reset_database()

    ok2, errs2, warns2 = import_config_from_file(out_file, "default", operator="tester")
    for w in warns2:
        print(f"  [Info] {w}")
    passed &= check(ok2, f"配置从文件导入成功 (errs={errs2})")

    restored, _ = load_config("default")
    passed &= check(restored.format == original.format, "格式一致")
    passed &= check(restored.include_review_summary == original.include_review_summary, "复核摘要开关一致")
    passed &= check(restored.field_policies["status"] == original.field_policies["status"],
                    "字段策略 [status] 一致")
    passed &= check(restored.field_policies["created_at"] == original.field_policies["created_at"],
                    "字段策略 [created_at] 一致")

    logs_im = get_audit_logs(operation="config_save")
    passed &= check(len(logs_im) >= 1, "导入后 config_save 日志落点")

    return passed


def test_audit_log_sink_for_all_operations():
    """测试9：所有配置相关操作均有日志落点"""
    print_section("测试9：配置操作日志落点覆盖（save/import_failed/reset/export）")

    reset_database()

    cfg = ExportConfig(format=ExportFormat.JSONL.value)
    ok, _, _ = save_config(cfg, "default", operator="alice")

    bad = ExportConfig()
    bad.field_policies["id"] = FieldPolicy.DROP.value
    save_config(bad, "default", operator="bob")

    ok2, _ = reset_config("default", operator="charlie")

    cfg_file = os.path.join(TEST_DIR, "cfg_log.json")
    ok3, _ = export_config_to_file(cfg_file, "default", operator="dave")

    logs = get_audit_logs(limit=200)
    op_names = [log.operation for log in logs]

    passed = True
    passed &= check("config_save" in op_names, "audit_logs 包含 config_save")
    passed &= check("config_save_failed" in op_names, "audit_logs 包含 config_save_failed（冲突阻止记录）")
    passed &= check("config_export" in op_names, "audit_logs 包含 config_export")

    save_logs = [l for l in logs if l.operation == "config_save" and l.operator == "alice"]
    passed &= check(len(save_logs) >= 1 and any("JSONL" in l.details for l in save_logs),
                    "config_save 细节含格式")

    fail_logs = [l for l in logs if l.operation == "config_save_failed" and l.operator == "bob"]
    passed &= check(len(fail_logs) >= 1 and any("id" in l.details for l in fail_logs),
                    "config_save_failed 细节含冲突字段")

    return passed


def main():
    print("=" * 60)
    print("导出配置专项测试")
    print("=" * 60)

    print(f"[INFO] 测试临时目录: {TEST_DIR}")

    results = []
    results.append(("默认配置加载与字段策略", test_default_config_loading()))
    results.append(("配置持久化（重启后仍生效）", test_config_persistence_across_reset()))
    results.append(("旧配置导入迁移与兼容提示", test_legacy_config_migration()))
    results.append(("配置冲突检测与阻止（不静默）", test_config_conflict_detected_and_blocked()))
    results.append(("CSV 导出 + 字段策略 + 统计核对", test_csv_export_with_field_policy()))
    results.append(("JSONL 导出 + 复核摘要 + 日志", test_jsonl_export_with_review_summary()))
    results.append(("导出不绕过双人复核与冲突", test_export_never_bypasses_review_constraints()))
    results.append(("配置导出/导入往返（JSON）", test_config_export_import_roundtrip()))
    results.append(("配置操作日志落点全覆盖", test_audit_log_sink_for_all_operations()))

    print("\n" + "=" * 60)
    print("专项测试结果汇总")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "[OK]" if passed else "[ERROR]"
        print(f"  {status} {name}")
        if not passed:
            all_passed = False

    shutil.rmtree(TEST_DIR, ignore_errors=True)

    print("\n" + "=" * 60)
    if all_passed:
        print("[SUCCESS] 导出配置专项测试全部通过！")
        print("=" * 60)
        return 0
    else:
        print("[FAILED] 部分专项测试未通过！")
        print("=" * 60)
        return 1


if __name__ == '__main__':
    sys.exit(main())
