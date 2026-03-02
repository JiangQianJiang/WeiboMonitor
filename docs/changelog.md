# Changelog

## 2026-03-02

- **docs**: 更新 README.md 反映重构后的项目架构

### Phase 1：P0 基础架构改造

- **1.1 配置与状态分离**：新建 `core/config.py` 和 `state/store.py`，`latest_id` 从 `users.yaml` 分离到 `state.yaml`，路径基于 `__file__` 解析
- **1.2 Cookie 移入配置文件**：Cookie 从 `weibo.py` 硬编码移到 `users.yaml` 配置
- **1.3 AsyncIOScheduler 改造**：新建 `core/app.py`，`BlockingScheduler` → `AsyncIOScheduler`，单一事件循环贯穿全生命周期
- **1.4 HTTP Session 统一管理**：App 统一创建 `ClientSession` 并注入 `WeiboMonitor` 和 `Notifer`，移除各模块自建 session
