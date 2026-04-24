# SQLite 数据持久化设计方案

## 概述

将 WeiboMonitor 的状态管理从 YAML 文件迁移到 SQLite 数据库，在保持现有功能的基础上，增加微博历史记录和推送日志功能。

## 设计目标

1. **向后兼容**：保持现有 API 接口不变（`load_state()`, `save_state()`, `get_latest_id()`, `set_latest_id()`）
2. **高性能**：使用内存缓存，读取操作零数据库查询
3. **数据扩展**：支持微博历史记录和推送日志的永久保存
4. **安全迁移**：提供手动迁移脚本，支持数据备份和回滚

## 核心决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 数据保留策略 | 永久保留所有数据 | 支持长期分析和追溯 |
| 向后兼容性 | 保持现有 API 不变 | 最小化代码改动，降低风险 |
| 性能优化 | 内存缓存 + 异步写入 | 零读取延迟，不阻塞主流程 |
| 推送日志粒度 | 分渠道记录 | 便于分析每个渠道的可靠性 |
| 迁移策略 | 手动迁移脚本 | 用户可控，更安全 |
| 健康检查 | 暂不实现 | 专注核心功能，后续扩展 |
| 数据库位置 | 项目根目录 | 与 `state.yaml` 同级，便于管理 |
| 查询功能 | 历史微博 + 推送统计 | 支持后续分析和排查 |
| 异步库选择 | aiosqlite | 官方推荐，轻量级，无 C 依赖 |

## 数据库设计

### 表结构

#### 1. account_state（核心表）

替代 `state.yaml`，存储每个账号的最新状态。

```sql
CREATE TABLE account_state (
    weiboid TEXT PRIMARY KEY,           -- 微博用户ID
    latest_id TEXT NOT NULL,            -- 最新微博ID（用于去重）
    screen_name TEXT,                   -- 用户昵称
    last_check_time TIMESTAMP,          -- 最后检查时间
    last_update_time TIMESTAMP,         -- 最后更新时间（检测到新微博）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**字段说明：**
- `weiboid`：主键，对应 `users.yaml` 中配置的账号ID
- `latest_id`：最新微博ID，用于去重检测（核心字段）
- `screen_name`：用户昵称，便于日志和查询
- `last_check_time`：最后检查时间，用于监控
- `last_update_time`：最后检测到新微博的时间

#### 2. weibo_history（历史记录表）

永久保存所有微博的完整内容。

```sql
CREATE TABLE weibo_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weiboid TEXT NOT NULL,              -- 微博用户ID
    weibo_id TEXT NOT NULL,             -- 微博ID
    text TEXT NOT NULL,                 -- 微博原文
    screen_name TEXT NOT NULL,          -- 用户昵称
    source TEXT,                        -- 发布来源（如"iPhone客户端"）
    region_name TEXT,                   -- 地区信息
    created_time TEXT,                  -- 微博发布时间
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 抓取时间
    UNIQUE(weiboid, weibo_id),          -- 防止重复记录
    FOREIGN KEY (weiboid) REFERENCES account_state(weiboid)
);

CREATE INDEX idx_weibo_history_weiboid ON weibo_history(weiboid);
CREATE INDEX idx_weibo_history_fetched_at ON weibo_history(fetched_at);
```

**设计要点：**
- `UNIQUE(weiboid, weibo_id)`：防止同一条微博被重复保存
- 索引优化：按账号查询和按时间排序的场景
- 外键约束：确保数据完整性

#### 3. push_log（推送日志表）

分渠道记录每次推送的结果。

```sql
CREATE TABLE push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weiboid TEXT NOT NULL,
    weibo_id TEXT NOT NULL,
    channel TEXT NOT NULL,              -- 'telegram' 或 'serverchan'
    status TEXT NOT NULL,               -- 'success' 或 'failed'
    error_message TEXT,                 -- 失败时的错误信息
    pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (weiboid) REFERENCES account_state(weiboid)
);

CREATE INDEX idx_push_log_weiboid ON push_log(weiboid);
CREATE INDEX idx_push_log_pushed_at ON push_log(pushed_at);
CREATE INDEX idx_push_log_status ON push_log(status);
```

**设计要点：**
- 分渠道记录：一条微博可能有 2 条日志（telegram + serverchan）
- 记录错误信息：便于排查推送失败原因
- 索引优化：支持按账号、时间、状态查询

### 数据库文件

- **位置**：`weibo_monitor.db`（项目根目录）
- **格式**：SQLite 3
- **编码**：UTF-8

## 架构设计

### Repository 模式

采用 Repository 模式封装所有数据库操作，提供清晰的数据访问层。

```
┌─────────────────────────────────────────┐
│         core/app.py (应用层)             │
│  - 调度器                                │
│  - 业务逻辑                              │
└─────────────┬───────────────────────────┘
              │ 调用
              ↓
┌─────────────────────────────────────────┐
│    state/repository.py (数据访问层)      │
│  - StateRepository 类                    │
│  - 内存缓存管理                          │
│  - 数据库操作封装                        │
└─────────────┬───────────────────────────┘
              │ 读写
              ↓
┌─────────────────────────────────────────┐
│      weibo_monitor.db (数据层)          │
│  - account_state                         │
│  - weibo_history                         │
│  - push_log                              │
└─────────────────────────────────────────┘
```

### StateRepository 类

核心数据访问类，负责所有数据库操作。

#### 初始化

```python
class StateRepository:
    def __init__(self):
        self._cache: Dict[str, Dict] = {}  # 内存缓存
        self._db_path = Path(__file__).parent.parent / "weibo_monitor.db"
        self._write_lock = None  # asyncio.Lock
    
    async def initialize(self):
        """初始化数据库并加载状态到内存"""
        import asyncio
        self._write_lock = asyncio.Lock()
        await self._ensure_database()  # 创建表结构
        await self._load_cache()       # 加载到内存
```

#### 核心方法

**状态管理（向后兼容）：**

```python
def get_state(self) -> Dict:
    """返回与 load_state() 相同格式的字典"""
    return {"accounts": self._cache.copy()}

def get_latest_id(self, weiboid: str) -> str:
    """从内存获取 latest_id（零数据库查询）"""
    return self._cache.get(weiboid, {}).get("latest_id", "")

async def set_latest_id(self, weiboid: str, latest_id: str, screen_name: str = None):
    """更新 latest_id（先更新内存，再异步写数据库）"""
    # 1. 立即更新内存缓存
    if weiboid not in self._cache:
        self._cache[weiboid] = {}
    self._cache[weiboid]["latest_id"] = latest_id
    
    # 2. 异步写入数据库
    async with self._write_lock:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                INSERT INTO account_state (weiboid, latest_id, screen_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(weiboid) DO UPDATE SET
                    latest_id = excluded.latest_id,
                    screen_name = excluded.screen_name,
                    updated_at = excluded.updated_at
            """, (weiboid, latest_id, screen_name, datetime.now().isoformat()))
            await db.commit()
```

**扩展功能：**

```python
async def save_weibo_history(self, weibo_info: Dict):
    """保存微博历史记录"""
    async with aiosqlite.connect(self._db_path) as db:
        await db.execute("""
            INSERT INTO weibo_history 
            (weiboid, weibo_id, text, screen_name, source, region_name, created_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(weiboid, weibo_id) DO NOTHING
        """, (...))
        await db.commit()

async def log_push(self, weiboid: str, weibo_id: str, channel: str, 
                   status: str, error_message: str = None):
    """记录推送日志"""
    async with aiosqlite.connect(self._db_path) as db:
        await db.execute("""
            INSERT INTO push_log (weiboid, weibo_id, channel, status, error_message)
            VALUES (?, ?, ?, ?, ?)
        """, (weiboid, weibo_id, channel, status, error_message))
        await db.commit()

async def get_weibo_history(self, weiboid: str, limit: int = 100) -> List[Dict]:
    """查询微博历史记录"""
    async with aiosqlite.connect(self._db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM weibo_history 
            WHERE weiboid = ? 
            ORDER BY fetched_at DESC 
            LIMIT ?
        """, (weiboid, limit)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_push_stats(self, days: int = 7) -> Dict:
    """获取推送统计（最近N天）"""
    async with aiosqlite.connect(self._db_path) as db:
        async with db.execute("""
            SELECT channel, status, COUNT(*) as count
            FROM push_log
            WHERE pushed_at >= datetime('now', '-' || ? || ' days')
            GROUP BY channel, status
        """, (days,)) as cursor:
            rows = await cursor.fetchall()
            return {f"{row[0]}_{row[1]}": row[2] for row in rows}
```

### 向后兼容层

在 `state/store.py` 中保留现有函数，内部调用 Repository：

```python
_repository: Optional[StateRepository] = None

async def get_repository() -> StateRepository:
    """获取全局仓库实例（单例模式）"""
    global _repository
    if _repository is None:
        _repository = StateRepository()
        await _repository.initialize()
    return _repository

def load_state() -> Dict:
    """向后兼容：返回内存中的状态"""
    loop = asyncio.get_event_loop()
    repo = loop.run_until_complete(get_repository())
    return repo.get_state()

def save_state(state: Dict) -> None:
    """向后兼容：空操作（状态自动持久化）"""
    logger.debug("save_state() 调用已忽略（状态自动持久化）")

def get_latest_id(state: Dict, weiboid: str) -> str:
    """向后兼容：从字典读取"""
    return state.get("accounts", {}).get(weiboid, {}).get("latest_id", "")

def set_latest_id(state: Dict, weiboid: str, latest_id: str) -> None:
    """向后兼容：更新字典"""
    state.setdefault("accounts", {})[weiboid] = {"latest_id": latest_id}
```

**设计要点：**
- 单例模式：全局只有一个 `StateRepository` 实例
- 函数签名完全不变：现有代码无需修改
- `save_state()` 变为空操作：状态由 Repository 自动持久化

## 性能优化

### 内存缓存策略

**启动时加载：**
```python
async def _load_cache(self):
    """从数据库加载所有账号状态到内存"""
    async with aiosqlite.connect(self._db_path) as db:
        async with db.execute("SELECT weiboid, latest_id FROM account_state") as cursor:
            async for row in cursor:
                self._cache[row[0]] = {"latest_id": row[1]}
```

**读取操作：**
- 从内存字典直接读取
- 时间复杂度：O(1)
- 零数据库查询

**写入操作：**
- 先更新内存（立即生效）
- 再异步写数据库（不阻塞）
- 使用 `asyncio.Lock` 保护并发写入

### 性能指标

| 操作 | 当前（YAML） | 迁移后（SQLite） | 说明 |
|------|-------------|-----------------|------|
| 启动加载 | ~10ms | ~50ms | 增加数据库初始化时间 |
| 读取 latest_id | ~0.1ms | ~0.1ms | 都是内存操作，无差异 |
| 写入 latest_id | ~5ms | ~5ms | 异步写入，不阻塞主流程 |
| 内存占用 | ~1KB | ~10KB | 缓存所有账号状态 |

**结论：** 性能影响可忽略，读取性能完全一致。

## 数据迁移

### 迁移脚本

提供 `state/migration.py` 脚本，手动执行迁移。

```python
async def migrate_from_yaml():
    """从 state.yaml 迁移到 SQLite"""
    # 1. 检查 state.yaml 是否存在
    if not STATE_YAML_PATH.exists():
        logger.info("state.yaml 不存在，跳过迁移")
        return
    
    # 2. 读取 YAML 数据
    with open(STATE_YAML_PATH, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f) or {}
    
    accounts = yaml_data.get("accounts", {})
    
    # 3. 初始化仓库
    repo = StateRepository()
    await repo.initialize()
    
    # 4. 迁移数据
    for weiboid, data in accounts.items():
        latest_id = data.get("latest_id", "")
        if latest_id:
            await repo.set_latest_id(weiboid, latest_id)
            logger.info(f"已迁移账号 {weiboid}: {latest_id}")
    
    # 5. 备份原文件
    STATE_YAML_PATH.rename(STATE_YAML_BACKUP)
    logger.info("迁移完成，原文件已备份为 state.yaml.backup")
```

### 迁移步骤

```bash
# 1. 备份当前数据（可选，脚本会自动备份）
cp state.yaml state.yaml.original

# 2. 执行迁移
python -m state.migration

# 3. 验证数据库内容
sqlite3 weibo_monitor.db "SELECT * FROM account_state;"

# 4. 测试运行
python core/main.py
```

### 回滚方案

如果迁移后出现问题，可以回滚：

```bash
# 方案 1：使用备份文件
cp state.yaml.backup state.yaml

# 方案 2：从 SQLite 导出
python -m state.migration rollback
```

## 代码改动

### 新增文件

1. **state/repository.py** - Repository 层实现
   - `StateRepository` 类
   - 数据库初始化逻辑
   - 所有数据库操作方法

2. **state/migration.py** - 迁移工具
   - `migrate_from_yaml()` - YAML → SQLite
   - `rollback_to_yaml()` - SQLite → YAML

### 修改文件

1. **state/store.py** - 向后兼容层
   - 保留现有函数签名
   - 内部调用 Repository

2. **core/app.py** - 应用层集成
   - 初始化 Repository
   - 调用扩展功能（保存历史、记录日志）

3. **requirements.txt** - 添加依赖
   - `aiosqlite>=0.19.0`

### core/app.py 改动示例

```python
# 修改导入
from state.repository import get_repository, StateRepository

class App:
    def __init__(self):
        # ... 现有字段 ...
        self.repository: StateRepository | None = None  # 新增

    async def start(self):
        # ... 现有代码 ...
        
        # 初始化仓库（替代 load_state）
        self.repository = await get_repository()
        self.state = self.repository.get_state()
        
        # ... 现有代码 ...

    async def _check_single(self, account: dict):
        weiboid = account["weiboid"]
        try:
            info = await self.monitor.get_latest_weibo(weiboid)
            if not info:
                return

            # 从内存读取（零查询）
            old_id = self.repository.get_latest_id(weiboid)
            if old_id == info["id"]:
                return

            # ... 推送逻辑 ...
            
            try:
                await self.notifer.send_message(message, telegram_message, title)
            except Exception as e:
                # 记录推送失败
                await self.repository.log_push(weiboid, info["id"], "all", "failed", str(e))
                return

            # 推送成功后更新状态
            await self.repository.set_latest_id(weiboid, info["id"], info["screen_name"])
            
            # 保存微博历史
            info["weiboid"] = weiboid
            await self.repository.save_weibo_history(info)
            
            # 记录推送成功
            await self.repository.log_push(weiboid, info["id"], "telegram", "success")
            await self.repository.log_push(weiboid, info["id"], "serverchan", "success")
            
        except Exception:
            logger.exception(f"检查微博更新失败: {weiboid}")
```

**关键改动：**
- 移除 `save_state(self.state)` 调用
- 使用 `self.repository.get_latest_id(weiboid)` 替代 `get_latest_id(self.state, weiboid)`
- 使用 `await self.repository.set_latest_id(...)` 替代 `set_latest_id(self.state, ...)`
- 新增微博历史保存和推送日志记录

## 测试验证

### 功能测试

```bash
# 1. 启动应用
python core/main.py

# 2. 观察日志
tail -f log/runtime_*.log

# 3. 验证关键行为
# - 启动时加载状态（"状态仓库初始化完成，已加载 N 个账号状态"）
# - 检测到新微博时推送（"检测到新微博，准备推送"）
# - 推送成功后更新状态（"推送成功，状态已更新"）

# 4. 验证数据库写入
sqlite3 weibo_monitor.db "SELECT * FROM weibo_history ORDER BY fetched_at DESC LIMIT 5;"
sqlite3 weibo_monitor.db "SELECT * FROM push_log ORDER BY pushed_at DESC LIMIT 5;"
```

### 性能测试

对比迁移前后的性能指标：
- 启动时间（加载状态）
- 检查延迟（get_latest_id 调用）
- 内存占用

预期结果：
- 启动时间增加 < 100ms
- 检查延迟无变化
- 内存占用增加 < 1MB

### 数据一致性测试

```python
# 验证迁移数据一致性
import yaml
import sqlite3

# 读取原 YAML
with open('state.yaml.backup', 'r') as f:
    yaml_data = yaml.safe_load(f)

# 读取 SQLite
conn = sqlite3.connect('weibo_monitor.db')
cursor = conn.execute('SELECT weiboid, latest_id FROM account_state')
db_data = {row[0]: row[1] for row in cursor}

# 对比
for weiboid, data in yaml_data['accounts'].items():
    assert db_data[weiboid] == data['latest_id'], f'数据不一致: {weiboid}'

print('✓ 迁移数据一致性验证通过')
```

## 风险评估

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 迁移失败导致数据丢失 | 高 | 低 | 自动备份 `state.yaml.backup`，保留原文件 |
| 数据库文件损坏 | 中 | 低 | 提供回滚工具，可从 SQLite 导出到 YAML |
| 性能下降 | 低 | 低 | 内存缓存策略，读取操作零查询 |
| 并发写入冲突 | 低 | 低 | 使用 `asyncio.Lock` 保护写入操作 |
| 依赖安装失败 | 低 | 低 | `aiosqlite` 是纯 Python 库，无 C 扩展依赖 |

## 未来扩展

迁移完成后可扩展的功能：

1. **健康检查**
   - Cookie 有效性自动检测
   - 推送渠道可用性监控
   - 异常告警（长时间无数据）

2. **数据分析**
   - 每日微博数量趋势图
   - 推送成功率统计
   - 活跃时段分析

3. **数据导出**
   - 导出微博历史为 CSV/JSON
   - 生成推送报告

4. **高级查询**
   - 按时间范围查询微博
   - 按关键词搜索历史记录
   - 推送失败记录查询

## 总结

本设计方案采用 Repository 模式 + 内存缓存策略，在保持现有 API 接口不变的前提下，将状态管理从 YAML 迁移到 SQLite，并增加微博历史记录和推送日志功能。

**核心优势：**
- 向后兼容：现有代码改动最小
- 高性能：读取零延迟，写入异步不阻塞
- 可扩展：统一的 Repository 层便于后续功能扩展
- 安全迁移：手动迁移脚本，支持备份和回滚

**实施路径：**
1. 实现 Repository 层（`state/repository.py`）
2. 实现迁移工具（`state/migration.py`）
3. 修改应用层集成（`core/app.py`）
4. 执行数据迁移
5. 功能测试和性能验证
