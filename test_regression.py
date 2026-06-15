"""回归测试：修复导出逻辑和地址脱敏规则的两个硬伤"""
import os
import sys
import csv
import shutil
import sqlite3
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_tool.database import init_db, get_connection, ensure_default_rules, DB_PATH
from corpus_tool.importer import import_txt_file
from corpus_tool.desensitizer import batch_desensitize, desensitize_text, get_active_rules, get_current_version
from corpus_tool.sampler import sample_corpus, submit_review, resolve_conflict
from corpus_tool.exporter import export_desensitized, check_export_ready, check_sensitive_leakage

TEST_DATA_DIR = os.path.join(os.path.dirname(DB_PATH), 'regression_test_data')
REGRESSION_OUTPUT = os.path.join(TEST_DATA_DIR, 'regression_export.csv')


def reset_database():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    ensure_default_rules()
    if os.path.exists(TEST_DATA_DIR):
        shutil.rmtree(TEST_DATA_DIR)
    os.makedirs(TEST_DATA_DIR, exist_ok=True)


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


def test_mixed_sensitive_data_desensitization():
    """测试：手机号、身份证、地址混排时全部脱敏"""
    print_section("回归测试1：敏感信息混排脱敏")

    test_cases = [
        {
            "text": "客户王女士，手机号13812345678，身份证110101199001011234，住址北京市朝阳区建国路88号SOHO现代城A座1201室，请联系她确认订单。",
            "contains": ["13812345678", "110101199001011234", "北京市朝阳区建国路88号SOHO现代城"]
        },
        {
            "text": "订单备注：收货人为李先生，电话15987654321，身份证440103198512125678，地址上海市浦东新区张江高科技园区博云路2号浦软大厦505室，周六送货。",
            "contains": ["15987654321", "440103198512125678", "上海市浦东新区张江高科技园区博云路2号"]
        },
        {
            "text": "投诉处理：用户反映卡号6222021234567890123被盗刷，联系电话18600001111，家庭住址广州市天河区珠江新城华夏路10号富力中心25楼，请尽快处理。",
            "contains": ["6222021234567890123", "18600001111", "广州市天河区珠江新城华夏路10号富力中心"]
        }
    ]

    rules = get_active_rules()
    all_passed = True

    for i, case in enumerate(test_cases):
        result, meta = desensitize_text(case["text"], rules)
        print(f"\n测试用例 #{i + 1}:")
        print(f"  原文: {case['text'][:80]}...")
        print(f"  脱敏: {result[:80]}...")

        case_passed = True
        for sensitive in case["contains"]:
            found = sensitive in result
            if found:
                print(f"  [ERROR] 敏感信息未脱敏: {sensitive}")
                case_passed = False
                all_passed = False
            else:
                print(f"  [OK] 已脱敏: {sensitive}")

        if not case_passed:
            all_passed = False

    return all_passed


def test_reviewed_samples_export():
    """测试：双人复核通过的reviewed样本能导出，冲突/未通过样本不能导出"""
    print_section("回归测试2：复核状态样本导出逻辑")

    test_file = os.path.join(TEST_DATA_DIR, 'regression_samples.txt')
    sample_texts = [
        "客服对话1：用户咨询退款，手机号13800000001，身份证110101199001010001，地址北京市海淀区中关村大街1号",
        "客服对话2：用户咨询退款，手机号13800000002，身份证110101199001010002，地址上海市浦东新区张江路100号",
        "客服对话3：用户咨询退款，手机号13800000003，身份证110101199001010003，地址广州市天河区天河路385号",
        "客服对话4：用户咨询退款，手机号13800000004，身份证110101199001010004，地址深圳市南山区科技园路1号",
        "客服对话5：用户咨询退款，手机号13800000005，身份证110101199001010005，地址杭州市西湖区文三路100号",
    ]
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(sample_texts))

    imported = import_txt_file(test_file, operator="test")
    check(len(imported) == 5, f"导入5条语料，实际导入{len(imported)}条")

    batch_desensitize(operator="test")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, original_text FROM corpus WHERE status = 'desensitized' ORDER BY id")
    all_corpus = cursor.fetchall()
    conn.close()

    check(len(all_corpus) == 5, f"脱敏后5条，实际{len(all_corpus)}条")

    batch_id = sample_corpus(count=5, operator="test")
    check(batch_id is not None, "抽检成功")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM corpus WHERE is_sampled = 1 ORDER BY id")
    sampled_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    check(len(sampled_ids) == 5, f"抽检5条，实际{len(sampled_ids)}条")

    print("\n--- 设置不同复核状态 ---")

    cid_approved = sampled_ids[0]
    submit_review(cid_approved, "reviewer1", "approved", "通过1", operator="test")
    submit_review(cid_approved, "reviewer2", "approved", "通过2", operator="test")
    print(f"  语料{cid_approved}: 双人approved → status=reviewed, final=approved")

    cid_rejected = sampled_ids[1]
    submit_review(cid_rejected, "reviewer1", "rejected", "拒绝1", operator="test")
    submit_review(cid_rejected, "reviewer2", "rejected", "拒绝2", operator="test")
    print(f"  语料{cid_rejected}: 双人rejected → status=reviewed, final=rejected")

    cid_conflict = sampled_ids[2]
    submit_review(cid_conflict, "reviewer1", "approved", "通过", operator="test")
    submit_review(cid_conflict, "reviewer2", "rejected", "拒绝", operator="test")
    print(f"  语料{cid_conflict}: 冲突 → status=conflict")

    cid_pending = sampled_ids[3]
    submit_review(cid_pending, "reviewer1", "approved", "通过", operator="test")
    print(f"  语料{cid_pending}: 单人复核 → status=single_review")

    cid_untouched = sampled_ids[4]
    print(f"  语料{cid_untouched}: 未复核 → status=pending_review")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, status, final_conclusion FROM corpus ORDER BY id")
    print("\n--- 复核后状态 ---")
    for row in cursor.fetchall():
        print(f"  语料{row[0]}: status={row[1]}, final={row[2]}")
    conn.close()

    print("\n--- 尝试导出（应该失败：有冲突和待复核） ---")
    try:
        export_desensitized(REGRESSION_OUTPUT, operator="test")
        print("  [ERROR] 导出未被阻止！")
        return False
    except ValueError as e:
        print(f"  [OK] 导出被正确阻止: {e}")

    print("\n--- 先解决冲突，再完成待复核 ---")
    resolve_conflict(cid_conflict, "approved", "test")
    submit_review(cid_pending, "reviewer2", "approved", "通过2", operator="test")
    submit_review(cid_untouched, "reviewer1", "approved", "通过1", operator="test")
    submit_review(cid_untouched, "reviewer2", "approved", "通过2", operator="test")

    ready, pending, conflicts = check_export_ready()
    check(ready and pending == 0 and conflicts == 0,
          f"导出就绪: ready={ready}, pending={pending}, conflicts={conflicts}")

    print("\n--- 检查各样本最终状态 ---")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, status, final_conclusion FROM corpus ORDER BY id")
    status_map = {}
    for row in cursor.fetchall():
        status_map[row[0]] = {'status': row[1], 'final': row[2]}
        print(f"  语料{row[0]}: status={row[1]}, final={row[2]}")
    conn.close()

    print("\n--- 执行导出 ---")
    export_count = export_desensitized(REGRESSION_OUTPUT, operator="test")
    print(f"  导出 {export_count} 条")

    expected_export = [
        cid for cid in sampled_ids
        if status_map[cid]['final'] == 'approved'
    ]
    not_exported_rejected = [
        cid for cid in sampled_ids
        if status_map[cid]['final'] == 'rejected'
    ]

    print(f"\n  应导出 (final=approved): {expected_export}")
    print(f"  不应导出 (final=rejected): {not_exported_rejected}")

    with open(REGRESSION_OUTPUT, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        exported_ids = [int(row['id']) for row in reader]

    print(f"  实际导出ID: {exported_ids}")

    all_passed = True
    for cid in expected_export:
        if cid not in exported_ids:
            print(f"  [ERROR] approved语料{cid}未导出！")
            all_passed = False
        else:
            print(f"  [OK] approved语料{cid}已导出")

    for cid in not_exported_rejected:
        if cid in exported_ids:
            print(f"  [ERROR] rejected语料{cid}被错误导出！")
            all_passed = False
        else:
            print(f"  [OK] rejected语料{cid}未导出（正确）")

    if export_count != len(exported_ids):
        print(f"  [ERROR] 导出数量不一致: export_count={export_count}, 文件行数={len(exported_ids)}")
        all_passed = False
    else:
        print(f"  [OK] 导出数量一致: {export_count} 条")

    return all_passed


def test_sensitive_leakage_in_export():
    """测试：导出文件中不含任何原始敏感信息"""
    print_section("回归测试3：导出文件敏感信息检查")

    issues = check_sensitive_leakage(REGRESSION_OUTPUT)

    if issues:
        for issue in issues:
            print(f"  [ERROR] {issue}")
        return False
    else:
        print(f"  [OK] 导出文件敏感信息检查通过")

    print("\n--- 手工检查导出内容中的敏感信息 ---")
    all_passed = True
    with open(REGRESSION_OUTPUT, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    phone_pattern = re.compile(r'1[3-9]\d{9}')
    id_card_pattern = re.compile(r'\d{17}[\dXx]|\d{15}')
    address_pattern = re.compile(r'(北京市|上海市|广州市|深圳市|杭州市|南京市|成都市|武汉市|西安市|重庆市)[市区][^，。！？；\n]{2,10}')

    phones_found = phone_pattern.findall(content)
    ids_found = id_card_pattern.findall(content)
    addresses_found = address_pattern.findall(content)

    if phones_found:
        print(f"  [ERROR] 发现原始手机号: {phones_found}")
        all_passed = False
    else:
        print(f"  [OK] 无原始手机号")

    if ids_found:
        print(f"  [ERROR] 发现原始身份证号: {ids_found}")
        all_passed = False
    else:
        print(f"  [OK] 无原始身份证号")

    if addresses_found:
        print(f"  [ERROR] 发现原始地址片段: {addresses_found}")
        all_passed = False
    else:
        print(f"  [OK] 无原始地址片段")

    return all_passed


def test_status_count_consistency():
    """测试：status总数、已复核数和导出行数一致"""
    print_section("回归测试4：数据一致性核对")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM corpus")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT status, COUNT(*) FROM corpus GROUP BY status")
    status_counts = dict(cursor.fetchall())

    cursor.execute("SELECT COUNT(*) FROM corpus WHERE final_conclusion = 'approved'")
    approved_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM corpus WHERE final_conclusion = 'rejected'")
    rejected_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM corpus WHERE is_sampled = 1")
    sampled_count = cursor.fetchone()[0]

    conn.close()

    with open(REGRESSION_OUTPUT, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        export_lines = len(list(reader))

    print(f"  总语料数: {total}")
    print(f"  按status分组: {status_counts}")
    print(f"  已抽检数: {sampled_count}")
    print(f"  复核通过数: {approved_count}")
    print(f"  复核拒绝数: {rejected_count}")
    print(f"  导出文件行数: {export_lines}")

    all_passed = True
    status_sum = sum(status_counts.values())
    if status_sum != total:
        print(f"  [ERROR] status分组总数{status_sum} != 总语料数{total}")
        all_passed = False
    else:
        print(f"  [OK] status分组总数 = 总语料数 = {total}")

    expected_export = status_counts.get('desensitized', 0) + status_counts.get('reviewed', 0) - rejected_count
    if export_lines != expected_export:
        print(f"  [ERROR] 导出行数{export_lines} != 预期{expected_export} "
              f"(desensitized={status_counts.get('desensitized', 0)} + "
              f"reviewed={status_counts.get('reviewed', 0)} - "
              f"rejected={rejected_count})")
        all_passed = False
    else:
        print(f"  [OK] 导出行数 = {export_lines}，与预期一致")

    if export_lines != approved_count:
        print(f"  [WARN] 导出行数{export_lines} != approved数{approved_count} "
              f"(未抽检的desensitized样本也会导出)")

    return all_passed


def main():
    print("=" * 60)
    print("离线客服语料脱敏 - 回归测试")
    print("=" * 60)

    print("\n[INFO] 重置数据库...")
    reset_database()
    print("[OK] 数据库重置完成")

    results = []
    results.append(("敏感信息混排脱敏", test_mixed_sensitive_data_desensitization()))
    results.append(("复核状态样本导出", test_reviewed_samples_export()))
    results.append(("导出文件敏感检查", test_sensitive_leakage_in_export()))
    results.append(("数据一致性核对", test_status_count_consistency()))

    print("\n" + "=" * 60)
    print("回归测试结果汇总")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "[OK]" if passed else "[ERROR]"
        print(f"  {status} {name}")
        if not passed:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("[SUCCESS] 所有回归测试通过！")
        print("=" * 60)
        return 0
    else:
        print("[FAILED] 部分回归测试未通过！")
        print("=" * 60)
        return 1


if __name__ == '__main__':
    sys.exit(main())
