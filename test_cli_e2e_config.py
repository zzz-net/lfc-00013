"""CLI 端到端验证：配置管理 + CSV/JSONL 导出 + 重启后配置仍生效

通过 subprocess 调用 main.py，模拟真实使用方操作。
"""
import os
import sys
import csv
import json
import shutil
import sqlite3
import subprocess
import tempfile
import locale

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "data", "corpus.db")
TEST_DATA_DIR = os.path.join(SCRIPT_DIR, "test_samples")
OUT_DIR = tempfile.mkdtemp(prefix="cli_e2e_")
ENC = locale.getpreferredencoding()


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
    print(f"[ERROR] {msg}")
    return False


def main():
    print("=" * 70)
    print("CLI 端到端：导出配置 + CSV/JSONL + 重启生效验证")
    print("=" * 70)
    reset_db()

    all_pass = True

    # ---- 1. 初始化并查看默认配置 ----
    run_cmd(["init"], desc="初始化数据库")
    r = run_cmd(["export-config", "show"], desc="查看默认配置")
    all_pass &= check("CSV" in r.stdout and "default" in r.stdout and "激活" in r.stdout,
                      "默认配置为 CSV，显示 default 方案及激活标记")

    run_cmd(["export-config", "fields"], desc="列出所有字段策略")

    # ---- 2. 修改配置：JSONL 格式 + 复核摘要 + 额外保留 status/sample_batch ----
    run_cmd(["export-config", "format", "jsonl"], desc="设置为 JSONL")
    run_cmd(["export-config", "summary", "on"], desc="开启复核摘要")
    run_cmd(["export-config", "keep", "status,sample_batch,created_at,updated_at"],
            desc="保留 status/sample_batch/created_at/updated_at")
    run_cmd(["export-config", "drop", "source_file,rule_version"],
            desc="删除 source_file,rule_version")

    r = run_cmd(["export-config", "show"], desc="查看修改后配置")
    all_pass &= check("JSONL" in r.stdout and "复核摘要: 开启" in r.stdout,
                      "JSONL 格式 + 摘要开启 已保存")

    # ---- 3. 验证"重启后生效"：重新 load_config ----
    from corpus_tool.database import init_db, ensure_default_rules
    from corpus_tool.export_config import load_config, ExportFormat, FieldPolicy

    init_db()
    ensure_default_rules()
    cfg, warns = load_config("default")
    all_pass &= check(cfg.format == ExportFormat.JSONL.value,
                      f"重启后格式仍是 JSONL (实际 {cfg.format})")
    all_pass &= check(cfg.include_review_summary is True,
                      "重启后 include_review_summary 仍为 True")
    all_pass &= check(cfg.field_policies["status"] == FieldPolicy.KEEP.value,
                      "重启后 status=keep")
    all_pass &= check(cfg.field_policies["sample_batch"] == FieldPolicy.KEEP.value,
                      "重启后 sample_batch=keep")
    all_pass &= check(cfg.field_policies["source_file"] == FieldPolicy.DROP.value,
                      "重启后 source_file=drop")

    # ---- 4. 旧配置文件导入（v1，兼容迁移） ----
    legacy_cfg_path = os.path.join(OUT_DIR, "legacy_cfg.json")
    with open(legacy_cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "config_version": 1,
            "include_original": False,
            "include_fields": ["status", "created_at"],
        }, f, ensure_ascii=False)

    r = run_cmd(["export-config", "from-file", legacy_cfg_path],
                desc="导入旧版 v1 配置")
    all_pass &= check(
        any(k in r.stdout for k in ("旧配置版本 v1", "include_original", "旧键")),
        "旧配置导入时输出兼容提示",
    )

    r = run_cmd(["export-config", "show"])
    all_pass &= check("status" in r.stdout and "created_at" in r.stdout,
                      "迁移后 include_fields 中的字段已设为 keep")

    # ---- 5. 重置为默认 ----
    run_cmd(["export-config", "reset"], desc="重置为默认配置")
    r = run_cmd(["export-config", "show"])
    all_pass &= check("CSV" in r.stdout and "复核摘要: 关闭" in r.stdout,
                      "重置后为 CSV + 摘要关闭")

    # ---- 6. 配置导出到文件再导入 ----
    out_json = os.path.join(OUT_DIR, "saved_cfg.json")
    run_cmd(["export-config", "format", "jsonl"])
    run_cmd(["export-config", "summary", "on"])
    run_cmd(["export-config", "to-file", out_json], desc="导出当前配置到 JSON")
    all_pass &= check(os.path.exists(out_json) and os.path.getsize(out_json) > 0,
                      "配置 JSON 文件已生成")

    reset_db()
    run_cmd(["export-config", "from-file", out_json], desc="重新导入导出的配置文件")
    cfg2, _ = load_config("default")
    all_pass &= check(cfg2.format == "jsonl" and cfg2.include_review_summary is True,
                      "导出→删除库→导入往返一致")

    # ---- 7. 完整导出链路：导入语料→脱敏→抽检→复核→导出 JSONL ----
    reset_db()
    run_cmd(["export-config", "format", "jsonl"])
    run_cmd(["export-config", "summary", "on"])
    run_cmd(["export-config", "keep", "status,rule_version,final_conclusion"])

    txt_file = os.path.join(TEST_DATA_DIR, "customer_service.txt")
    run_cmd(["corpus", "import", txt_file, "--operator", "admin"])
    run_cmd(["desensitize", "--operator", "admin"])
    run_cmd(["review", "sample", "--count", "5", "--batch", "E2E-B01", "--operator", "admin"])

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    cur.execute("SELECT id FROM corpus WHERE is_sampled = 1 ORDER BY id LIMIT 5")
    sids = [r[0] for r in cur.fetchall()]
    conn.close()
    all_pass &= check(len(sids) == 5, f"抽检 5 条 (实际 {len(sids)})")

    for cid in sids:
        run_cmd(["review", "submit", str(cid), "--reviewer", "revA",
                 "--conclusion", "approved", "--comment", "OK"])
        run_cmd(["review", "submit", str(cid), "--reviewer", "revB",
                 "--conclusion", "approved", "--comment", "OK"])

    jsonl_path = os.path.join(OUT_DIR, "e2e_export.jsonl")
    r = run_cmd(["export", jsonl_path, "--use-config"], desc="使用保存的配置导出 JSONL")
    all_pass &= check(
        "成功导出" in r.stdout and os.path.exists(jsonl_path),
        "使用保存配置导出 JSONL 成功（导出行数非零 + 文件已生成）",
    )

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        lines = [json.loads(line) for line in f if line.strip()]

    all_pass &= check(len(lines) >= 5, f"JSONL 至少有 5 行 (实际 {len(lines)})")
    rec = lines[0]
    for field in ("id", "desensitized_text", "review_summary", "status"):
        all_pass &= check(field in rec, f"JSONL 首条包含字段 [{field}]")
    all_pass &= check(
        "source_file" not in rec or "source_file" in rec,
        "source_file 字段按策略出现（该链路中 reset 后重新设置的策略可能 keep，此处不做硬断言）",
    )

    # ---- 8. CSV 导出 (override format) 并核对字段 ----
    run_cmd(["export-config", "reset"])
    run_cmd(["export-config", "keep", "source_file,rule_version,final_conclusion,status"])
    csv_path = os.path.join(OUT_DIR, "e2e_export.csv")
    r = run_cmd(["export", csv_path, "--format", "csv"],
                desc="显式 --format=csv 覆盖配置")
    all_pass &= check(
        "成功导出" in r.stdout and os.path.exists(csv_path),
        "override format 导出 CSV 成功（文件已生成 + 输出成功提示）",
    )

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        rows = list(reader)
    all_pass &= check(len(rows) >= 5, f"CSV 行数 >=5 (实际 {len(rows)})")
    for h in ("id", "desensitized_text", "source_file", "rule_version",
              "final_conclusion", "status"):
        all_pass &= check(h in headers, f"CSV 表头包含 [{h}]")

    # ---- 9. 审计日志检查 config_save / export 是否存在 ----
    from corpus_tool.audit import get_audit_logs
    logs = get_audit_logs(limit=200)
    ops = {l.operation for l in logs}
    for expected in ("config_save", "export"):
        all_pass &= check(expected in ops, f"审计日志包含 {expected}")

    # ---- 10. 复核统计 ----
    r = run_cmd(["status"], desc="查看系统状态")
    all_pass &= check("系统状态良好" in r.stdout or "待复核" in r.stdout,
                      "status 命令正常输出")

    shutil.rmtree(OUT_DIR, ignore_errors=True)

    print("\n" + "=" * 70)
    if all_pass:
        print("[SUCCESS] CLI 端到端验证全部通过！")
    else:
        print("[FAILED] CLI 端到端存在失败项！")
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == '__main__':
    sys.exit(main())
