"""命令行接口"""
import click
import sys
import os
from tabulate import tabulate
from colorama import init, Fore, Style

from .database import init_db, ensure_default_rules, DB_PATH
from . import importer
from . import desensitizer
from . import exporter
from . import sampler
from . import rules
from . import audit

init(autoreset=True)


def _print_success(msg):
    click.echo(Fore.GREEN + "[OK] " + msg)


def _print_error(msg):
    click.echo(Fore.RED + "[ERROR] " + msg)


def _print_warning(msg):
    click.echo(Fore.YELLOW + "[WARN] " + msg)


def _print_info(msg):
    click.echo(Fore.CYAN + "[INFO] " + msg)


@click.group(help="离线客服语料脱敏与抽检工作台")
@click.version_option("1.0.0")
def cli():
    init_db()
    ensure_default_rules()


@cli.command(help="初始化系统数据库")
def init():
    click.echo(f"数据库路径: {DB_PATH}")
    _print_success("系统初始化完成")


@cli.group(help="语料数据管理")
def corpus():
    pass


@corpus.command("import", help="导入语料数据 (.txt 或 .csv)")
@click.argument('file_path', type=click.Path(exists=True))
@click.option('--operator', default="admin", help="操作人")
@click.option('--csv-column', default="text", help="CSV文件的文本列名")
def corpus_import(file_path, operator, csv_column):
    try:
        if file_path.endswith('.csv'):
            ids = importer.import_csv_file(file_path, text_column=csv_column, operator=operator)
        else:
            ids = importer.import_file(file_path, operator=operator)
        _print_success(f"成功导入 {len(ids)} 条语料，ID范围: {ids[0]} - {ids[-1]}")
    except Exception as e:
        _print_error(f"导入失败: {str(e)}")
        sys.exit(1)


@corpus.command("list", help="列出语料数据")
@click.option('--status', default=None, help="按状态过滤: imported/desensitized/pending_review/reviewed")
@click.option('--limit', default=20, help="显示数量")
def corpus_list(status, limit):
    data = importer.list_corpus(status=status, limit=limit)
    if not data:
        _print_info("没有找到语料数据")
        return
    table_data = []
    for c in data:
        original = c.original_text[:40] + "..." if len(c.original_text) > 40 else c.original_text
        desensitized = c.desensitized_text[:40] + "..." if c.desensitized_text and len(c.desensitized_text) > 40 else (c.desensitized_text or "")
        table_data.append([
            c.id, c.status, c.rule_version,
            Fore.MAGENTA + original + Style.RESET_ALL,
            Fore.GREEN + desensitized + Style.RESET_ALL,
            c.sample_batch or "-",
            c.final_conclusion or "-",
        ])
    headers = ["ID", "状态", "规则版本", "原文", "脱敏后", "抽检批次", "最终结论"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@corpus.command("show", help="显示单条语料详情")
@click.argument('corpus_id', type=int)
def corpus_show(corpus_id):
    try:
        c = importer.get_corpus(corpus_id)
        click.echo(f"\n{Fore.CYAN}=== 语料详情 ID: {c.id} ==={Style.RESET_ALL}")
        click.echo(f"状态:         {c.status}")
        click.echo(f"来源文件:     {c.source_file}")
        click.echo(f"规则版本:     v{c.rule_version}")
        click.echo(f"是否抽检:     {'是' if c.is_sampled else '否'}")
        click.echo(f"抽检批次:     {c.sample_batch or '-'}")
        click.echo(f"最终结论:     {c.final_conclusion or '-'}")
        click.echo(f"创建时间:     {c.created_at}")
        click.echo(f"更新时间:     {c.updated_at}")
        click.echo(f"\n{Fore.MAGENTA}原文:{Style.RESET_ALL}")
        click.echo(c.original_text)
        if c.desensitized_text:
            click.echo(f"\n{Fore.GREEN}脱敏后:{Style.RESET_ALL}")
            click.echo(c.desensitized_text)
        reviews = sampler.get_review_records(corpus_id)
        if reviews:
            click.echo(f"\n{Fore.YELLOW}复核记录:{Style.RESET_ALL}")
            for r in reviews:
                click.echo(f"  [{r.created_at}] {r.reviewer} -> {r.conclusion} (v{r.rule_version_at_review})")
                if r.comment:
                    click.echo(f"      备注: {r.comment}")
    except Exception as e:
        _print_error(str(e))
        sys.exit(1)


@cli.command(help="执行脱敏处理")
@click.option('--ids', default=None, help="指定语料ID，逗号分隔")
@click.option('--operator', default="admin", help="操作人")
def desensitize(ids, operator):
    try:
        corpus_ids = [int(x.strip()) for x in ids.split(',')] if ids else None
        version = desensitizer.get_current_version()
        _print_info(f"当前使用规则版本: v{version}")
        count = desensitizer.batch_desensitize(corpus_ids=corpus_ids, operator=operator)
        _print_success(f"完成 {count} 条语料脱敏")
    except Exception as e:
        _print_error(f"脱敏失败: {str(e)}")
        sys.exit(1)


@cli.group(help="规则管理")
def rule():
    pass


@rule.command("list", help="列出脱敏规则")
@click.option('--all', is_flag=True, help="显示所有版本规则")
def rule_list(all):
    data = rules.list_rules(active_only=not all)
    if not data:
        _print_info("没有找到规则")
        return
    table_data = []
    for r in data:
        table_data.append([
            r.id, r.name, r.category,
            Fore.CYAN + r.pattern + Style.RESET_ALL,
            Fore.GREEN + r.replacement + Style.RESET_ALL,
            f"v{r.version}",
            r.description[:30] if r.description else "",
        ])
    headers = ["ID", "名称", "分类", "匹配模式", "替换为", "版本", "描述"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@rule.command("add", help="新增脱敏规则")
@click.option('--name', required=True, help="规则名称")
@click.option('--category', required=True, help="规则分类")
@click.option('--pattern', required=True, help="正则表达式")
@click.option('--replacement', required=True, help="替换文本")
@click.option('--description', default="", help="规则描述")
@click.option('--operator', default="admin", help="操作人")
def rule_add(name, category, pattern, replacement, description, operator):
    try:
        rule_id = rules.add_rule(name, category, pattern, replacement, description, operator)
        _print_success(f"规则新增成功，新规则ID: {rule_id}")
        _print_warning("规则版本已升级，已通过的样本将标记为待复核")
    except Exception as e:
        _print_error(f"新增规则失败: {str(e)}")
        sys.exit(1)


@rule.command("update", help="更新脱敏规则")
@click.argument('rule_id', type=int)
@click.option('--name', help="规则名称")
@click.option('--pattern', help="正则表达式")
@click.option('--replacement', help="替换文本")
@click.option('--description', help="规则描述")
@click.option('--operator', default="admin", help="操作人")
def rule_update(rule_id, name, pattern, replacement, description, operator):
    try:
        new_id = rules.update_rule(
            rule_id, pattern=pattern, replacement=replacement,
            name=name, description=description, operator=operator
        )
        _print_success(f"规则更新成功，新规则ID: {new_id}")
        _print_warning("规则版本已升级，已通过的样本将标记为待复核")
    except Exception as e:
        _print_error(f"更新规则失败: {str(e)}")
        sys.exit(1)


@rule.command("delete", help="删除脱敏规则")
@click.argument('rule_id', type=int)
@click.option('--operator', default="admin", help="操作人")
def rule_delete(rule_id, operator):
    try:
        new_version = rules.delete_rule(rule_id, operator)
        _print_success(f"规则删除成功，规则版本升级到 v{new_version}")
        _print_warning("规则版本已升级，已通过的样本将标记为待复核")
    except Exception as e:
        _print_error(f"删除规则失败: {str(e)}")
        sys.exit(1)


@rule.command("versions", help="列出规则版本历史")
def rule_versions():
    data = rules.list_versions()
    if not data:
        _print_info("没有版本记录")
        return
    table_data = []
    for v in data:
        active = Fore.GREEN + "✓ 当前" + Style.RESET_ALL if v['is_active'] else ""
        table_data.append([
            f"v{v['version']}",
            v['description'],
            v['rule_count'],
            v['created_at'],
            active,
        ])
    headers = ["版本", "描述", "规则数", "创建时间", "状态"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@rule.command("rollback", help="回滚到指定规则版本")
@click.argument('target_version', type=int)
@click.option('--operator', default="admin", help="操作人")
def rule_rollback(target_version, operator):
    try:
        version = rules.rollback_to_version(target_version, operator)
        _print_success(f"已回滚到规则版本 v{version}")
        _print_info("请重新运行 desensitize 命令使用旧版本规则重新脱敏")
    except Exception as e:
        _print_error(f"回滚失败: {str(e)}")
        sys.exit(1)


@cli.group(help="抽检与复核管理")
def review():
    pass


@review.command("sample", help="抽检语料到复核队列")
@click.option('--ratio', type=float, default=0.1, help="抽检比例 (0.01-1.0)")
@click.option('--count', type=int, default=None, help="指定抽检数量")
@click.option('--batch', default=None, help="批次名称")
@click.option('--operator', default="admin", help="操作人")
def review_sample(ratio, count, batch, operator):
    try:
        count, batch_name = sampler.sample_corpus(ratio=ratio, count=count, batch_name=batch, operator=operator)
        if count == 0:
            _print_warning("没有可抽检的语料，请先完成脱敏")
            return
        _print_success(f"抽检完成，批次: {batch_name}，共 {count} 条语料进入复核队列")
    except Exception as e:
        _print_error(f"抽检失败: {str(e)}")
        sys.exit(1)


@review.command("pending", help="列出待复核语料")
@click.option('--batch', default=None, help="按批次过滤")
def review_pending(batch):
    data = sampler.list_pending_reviews(batch_name=batch)
    if not data:
        _print_info("没有待复核的语料")
        return
    table_data = []
    for c in data:
        text = c.desensitized_text[:50] + "..." if len(c.desensitized_text) > 50 else c.desensitized_text
        table_data.append([
            c.id, c.sample_batch, f"v{c.rule_version}",
            Fore.GREEN + text + Style.RESET_ALL,
        ])
    headers = ["ID", "批次", "规则版本", "脱敏内容"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@review.command("submit", help="提交复核意见")
@click.argument('corpus_id', type=int)
@click.option('--reviewer', required=True, help="复核人")
@click.option('--conclusion', required=True, type=click.Choice(['approved', 'rejected']), help="结论")
@click.option('--comment', default="", help="复核意见")
@click.option('--operator', default=None, help="操作人（默认同复核人）")
def review_submit(corpus_id, reviewer, conclusion, comment, operator):
    try:
        result = sampler.submit_review(corpus_id, reviewer, conclusion, comment, operator)
        if result['conflict']:
            _print_warning(f"复核冲突！与复核人 [{result['conflict_with']}] 结论相反，需管理员仲裁")
        elif result['finalized']:
            _print_success(f"复核完成，最终结论: {result['final_conclusion']}")
        else:
            _print_success(f"复核意见已提交，等待第二人复核 (已提交 {result['review_count']}/2)")
    except Exception as e:
        _print_error(f"提交失败: {str(e)}")
        sys.exit(1)


@review.command("resolve", help="管理员仲裁解决冲突")
@click.argument('corpus_id', type=int)
@click.option('--conclusion', required=True, type=click.Choice(['approved', 'rejected']), help="最终结论")
@click.option('--operator', default="admin", help="操作人")
def review_resolve(corpus_id, conclusion, operator):
    try:
        result = sampler.resolve_conflict(corpus_id, conclusion, operator)
        _print_success(f"冲突已解决，语料 {result['corpus_id']} 最终结论: {result['final_conclusion']}")
    except Exception as e:
        _print_error(f"仲裁失败: {str(e)}")
        sys.exit(1)


@review.command("conflicts", help="列出复核冲突")
@click.option('--resolved', is_flag=True, help="显示已解决的冲突")
def review_conflicts(resolved):
    data = sampler.get_conflicts(resolved=resolved)
    if not data:
        _print_info("没有冲突记录")
        return
    table_data = []
    for c in data:
        status = Fore.GREEN + "已解决" + Style.RESET_ALL if c.resolved else Fore.RED + "未解决" + Style.RESET_ALL
        table_data.append([
            c.id, c.corpus_id,
            f"{c.reviewer1}({c.conclusion1})",
            f"{c.reviewer2}({c.conclusion2})",
            status, c.created_at,
        ])
    headers = ["ID", "语料ID", "复核人1", "复核人2", "状态", "创建时间"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@review.command("batches", help="列出抽检批次")
def review_batches():
    data = sampler.list_batches()
    if not data:
        _print_info("没有抽检批次")
        return
    table_data = []
    for b in data:
        status = Fore.GREEN + "已完成" + Style.RESET_ALL if b['pending_count'] == 0 else Fore.YELLOW + f"待复核 {b['pending_count']}" + Style.RESET_ALL
        table_data.append([
            b['batch_name'], b['sample_count'], b['reviewed_count'],
            f"v{b['rule_version']}", b['created_at'], status,
        ])
    headers = ["批次名称", "抽检数", "已复核", "规则版本", "创建时间", "状态"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@cli.command(help="导出脱敏后的语料")
@click.argument('output_path', type=click.Path())
@click.option('--include-original', is_flag=True, help="包含原文（敏感！请谨慎使用）")
@click.option('--operator', default="admin", help="操作人")
def export(output_path, include_original, operator):
    try:
        ready, pending, conflicts = exporter.check_export_ready()
        if not ready:
            _print_error(f"导出条件不满足：")
            if pending > 0:
                _print_warning(f"  - {pending} 条样本待复核")
            if conflicts > 0:
                _print_warning(f"  - {conflicts} 个冲突未解决")
            sys.exit(1)
        count = exporter.export_desensitized(output_path, include_original=include_original, operator=operator)
        _print_success(f"成功导出 {count} 条脱敏语料到 {output_path}")

        if include_original:
            _print_warning("警告：导出文件包含原文敏感数据，请妥善保管！")
        else:
            issues = exporter.check_sensitive_leakage(output_path)
            if issues:
                _print_warning("检测到潜在敏感信息泄露风险：")
                for issue in issues:
                    _print_warning(f"  - {issue}")
            else:
                _print_success("脱敏文件完整性校验通过，未检测到敏感信息泄露")
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)
    except Exception as e:
        _print_error(f"导出失败: {str(e)}")
        sys.exit(1)


@cli.command(help="查看审计日志")
@click.option('--limit', default=50, help="显示数量")
@click.option('--operation', default=None, help="按操作类型过滤")
@click.option('--version', type=int, default=None, help="按规则版本过滤")
def audit_log(limit, operation, version):
    if version:
        data = audit.get_audit_logs_by_version(version)
    else:
        data = audit.get_audit_logs(limit=limit, operation=operation)
    if not data:
        _print_info("没有审计日志")
        return
    table_data = []
    for log in data:
        op_color = {
            'import': Fore.CYAN,
            'desensitize': Fore.GREEN,
            'export': Fore.MAGENTA,
            'rule_add': Fore.YELLOW,
            'rule_update': Fore.YELLOW,
            'rule_delete': Fore.YELLOW,
            'rollback': Fore.RED,
            'sample': Fore.BLUE,
            'conflict': Fore.RED,
            'conflict_resolve': Fore.GREEN,
            'review_finalize': Fore.GREEN,
            'status_change': Fore.YELLOW,
        }.get(log.operation, Fore.WHITE)
        table_data.append([
            log.id,
            op_color + log.operation + Style.RESET_ALL,
            log.operator,
            f"v{log.rule_version}" if log.rule_version > 0 else "-",
            log.details[:60] + "..." if len(log.details) > 60 else log.details,
            log.created_at,
        ])
    headers = ["ID", "操作", "操作人", "规则版本", "详情", "时间"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@cli.command(help="系统状态概览")
def status():
    conn = importer.get_connection() if hasattr(importer, 'get_connection') else None
    from .database import get_connection
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM corpus")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT status, COUNT(*) FROM corpus GROUP BY status")
    status_counts = dict(cursor.fetchall())

    cursor.execute("SELECT COUNT(*) FROM corpus WHERE is_sampled = 1")
    sampled = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM corpus WHERE is_sampled = 1 AND final_conclusion IS NULL")
    pending = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM conflict_records WHERE resolved = 0")
    conflicts = cursor.fetchone()[0]

    cursor.execute("SELECT version FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1")
    version_row = cursor.fetchone()
    current_version = version_row[0] if version_row else 1

    cursor.execute("SELECT COUNT(*) FROM desensitization_rules WHERE is_active = 1")
    rule_count = cursor.fetchone()[0]

    conn.close()

    click.echo(f"\n{Fore.CYAN}===== 系统状态概览 ====={Style.RESET_ALL}")
    click.echo(f"数据库路径:     {DB_PATH}")
    click.echo(f"\n{Fore.YELLOW}--- 规则状态 ---{Style.RESET_ALL}")
    click.echo(f"当前规则版本:   v{current_version}")
    click.echo(f"活跃规则数:     {rule_count}")

    click.echo(f"\n{Fore.YELLOW}--- 语料状态 ---{Style.RESET_ALL}")
    click.echo(f"总语料数:       {total}")
    click.echo(f"  已导入:       {status_counts.get('imported', 0)}")
    click.echo(f"  已脱敏:       {status_counts.get('desensitized', 0)}")
    click.echo(f"  待复核:       {status_counts.get('pending_review', 0)}")
    click.echo(f"  需重审:       {status_counts.get('needs_review', 0)}")
    click.echo(f"  已复核:       {status_counts.get('reviewed', 0)}")

    click.echo(f"\n{Fore.YELLOW}--- 抽检复核 ---{Style.RESET_ALL}")
    click.echo(f"已抽检:         {sampled}")
    click.echo(f"待复核:         {pending}")
    click.echo(f"未解决冲突:     {conflicts}")

    if pending == 0 and conflicts == 0 and total > 0 and status_counts.get('desensitized', 0) > 0:
        _print_success("系统状态良好，可以执行导出")
    elif conflicts > 0:
        _print_warning(f"存在 {conflicts} 个未解决的冲突，需要管理员仲裁后才能导出")
    elif pending > 0:
        _print_warning(f"还有 {pending} 条样本待复核，完成后才能导出")


if __name__ == '__main__':
    cli()
