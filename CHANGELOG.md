# Changelog

## 2026-04-23

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
