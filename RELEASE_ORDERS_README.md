# 导出配置发布单（Release Order）模块说明

## 模块功能

发布单用于管理导出配置的变更流程，支持草稿 → 锁定 → 审批 → 发布 → 撤销的完整生命周期，
以及导入/导出 JSON 实现跨环境迁移。

## 导入链路（核心重构说明）

导入链路采用统一的三步架构，确保命令层、服务层、测试层复用同一套逻辑：

```
_prepare_import_context   →   _check_import_conflicts   →   _persist_imported_order
  (解析文件 / 合法性校验)        (三类冲突统一校验)           (落库 + 历史 + 审计)
```

### 1. 解析阶段（_prepare_import_context）
- 文件存在性与编码检查
- JSON 解析与 schema 版本兼容校验
- 发布单名称、源/目标配置名称合法性
- 配置数据解析与字段策略合法性

### 2. 冲突校验阶段（_check_import_conflicts）
导入时若命中以下任一冲突，**会被直接拦下，不会落成任何 draft 数据**：

| 冲突类型 | 拦截策略 | 可强制绕过 |
|---------|---------|-----------|
| 同名发布单已存在 | 拒绝 | ✅ 可通过 `--force` 覆盖 |
| 目标配置已存在 | 拒绝 | ❌ 不可强制，需更换目标配置名称 |
| 激活配置被他人改动（漂移） | 拒绝 | ❌ 不可强制，需先回滚或确认变更 |
| 发布单规则版本落后于当前 | 拒绝 | ❌ 不可强制，需在源环境重新导出 |

### 3. 落库阶段（_persist_imported_order）
- 写入 `release_orders` 表（状态始终为 `draft`）
- 写入 `release_order_history` 历史记录
- 写入 `audit_logs` 审计日志（成功时记录 `release_order_import`，失败时记录 `release_order_import_failed`）

## 与创建发布单的区别

- **创建（create_release_order）**：仅校验发布单名称和源/目标配置合法性，
  不执行三类冲突拦截。冲突留到发布（publish）阶段统一处理。
- **导入（import_release_order）**：在落库前即执行完整的三类冲突拦截，
  被拦截时不会产生任何 draft 数据。

## CLI 用法

```bash
# 导入发布单
python main.py release-order import <file.json>
python main.py release-order import <file.json> --rename NEW-NAME
python main.py release-order import <file.json> --force    # 仅对"同名发布单"冲突生效

# 查看导入命令详细帮助
python main.py release-order import --help
```

## 导入被拒绝后的状态

当导入被冲突拦截时：
- 数据库中**不会**新增 draft 状态的发布单记录
- `release_order_history` 表中**不会**写入历史
- 审计日志中会写入一条 `release_order_import_failed` 记录，详情包含冲突原因
- 命令以非零退出码返回，错误信息列出所有冲突

## 测试夹具（可复用）

`test_release_orders.py` 中提供以下可复用测试夹具：

| 夹具函数 | 用途 |
|---------|-----|
| `setup_test_env()` | 重置 DB + 初始化 + 准备 prod_config / test_config 命名配置 |
| `build_export_file()` | 创建发布单 → 可选修改配置 → 导出为 JSON 文件 |
| `assert_import_blocked()` | 断言导入被某类冲突拦截且不落库 draft |
| `assert_audit_has_op()` | 断言审计日志中存在指定操作（可选详情关键词） |

覆盖的回归场景：
1. 同名发布单冲突（force=True 可覆盖）
2. 目标配置已存在
3. 激活配置漂移（被他人改动）
4. 规则版本落后
5. 重启后再次导入
6. 审计日志记录完整性
