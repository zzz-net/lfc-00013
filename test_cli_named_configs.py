"""CLI 端到端验证：命名配置方案（多方案管理）

通过 subprocess 调用 main.py，模拟真实使用方操作。
覆盖：list/use/copy/rename/delete、show 显示激活标记、from-file 导入新方案/覆盖、
切换方案后导出结果变化、重启恢复、失败场景不污染。
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
OUT_DIR = tempfile.mkdtemp(prefix="cli_named_")
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
    print(f"[FAIL] {msg}")
    return False


def main():
    print("=" * 70)
    print("CLI 端到端：命名配置方案（多方案管理）")
    print("=" * 70)
    reset_db()

    all_pass = True

    # ============================================================
    # 场景 1：初始化后，list/show 显示默认方案
    # ============================================================
    print("\n--- 场景 1：初始化后 list/show 显示默认方案 ---")

    run_cmd(["init"], desc="初始化数据库")

    r = run_cmd(["export-config", "list"], desc="列出所有配置方案")
    all_pass &= check("default" in r.stdout, "list 输出包含 default 方案")
    all_pass &= check("★" in r.stdout or "*" in r.stdout or "激活" in r.stdout,
                      "list 输出包含激活标记")

    r = run_cmd(["export-config", "show"], desc="查看当前激活配置")
    all_pass &= check("CSV" in r.stdout, "默认配置为 CSV 格式")
    all_pass &= check("激活" in r.stdout or "active" in r.stdout or "当前" in r.stdout,
                      "show 输出显示当前激活标记")

    # ============================================================
    # 场景 2：创建多个配置方案
    # ============================================================
    print("\n--- 场景 2：创建多个配置方案 ---")

    # 创建方案 A：JSONL + 摘要
    run_cmd(["export-config", "format", "jsonl", "--config-name", "scheme-jsonl"])
    run_cmd(["export-config", "summary", "on", "--config-name", "scheme-jsonl"])
    run_cmd(["export-config", "keep", "status,rule_version,final_conclusion",
             "--config-name", "scheme-jsonl"])
    run_cmd(["export-config", "drop", "source_file,sample_batch",
             "--config-name", "scheme-jsonl"])

    # 创建方案 B：CSV 精简
    run_cmd(["export-config", "format", "csv", "--config-name", "scheme-csv-min"])
    run_cmd(["export-config", "summary", "off", "--config-name", "scheme-csv-min"])
    run_cmd(["export-config", "keep", "status,desensitized_text",
             "--config-name", "scheme-csv-min"])
    run_cmd(["export-config", "drop", "source_file,rule_version,final_conclusion",
             "--config-name", "scheme-csv-min"])

    r = run_cmd(["export-config", "list"])
    all_pass &= check("scheme-jsonl" in r.stdout, "list 包含 scheme-jsonl")
    all_pass &= check("scheme-csv-min" in r.stdout, "list 包含 scheme-csv-min")
    all_pass &= check("default" in r.stdout, "list 仍包含 default")

    # 数一下有多少行方案（不含表头）
    lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
    all_pass &= check(len(lines) >= 3, f"至少 3 个方案（default + 2个新建）(实际行数约 {len(lines)})")

    # 当前激活仍是 default（带 ★ 的行）
    lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
    active_line = next((l for l in lines if "★" in l), "")
    all_pass &= check("default" in active_line,
                      f"default 仍是激活方案（★行内容: {active_line}）")

    # ============================================================
    # 场景 3：切换激活方案（use）
    # ============================================================
    print("\n--- 场景 3：切换激活方案（use） ---")

    r = run_cmd(["export-config", "use", "scheme-jsonl"], desc="切换到 JSONL 方案")
    all_pass &= check(r.returncode == 0, "切换到 scheme-jsonl 成功")

    r = run_cmd(["export-config", "show"], desc="查看当前激活配置")
    all_pass &= check("JSONL" in r.stdout, "切换后 show 显示 JSONL 格式")
    all_pass &= check("scheme-jsonl" in r.stdout, "show 显示方案名称 scheme-jsonl")

    r = run_cmd(["export-config", "list"])
    lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
    active_line = next((l for l in lines if "★" in l), "")
    all_pass &= check("scheme-jsonl" in active_line,
                      f"list 中 scheme-jsonl 为激活状态（★行: {active_line}）")

    # 切换到不存在的方案
    r = run_cmd(["export-config", "use", "not-exist"], check=False, desc="切换到不存在的方案")
    all_pass &= check(r.returncode != 0, "切换到不存在的方案失败")
    all_pass &= check("不存在" in r.stdout or "不存在" in r.stderr or "error" in r.stderr.lower(),
                      "错误信息说明方案不存在")

    # 失败后激活方案不变
    r = run_cmd(["export-config", "show"])
    all_pass &= check("JSONL" in r.stdout, "切换失败后激活方案仍是 scheme-jsonl")

    # ============================================================
    # 场景 4：复制方案（copy）
    # ============================================================
    print("\n--- 场景 4：复制方案（copy） ---")

    r = run_cmd(["export-config", "copy", "scheme-jsonl", "scheme-jsonl-copy"],
                desc="复制 JSONL 方案")
    all_pass &= check(r.returncode == 0, "复制方案成功")

    r = run_cmd(["export-config", "show", "--config-name", "scheme-jsonl-copy"])
    all_pass &= check("JSONL" in r.stdout, "复制的方案格式一致")

    # 复制到已存在的目标失败
    r = run_cmd(["export-config", "copy", "scheme-jsonl", "scheme-csv-min"], check=False)
    all_pass &= check(r.returncode != 0, "复制到已存在的目标失败")
    all_pass &= check("已存在" in r.stdout or "已存在" in r.stderr,
                      "错误信息说明已存在")

    # ============================================================
    # 场景 5：重命名方案（rename）
    # ============================================================
    print("\n--- 场景 5：重命名方案（rename） ---")

    r = run_cmd(["export-config", "rename", "scheme-jsonl-copy", "scheme-jsonl-v2"],
                desc="重命名方案")
    all_pass &= check(r.returncode == 0, "重命名成功")

    r = run_cmd(["export-config", "list"])
    all_pass &= check("scheme-jsonl-v2" in r.stdout, "新名称存在")
    all_pass &= check("scheme-jsonl-copy" not in r.stdout, "旧名称不存在")

    # 不能重命名 default
    r = run_cmd(["export-config", "rename", "default", "my-default"], check=False)
    all_pass &= check(r.returncode != 0, "不能重命名 default")
    all_pass &= check("default" in r.stdout.lower() or "default" in r.stderr.lower(),
                      "错误信息提到 default")

    # ============================================================
    # 场景 6：删除方案（delete）
    # ============================================================
    print("\n--- 场景 6：删除方案（delete） ---")

    r = run_cmd(["export-config", "delete", "scheme-jsonl-v2", "--yes"],
                desc="删除 scheme-jsonl-v2")
    all_pass &= check(r.returncode == 0, "删除方案成功")

    r = run_cmd(["export-config", "list"])
    all_pass &= check("scheme-jsonl-v2" not in r.stdout, "删除后方案不存在")

    # 不能删除 default
    r = run_cmd(["export-config", "delete", "default", "--yes"], check=False)
    all_pass &= check(r.returncode != 0, "不能删除 default 方案")

    # ============================================================
    # 场景 7：from-file 导入为新方案 / 覆盖方案
    # ============================================================
    print("\n--- 场景 7：from-file 导入 ---")

    # 先导出一个配置文件
    export_file = os.path.join(OUT_DIR, "exported_cfg.json")
    run_cmd(["export-config", "to-file", export_file, "--config-name", "scheme-csv-min"])

    # 导入为新方案
    r = run_cmd(["export-config", "from-file", export_file,
                 "--config-name", "imported-new", "--as-new"],
                desc="导入为新方案 imported-new")
    all_pass &= check(r.returncode == 0, "导入为新方案成功")

    r = run_cmd(["export-config", "show", "--config-name", "imported-new"])
    all_pass &= check("CSV" in r.stdout, "导入的新方案为 CSV 格式")

    # 不允许覆盖时导入到已存在方案失败
    r = run_cmd(["export-config", "from-file", export_file,
                 "--config-name", "scheme-csv-min"], check=False,
                desc="不允许覆盖时导入失败")
    all_pass &= check(r.returncode != 0, "不允许覆盖时导入失败")
    all_pass &= check("已存在" in r.stdout or "已存在" in r.stderr
                      or "覆盖" in r.stdout or "覆盖" in r.stderr,
                      "错误信息说明已存在/需显式覆盖")

    # 显式覆盖
    r = run_cmd(["export-config", "from-file", export_file,
                 "--config-name", "scheme-csv-min", "--overwrite"],
                desc="显式覆盖 scheme-csv-min")
    all_pass &= check(r.returncode == 0, "显式覆盖成功")

    # ============================================================
    # 场景 8：切换方案后，实际导出结果变化
    # ============================================================
    print("\n--- 场景 8：切换方案后实际导出结果变化 ---")

    # 导入语料
    txt_file = os.path.join(TEST_DATA_DIR, "customer_service.txt")
    run_cmd(["corpus", "import", txt_file, "--operator", "admin"])
    run_cmd(["desensitize", "--operator", "admin"])

    # 用 JSONL 方案导出
    run_cmd(["export-config", "use", "scheme-jsonl"])
    jsonl_path = os.path.join(OUT_DIR, "output.jsonl")
    run_cmd(["export", jsonl_path, "--use-config"], desc="用 JSONL 方案导出")

    all_pass &= check(os.path.exists(jsonl_path) and os.path.getsize(jsonl_path) > 0,
                      "JSONL 导出文件已生成")

    with open(jsonl_path, "r", encoding="utf-8") as f:
        jsonl_lines = [json.loads(l) for l in f if l.strip()]
    all_pass &= check(len(jsonl_lines) > 0, f"JSONL 导出 {len(jsonl_lines)} 条")

    jsonl_keys = set(jsonl_lines[0].keys())
    all_pass &= check("status" in jsonl_keys, "JSONL 导出含 status 字段")
    all_pass &= check("review_summary" in jsonl_keys or "rule_version" in jsonl_keys,
                      "JSONL 导出包含保留字段")

    # 用 CSV 精简方案导出
    run_cmd(["export-config", "use", "scheme-csv-min"])
    csv_path = os.path.join(OUT_DIR, "output_min.csv")
    run_cmd(["export", csv_path, "--use-config"], desc="用 CSV 精简方案导出")

    all_pass &= check(os.path.exists(csv_path) and os.path.getsize(csv_path) > 0,
                      "CSV 精简导出文件已生成")

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        csv_rows = list(reader)
    all_pass &= check(len(csv_rows) > 0, f"CSV 导出 {len(csv_rows)} 条")

    csv_keys = set(csv_rows[0].keys())
    all_pass &= check("status" in csv_keys, "CSV 导出含 status 字段")
    all_pass &= check("desensitized_text" in csv_keys, "CSV 导出含 desensitized_text 字段")
    all_pass &= check("source_file" not in csv_keys,
                      "CSV 精简方案不含 source_file（已 drop）")

    # 两种方案导出条数一致
    all_pass &= check(len(jsonl_lines) == len(csv_rows),
                      f"两种方案导出条数一致（{len(jsonl_lines)} == {len(csv_rows)}）")

    # ============================================================
    # 场景 9：重启后方案及激活状态恢复
    # ============================================================
    print("\n--- 场景 9：重启后方案及激活状态恢复 ---")

    # 当前激活是 scheme-csv-min
    r = run_cmd(["export-config", "show"])
    all_pass &= check("CSV" in r.stdout, "重启前激活的是 CSV 方案")

    # 模拟"重启"：重新初始化数据库连接，重新加载
    from corpus_tool.database import init_db
    from corpus_tool.export_config import (
        list_configs, get_active_config_name, load_config
    )

    init_db()

    configs = list_configs()
    config_names = [c["name"] for c in configs]
    all_pass &= check(len(configs) >= 3,
                      f"重启后仍有至少 3 个方案 (实际 {len(configs)}: {config_names})")

    active_name = get_active_config_name()
    all_pass &= check(active_name == "scheme-csv-min",
                      f"重启后激活方案仍是 scheme-csv-min (实际 {active_name})")

    cfg, _ = load_config("scheme-jsonl")
    all_pass &= check(cfg.format == "jsonl", "重启后 scheme-jsonl 格式仍为 jsonl")

    cfg2, _ = load_config("scheme-csv-min")
    all_pass &= check(cfg2.format == "csv", "重启后 scheme-csv-min 格式仍为 csv")

    # ============================================================
    # 场景 10：现有单方案用法不回退（default 方案的默认行为）
    # ============================================================
    print("\n--- 场景 10：单方案用法兼容（default 默认行为） ---")

    # 重置数据库，使用默认方案
    reset_db()
    run_cmd(["init"])

    # 不指定 config-name 时操作 default
    run_cmd(["export-config", "format", "jsonl"])
    run_cmd(["export-config", "summary", "on"])

    r = run_cmd(["export-config", "show"])
    all_pass &= check("JSONL" in r.stdout, "不指定 config-name 时修改 default 方案")

    # 默认导出使用 default 方案
    run_cmd(["corpus", "import", txt_file, "--operator", "admin"])
    run_cmd(["desensitize", "--operator", "admin"])

    default_jsonl = os.path.join(OUT_DIR, "default_out.jsonl")
    run_cmd(["export", default_jsonl, "--use-config"])
    all_pass &= check(os.path.exists(default_jsonl) and os.path.getsize(default_jsonl) > 0,
                      "使用激活配置导出成功")

    with open(default_jsonl, "r", encoding="utf-8") as f:
        first_line = json.loads(f.readline())
    all_pass &= check("review_summary" in first_line,
                      "default 方案开启摘要后，导出含 review_summary")

    # ============================================================
    # 汇总
    # ============================================================
    print("\n" + "=" * 70)
    if all_pass:
        print("[SUCCESS] 所有 CLI 端到端命名配置方案测试通过")
    else:
        print("[FAILURE] 部分测试未通过")
    print("=" * 70)

    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
