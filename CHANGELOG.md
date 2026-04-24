# Changelog

## 2026-04-24

### 重构

- **refactor(state)**: 从 YAML 文件持久化迁移到 SQLite 数据库
  - 新建 `state/repository.py`，基于 `aiosqlite` 实现异步 SQLite 访问
  - 新建 `state/schema.sql`，定义 `account_state`、`weibo_history`、`push_log` 三张表
  - 新增 `state/migration.py` 迁移脚本，支持从 `state.yaml` 迁移到 SQLite 及回滚
  - `state/store.py` API 全面异步化，内部通过 Repository 实现零查询缓存
  - 外键约束确保数据完整性，唯一索引防止重复微博

### 改进

- **refactor(notifer)**: 推送结果改为按渠道返回 `{channel: (success, error)}` 字典
  - 每个渠道独立捕获异常，不再因单渠道失败中断其他渠道
  - `app.py` 中基于各渠道结果分别记录推送日志
- **perf(notifer)**: 恢复并发推送，使用 `asyncio.gather(return_exceptions=True)`
  - Telegram 和 Server酱同时推送，推送延迟从 `t1 + t2` 降为 `max(t1, t2)`
  - 保留按渠道独立捕获异常的能力

### 修复

- **fix(notifer)**: 修复 Telegram MarkdownV2 双重转义问题
  - 删除 `telegram_send()` 中的 `escape_markdown_v2()` 调用，避免二次转义
  - 用户数据字段在 `app.py` 模板替换前转义，保留模板标记字面量
  - 修复后 Telegram 消息格式化正常显示
- **fix(state)**: 添加迁移前置检查 —— 检测运行中的应用进程，防止迁移时数据竞争
- **fix(migration)**: `_ensure_backup_exists()` 显式返回 `False` 而非隐式 `None`
- **style(app)**: 将 `escape_markdown_v2` 导入移至文件顶部，避免条件导入
- **style(migration)**: 删除未使用的 `os` 导入

### 测试

- 新增 34 个测试用例覆盖：Schema 验证、状态更新时序、按渠道日志、并发操作、迁移脚本、App._check_single() 回归测试
- 新增 5 个测试用例验证修复：单次转义、并发推送、部分失败处理

## 2026-04-23

### 改进

- **refactor(notifer)**: 优化 Server酱推送逻辑，支持新旧版 sendkey 格式
  - 自动识别 sendkey 格式（`sctp{num}t...` 或旧版格式）
  - 新版格式自动从 sendkey 中提取 num 构造 URL
  - 移除配置文件中的 `num` 字段，简化配置
  - 兼容官方推荐的推送方式

### 修复

- **fix(docs)**: 在 users.yaml.example 中补充推送开关字段说明
  - 添加 `enable_telegram` 和 `enable_serverchan` 字段到示例配置
  - 说明默认值为 `true`，保持向后兼容
  - 修复开关配置缺失导致无法生效的问题

- **fix(docs)**: 修复 README 中的运行命令
  - 将 `python core/main.py` 改为 `python -m core.main`
  - 解决直接运行时的 `ModuleNotFoundError: No module named 'core'` 错误
  - 使用 `-m` 参数确保 Python 正确解析模块路径

### 新增

- **feat(notifer)**: 添加 Telegram 和 Server酱通知开关
  - 在 `users.yaml` 中新增 `enable_telegram` 和 `enable_serverchan` 配置项
  - 修改 `Notifer.send_message()` 方法，根据开关决定是否推送
  - 默认两个开关均为 `true`，保持向后兼容
  - 当所有渠道禁用时记录警告日志

### 修复

- **fix(core)**: 修复阿里云函数入口导入错误
  - 将 `core/index.py` 中的 `from main import run` 改为 `from core.app import App`
  - 使用 `asyncio.run(App().run_once())` 正确调用异步函数
  - 修复后可正常部署到阿里云函数计算

- **fix(core)**: 修复通知失败导致告警永久丢失的问题
  - 将状态更新（`set_latest_id` + `save_state`）移到通知成功之后
  - 确保推送失败时状态不会前移，下次轮询会重试该微博
  - 避免网络故障或 API 限流导致的告警丢失

- **fix(notifer)**: 添加 Telegram MarkdownV2 特殊字符转义
  - 新增 `escape_markdown_v2()` 函数转义 `_*[]()~` 等特殊字符
  - 防止微博内容中的特殊字符导致 Telegram API 400 错误
  - 提高推送成功率

### 改进

- **chore**: 更新 `.gitignore`
  - 添加 Python 编译文件忽略规则（`__pycache__/`, `*.pyc` 等）
  - 添加临时目录忽略规则（`.humanize/`, `codex-loguru-check/`）

## 2026-03-02

- **docs**: 更新 README.md 反映重构后的项目架构

### Phase 1：P0 基础架构改造

- **1.1 配置与状态分离**：新建 `core/config.py` 和 `state/store.py`，`latest_id` 从 `users.yaml` 分离到 `state.yaml`，路径基于 `__file__` 解析
- **1.2 Cookie 移入配置文件**：Cookie 从 `weibo.py` 硬编码移到 `users.yaml` 配置
- **1.3 AsyncIOScheduler 改造**：新建 `core/app.py`，`BlockingScheduler` → `AsyncIOScheduler`，单一事件循环贯穿全生命周期
- **1.4 HTTP Session 统一管理**：App 统一创建 `ClientSession` 并注入 `WeiboMonitor` 和 `Notifer`，移除各模块自建 session
