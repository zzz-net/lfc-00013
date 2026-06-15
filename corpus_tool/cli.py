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
from . import export_config as ec
from . import snapshot as snap

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


@cli.command(help="导出脱敏后的语料（支持 CSV/JSONL 和字段策略配置）")
@click.argument('output_path', type=click.Path())
@click.option('--include-original', is_flag=True, help="包含原文（敏感！请谨慎使用，兼容旧参数）")
@click.option('--format', 'fmt', default=None, type=click.Choice(['csv', 'jsonl']),
              help="指定导出格式（默认按已保存配置，其次按扩展名）")
@click.option('--include-summary', is_flag=True, help="包含复核摘要字段")
@click.option('--use-config', is_flag=True, help="使用数据库中已保存的导出配置（默认使用当前激活方案）")
@click.option('--config-name', default=None, help="配置名称（不指定则使用当前激活方案）")
@click.option('--operator', default="admin", help="操作人")
def export(output_path, include_original, fmt, include_summary, use_config, config_name, operator):
    try:
        if config_name is None:
            config_name = ec.get_active_config_name() or "default"

        ready, pending, conflicts = exporter.check_export_ready()
        if not ready:
            _print_error(f"导出条件不满足：")
            if pending > 0:
                _print_warning(f"  - {pending} 条样本待复核")
            if conflicts > 0:
                _print_warning(f"  - {conflicts} 个冲突未解决")
            sys.exit(1)

        override_cfg = None
        if not use_config:
            cfg, warnings = ec.load_config(config_name)
            for w in warnings:
                _print_warning(w)
            override_cfg = cfg
            if include_original:
                override_cfg.field_policies["original_text"] = ec.FieldPolicy.KEEP.value
            if fmt:
                override_cfg.format = fmt
            if include_summary:
                override_cfg.include_review_summary = True
                override_cfg.field_policies["review_summary"] = ec.FieldPolicy.KEEP.value
            valid, errs = override_cfg.validate()
            fatal = [e for e in errs if "安全提示" not in e]
            safety = [e for e in errs if "安全提示" in e]
            for s in safety:
                _print_warning(s)
            if fatal:
                for e in fatal:
                    _print_error(e)
                sys.exit(1)
            count = exporter.export_desensitized(
                output_path, operator=operator, config=override_cfg
            )
        else:
            count = exporter.export_desensitized(
                output_path, operator=operator,
                use_saved_config=True, config_name=config_name
            )

        fields_hint = ""
        if override_cfg:
            ef = override_cfg.get_effective_fields()
            fields_hint = f"，字段数={len(ef)}"
        _print_success(f"成功导出 {count} 条脱敏语料到 {output_path}{fields_hint}")

        exporting_original = (
            (override_cfg and override_cfg.field_policies.get("original_text") == ec.FieldPolicy.KEEP.value)
            or include_original
        )
        if exporting_original:
            _print_warning("警告：导出文件包含原文敏感数据，请妥善保管！")
        else:
            issues = exporter.check_sensitive_leakage(output_path)
            if issues:
                _print_warning("检测到潜在敏感信息泄露风险：")
                for issue in issues:
                    _print_warning(f"  - {issue}")
            else:
                _print_success("脱敏文件完整性校验通过，未检测到敏感信息泄露")

        stats = exporter.build_review_stats()
        _print_info(
            f"复核统计：总语料 {stats['total_corpus']} 条，"
            f"通过 {stats['by_final_conclusion'].get('approved', 0)}，"
            f"拒绝 {stats['by_final_conclusion'].get('rejected', 0)}，"
            f"未解决冲突 {stats['unresolved_conflicts']}"
        )
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)
    except Exception as e:
        _print_error(f"导出失败: {str(e)}")
        sys.exit(1)


@cli.group(help="导出配置管理（字段策略/格式/复核摘要）")
def export_config():
    pass


@export_config.command("show", help="查看导出配置（默认查看当前激活方案）")
@click.option('--config-name', default=None, help="配置名称（不指定则查看当前激活方案）")
def export_config_show(config_name):
    active_name = ec.get_active_config_name()
    if config_name is None:
        config_name = active_name or "default"

    cfg, warnings = ec.load_config(config_name)
    for w in warnings:
        _print_warning(w)
    if cfg is None:
        _print_error("无法加载配置")
        sys.exit(1)

    is_active = config_name == active_name
    active_tag = Fore.GREEN + " [当前激活]" + Style.RESET_ALL if is_active else ""
    click.echo(f"\n{Fore.CYAN}配置方案: {Style.RESET_ALL}{config_name}{active_tag}")
    click.echo(cfg.summary_text())
    click.echo("\n" + Fore.CYAN + "完整 JSON:" + Style.RESET_ALL)
    click.echo(cfg.to_json())


@export_config.command("list", help="列出所有导出配置方案")
def export_config_list():
    configs = ec.list_configs()
    if not configs:
        _print_info("没有找到任何配置方案")
        return

    table_data = []
    for cfg in configs:
        active_mark = Fore.GREEN + "★" + Style.RESET_ALL if cfg["is_active"] else ""
        fmt = cfg["format"].upper()
        table_data.append([
            active_mark,
            cfg["name"],
            fmt,
            cfg["field_count"],
            cfg.get("updated_at", "-") or "-",
        ])
    headers = ["", "方案名称", "格式", "保留字段数", "更新时间"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))
    click.echo(f"\n共 {len(configs)} 个配置方案，★ 表示当前激活方案")


@export_config.command("use", help="切换激活的导出配置方案")
@click.argument('config_name')
@click.option('--operator', default="admin", help="操作人")
def export_config_use(config_name, operator):
    ok, errs = ec.set_active_config(config_name, operator)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已切换到配置方案 [{config_name}]")


@export_config.command("copy", help="复制配置方案（基于已有方案新建）")
@click.argument('source_name')
@click.argument('target_name')
@click.option('--operator', default="admin", help="操作人")
def export_config_copy(source_name, target_name, operator):
    ok, errs, warns = ec.copy_config(source_name, target_name, operator)
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已复制配置：[{source_name}] -> [{target_name}]")


@export_config.command("rename", help="重命名配置方案")
@click.argument('old_name')
@click.argument('new_name')
@click.option('--operator', default="admin", help="操作人")
def export_config_rename(old_name, new_name, operator):
    ok, errs = ec.rename_config(old_name, new_name, operator)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已重命名配置：[{old_name}] -> [{new_name}]")


@export_config.command("delete", help="删除配置方案（不能删除 default）")
@click.argument('config_name')
@click.option('--operator', default="admin", help="操作人")
@click.option('--yes', is_flag=True, help="跳过确认直接删除")
def export_config_delete(config_name, operator, yes):
    if not yes:
        click.echo(f"{Fore.YELLOW}警告：即将删除配置方案 [{config_name}]，此操作不可撤销！{Style.RESET_ALL}")
        confirm = click.prompt("请输入配置名称确认删除", default="")
        if confirm != config_name:
            _print_error("名称不匹配，已取消删除")
            sys.exit(1)

    ok, errs = ec.delete_config(config_name, operator)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已删除配置方案 [{config_name}]")


@export_config.command("fields", help="列出所有可配置字段及其当前策略")
@click.option('--config-name', default="default", help="配置名称")
def export_config_fields(config_name):
    cfg, _ = ec.load_config(config_name)
    table_data = []
    for f in ec.ALL_EXPORTABLE_FIELDS:
        policy = cfg.field_policies.get(f, "-")
        required = "是" if f in ec.REQUIRED_FIELDS else ""
        desc = {
            "id": "语料ID",
            "original_text": "原始文本（含敏感信息）",
            "desensitized_text": "脱敏后的文本",
            "source_file": "来源文件名",
            "status": "语料状态",
            "rule_version": "脱敏使用的规则版本",
            "created_at": "创建时间",
            "updated_at": "更新时间",
            "is_sampled": "是否进入抽检",
            "sample_batch": "抽检批次",
            "final_conclusion": "复核最终结论",
            "review_summary": "复核摘要（复核人/结论/冲突信息）",
        }.get(f, "")
        color = Fore.GREEN if policy == ec.FieldPolicy.KEEP.value else Fore.RED
        table_data.append([
            f, color + policy + Style.RESET_ALL, required, desc,
        ])
    click.echo(tabulate(table_data, headers=["字段名", "策略", "必填", "说明"], tablefmt="simple"))


@export_config.command("keep", help="将指定字段标记为保留（可多个，逗号分隔）")
@click.argument('fields')
@click.option('--config-name', default="default", help="配置名称")
@click.option('--operator', default="admin", help="操作人")
def export_config_keep(fields, config_name, operator):
    cfg, _ = ec.load_config(config_name)
    flist = [x.strip() for x in fields.split(",")]
    unknown = [f for f in flist if f not in ec.ALL_EXPORTABLE_FIELDS]
    if unknown:
        _print_error(f"未知字段: {', '.join(unknown)}")
        sys.exit(1)
    for f in flist:
        cfg.field_policies[f] = ec.FieldPolicy.KEEP.value
    ok, errs, warns = ec.save_config(cfg, config_name, operator)
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已将 {flist} 标记为保留并保存")


@export_config.command("drop", help="将指定字段标记为删除（可多个，逗号分隔）")
@click.argument('fields')
@click.option('--config-name', default="default", help="配置名称")
@click.option('--operator', default="admin", help="操作人")
def export_config_drop(fields, config_name, operator):
    cfg, _ = ec.load_config(config_name)
    flist = [x.strip() for x in fields.split(",")]
    unknown = [f for f in flist if f not in ec.ALL_EXPORTABLE_FIELDS]
    if unknown:
        _print_error(f"未知字段: {', '.join(unknown)}")
        sys.exit(1)
    for f in flist:
        if f in ec.REQUIRED_FIELDS:
            _print_warning(f"字段 [{f}] 为必填，不能删除，已忽略")
            continue
        cfg.field_policies[f] = ec.FieldPolicy.DROP.value
    ok, errs, warns = ec.save_config(cfg, config_name, operator)
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已将 {[f for f in flist if f not in ec.REQUIRED_FIELDS]} 标记为删除并保存")


@export_config.command("format", help="设置导出格式")
@click.argument('fmt', type=click.Choice(['csv', 'jsonl']))
@click.option('--config-name', default="default", help="配置名称")
@click.option('--operator', default="admin", help="操作人")
def export_config_format(fmt, config_name, operator):
    cfg, _ = ec.load_config(config_name)
    cfg.format = fmt
    ok, errs, warns = ec.save_config(cfg, config_name, operator)
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"导出格式已设为 {fmt.upper()} 并保存")


@export_config.command("summary", help="开启/关闭复核摘要")
@click.argument('switch', type=click.Choice(['on', 'off']))
@click.option('--config-name', default="default", help="配置名称")
@click.option('--operator', default="admin", help="操作人")
def export_config_summary(switch, config_name, operator):
    cfg, _ = ec.load_config(config_name)
    enabled = switch == "on"
    cfg.include_review_summary = enabled
    cfg.field_policies["review_summary"] = (
        ec.FieldPolicy.KEEP.value if enabled else ec.FieldPolicy.DROP.value
    )
    ok, errs, warns = ec.save_config(cfg, config_name, operator)
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"复核摘要已{'开启' if enabled else '关闭'}并保存")


@export_config.command("save", help="按当前设置保存完整配置（带交互式 JSON 覆盖能力）")
@click.option('--config-name', default="default", help="配置名称")
@click.option('--json', 'json_in', default=None, type=click.Path(exists=True),
              help="从 JSON 文件读取完整配置覆盖保存")
@click.option('--operator', default="admin", help="操作人")
def export_config_save(config_name, json_in, operator):
    if json_in:
        ok, errs, warns = ec.import_config_from_file(json_in, config_name, operator)
        for w in warns:
            _print_warning(w)
        if not ok:
            for e in errs:
                _print_error(e)
            sys.exit(1)
        _print_success(f"已从 {json_in} 导入并保存配置")
        return
    cfg, _ = ec.load_config(config_name)
    ok, errs, warns = ec.save_config(cfg, config_name, operator)
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success("当前配置已保存")


@export_config.command("reset", help="重置为默认配置（CSV 格式，保留核心字段）")
@click.option('--config-name', default="default", help="配置名称")
@click.option('--operator', default="admin", help="操作人")
def export_config_reset(config_name, operator):
    ok, msgs = ec.reset_config(config_name, operator)
    for m in msgs:
        _print_warning(m)
    if not ok:
        _print_error("重置配置失败")
        sys.exit(1)
    _print_success("已重置为默认导出配置")


@export_config.command("to-file", help="将当前配置导出为 JSON 文件")
@click.argument('file_path', type=click.Path())
@click.option('--config-name', default="default", help="配置名称")
@click.option('--operator', default="admin", help="操作人")
def export_config_to_file(file_path, config_name, operator):
    ok, errs = ec.export_config_to_file(file_path, config_name, operator)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"配置已导出到 {file_path}")


@export_config.command("from-file", help="从 JSON 文件导入配置（含旧版兼容迁移）")
@click.argument('file_path', type=click.Path(exists=True))
@click.option('--config-name', default="default", help="目标配置名称")
@click.option('--as-new', is_flag=True, help="作为新方案导入（目标名称必须不存在）")
@click.option('--overwrite', is_flag=True, help="允许覆盖已存在的配置方案")
@click.option('--operator', default="admin", help="操作人")
def export_config_from_file(file_path, config_name, as_new, overwrite, operator):
    if as_new and overwrite:
        _print_error("--as-new 和 --overwrite 不能同时使用")
        sys.exit(1)

    if as_new:
        overwrite_val = False
    elif overwrite:
        overwrite_val = True
    else:
        overwrite_val = (config_name == "default")

    ok, errs, warns = ec.import_config_from_file(
        file_path, config_name, operator, overwrite=overwrite_val
    )
    for w in warns:
        _print_warning(w)
    if not ok:
        for e in errs:
            _print_error(e)
        sys.exit(1)
    _print_success(f"已从 {file_path} 导入配置到方案 [{config_name}]")


@cli.group(help="数据集快照与回滚管理")
def snapshot():
    pass


@snapshot.command("create", help="创建命名快照，保存当前语料状态")
@click.option('--name', required=True, help="快照名称")
@click.option('--description', default="", help="快照描述")
@click.option('--operator', default="admin", help="操作人")
def snapshot_create(name, description, operator):
    try:
        s = snap.create_snapshot(name, description=description, operator=operator)
        _print_success(
            f"快照创建成功: [{s.name}] "
            f"(语料 {s.corpus_count} 条, "
            f"复核 {s.review_count} 条, "
            f"冲突 {s.conflict_count} 条, "
            f"规则 v{s.rule_version})"
        )
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


@snapshot.command("list", help="列出所有快照")
def snapshot_list():
    data = snap.list_snapshots()
    if not data:
        _print_info("没有找到任何快照")
        return
    table_data = []
    for s in data:
        table_data.append([
            s.id, s.name,
            f"v{s.rule_version}",
            s.export_config_name or "-",
            s.corpus_count,
            s.review_count,
            s.conflict_count,
            s.created_by,
            s.created_at,
        ])
    headers = ["ID", "名称", "规则版本", "导出配置", "语料数", "复核数", "冲突数", "创建人", "创建时间"]
    click.echo(tabulate(table_data, headers=headers, tablefmt="simple"))


@snapshot.command("show", help="查看快照详情")
@click.argument('name_or_id')
def snapshot_show(name_or_id):
    try:
        if name_or_id.isdigit():
            s = snap.get_snapshot(int(name_or_id))
        else:
            s = snap.get_snapshot_by_name(name_or_id)
        click.echo(f"\n{Fore.CYAN}=== 快照详情: {s.name} ==={Style.RESET_ALL}")
        click.echo(f"ID:           {s.id}")
        click.echo(f"描述:         {s.description or '-'}")
        click.echo(f"规则版本:     v{s.rule_version}")
        click.echo(f"导出配置:     {s.export_config_name or '-'}")
        click.echo(f"语料数:       {s.corpus_count}")
        click.echo(f"复核记录数:   {s.review_count}")
        click.echo(f"冲突记录数:   {s.conflict_count}")
        click.echo(f"创建人:       {s.created_by}")
        click.echo(f"创建时间:     {s.created_at}")
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


@snapshot.command("export", help="导出快照到 JSON 文件")
@click.argument('name_or_id')
@click.argument('output_path', type=click.Path())
@click.option('--operator', default="admin", help="操作人")
def snapshot_export(name_or_id, output_path, operator):
    try:
        if name_or_id.isdigit():
            s = snap.get_snapshot(int(name_or_id))
        else:
            s = snap.get_snapshot_by_name(name_or_id)
        snap.export_snapshot(s.id, output_path, operator=operator)
        _print_success(f"快照 [{s.name}] 已导出到 {output_path}")
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


@snapshot.command("import", help="从 JSON 文件导入快照")
@click.argument('file_path', type=click.Path(exists=True))
@click.option('--rename', default=None, help="重命名导入后的快照")
@click.option('--overwrite', is_flag=True, help="允许覆盖已存在的同名快照")
@click.option('--operator', default="admin", help="操作人")
def snapshot_import(file_path, rename, overwrite, operator):
    try:
        s = snap.import_snapshot(
            file_path, operator=operator,
            overwrite=overwrite, rename_to=rename
        )
        _print_success(
            f"快照导入成功: [{s.name}] "
            f"(语料 {s.corpus_count} 条)"
        )
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


@snapshot.command("delete", help="删除快照")
@click.argument('name_or_id')
@click.option('--operator', default="admin", help="操作人")
@click.option('--yes', is_flag=True, help="跳过确认直接删除")
def snapshot_delete(name_or_id, operator, yes):
    try:
        if name_or_id.isdigit():
            s = snap.get_snapshot(int(name_or_id))
        else:
            s = snap.get_snapshot_by_name(name_or_id)

        if not yes:
            click.echo(f"{Fore.YELLOW}警告：即将删除快照 [{s.name}]，此操作不可撤销！{Style.RESET_ALL}")
            confirm = click.prompt("请输入快照名称确认删除", default="")
            if confirm != s.name:
                _print_error("名称不匹配，已取消删除")
                sys.exit(1)

        snap.delete_snapshot(s.id, operator=operator)
        _print_success(f"快照 [{s.name}] 已删除")
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


@snapshot.command("preview", help="预览回滚影响")
@click.argument('name_or_id')
def snapshot_preview(name_or_id):
    try:
        if name_or_id.isdigit():
            s = snap.get_snapshot(int(name_or_id))
        else:
            s = snap.get_snapshot_by_name(name_or_id)

        preview = snap.preview_rollback(s.id)

        click.echo(f"\n{Fore.CYAN}=== 回滚预览: {preview['snapshot_name']} ==={Style.RESET_ALL}")
        click.echo(f"快照规则版本:   v{preview['snapshot_rule_version']}")
        click.echo(f"当前规则版本:   v{preview['current_rule_version']}")
        click.echo(f"快照导出配置:   {preview['snapshot_export_config'] or '-'}")
        click.echo(f"当前导出配置:   {preview['current_export_config'] or '-'}")

        click.echo(f"\n{Fore.YELLOW}--- 数据变化 ---{Style.RESET_ALL}")

        def fmt_delta(d):
            if d > 0:
                return Fore.GREEN + f"+{d}" + Style.RESET_ALL
            elif d < 0:
                return Fore.RED + str(d) + Style.RESET_ALL
            else:
                return "0"

        click.echo(
            f"语料:     当前 {preview['corpus']['current']} 条 "
            f"→ 快照 {preview['corpus']['snapshot']} 条 "
            f"({fmt_delta(preview['corpus']['delta'])})"
        )
        click.echo(
            f"复核记录: 当前 {preview['review_records']['current']} 条 "
            f"→ 快照 {preview['review_records']['snapshot']} 条 "
            f"({fmt_delta(preview['review_records']['delta'])})"
        )
        click.echo(
            f"冲突记录: 当前 {preview['conflict_records']['current']} 条 "
            f"→ 快照 {preview['conflict_records']['snapshot']} 条 "
            f"({fmt_delta(preview['conflict_records']['delta'])})"
        )

        if preview["warnings"]:
            click.echo(f"\n{Fore.RED}--- 警告 ---{Style.RESET_ALL}")
            for w in preview["warnings"]:
                _print_warning(w)

        click.echo()
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


@snapshot.command("rollback", help="回滚到指定快照")
@click.argument('name_or_id')
@click.option('--operator', default="admin", help="操作人")
@click.option('--yes', is_flag=True, help="跳过确认直接回滚")
def snapshot_rollback(name_or_id, operator, yes):
    try:
        if name_or_id.isdigit():
            s = snap.get_snapshot(int(name_or_id))
        else:
            s = snap.get_snapshot_by_name(name_or_id)

        preview = snap.preview_rollback(s.id)

        if not yes:
            click.echo(f"\n{Fore.YELLOW}警告：即将回滚到快照 [{s.name}]，此操作将覆盖当前所有语料数据！{Style.RESET_ALL}")
            click.echo(f"  语料:     {preview['corpus']['current']} → {preview['corpus']['snapshot']} 条")
            click.echo(f"  复核记录: {preview['review_records']['current']} → {preview['review_records']['snapshot']} 条")
            click.echo(f"  冲突记录: {preview['conflict_records']['current']} → {preview['conflict_records']['snapshot']} 条")

            if preview["warnings"]:
                click.echo(f"\n{Fore.RED}警告信息：{Style.RESET_ALL}")
                for w in preview["warnings"]:
                    click.echo(f"  - {w}")

            click.echo()
            confirm = click.prompt("请输入快照名称确认回滚", default="")
            if confirm != s.name:
                _print_error("名称不匹配，已取消回滚")
                sys.exit(1)

        result = snap.rollback_snapshot(s.id, operator=operator)
        _print_success(
            f"回滚成功: [{result['snapshot_name']}] "
            f"(恢复语料 {result['corpus_restored']} 条, "
            f"复核记录 {result['review_records_restored']} 条, "
            f"冲突记录 {result['conflict_records_restored']} 条)"
        )
    except ValueError as e:
        _print_error(str(e))
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
