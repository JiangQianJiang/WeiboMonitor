# Weibo Monitor

微博监控工具：定时轮询指定微博账号的最新动态，通过 Telegram 和 Server酱双渠道推送通知。

## 功能特性

- 定时监控多个微博账号的最新动态（8 秒间隔）
- Telegram + Server酱双渠道**并发推送**
- SQLite 持久化，自动去重，冷启动不推送
- 推送失败自动中断，下次轮询从失败项继续
- Docker 部署，开箱即用

## 技术栈

Python 3.12+, asyncio + aiohttp, APScheduler, loguru, PyYAML, python-telegram-bot, aiosqlite

## 目录结构

```
WeiboMonitor/
├── core/
│   ├── app.py              # 主应用：调度器、更新检测、推送编排
│   ├── config.py           # 配置管理：加载/保存 users.yaml
│   └── main.py             # 入口
├── monitor/
│   └── weibo.py            # WeiboMonitor：异步获取微博 API
├── notifer/
│   └── notifer.py          # Notifer：Telegram + Server酱 推送
├── state/
│   ├── repository.py       # SQLite 持久化层
│   ├── schema.sql          # 数据库表结构
│   ├── migration.py        # state.yaml → SQLite 迁移工具
│   └── store.py            # 全局单例访问
├── tests/                  # 测试
├── data/                   # SQLite 数据库（Docker 持久化）
├── log/                    # 运行日志
├── users.yaml.example      # 配置模板
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker）

```bash
# 1. 克隆项目
git clone <your-repo> && cd WeiboMonitor

# 2. 创建配置文件
cp users.yaml.example users.yaml
# 编辑 users.yaml，填入你的微博 Cookie 和通知配置

# 3. 启动
docker compose up -d

# 4. 查看日志
docker compose logs -f
```

更新代码后：

```bash
git pull && docker compose restart
```

## 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 配置
cp users.yaml.example users.yaml
# 编辑 users.yaml

# 运行
python -m core.main

# 运行测试
pytest tests/ -v
```

## 配置

复制模板并填写：

```bash
cp users.yaml.example users.yaml
```

| 字段 | 说明 |
|------|------|
| `cookie` | 微博 Cookie，从浏览器开发者工具获取 |
| `accounts[].weiboid` | 要监控的微博用户 ID |
| `notification.tgbottoken` | Telegram Bot Token |
| `notification.chatid` | Telegram 聊天 ID |
| `notification.sendkey` | Server酱 SendKey |

> ⚠️ `users.yaml` 包含敏感凭据，**不要提交到 Git**（已在 `.gitignore` 中排除）。

## 数据迁移

从旧版 `state.yaml` 迁移到 SQLite：

```bash
python -m state.migration migrate
```

回滚：

```bash
python -m state.migration rollback
```

## 许可证

MIT
