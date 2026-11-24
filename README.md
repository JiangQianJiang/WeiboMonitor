# WeiboMonitor

微博内容监控并通知，支持多账号监控

## 目录

- [安装](#安装)
- [用法](#用法)
- [功能特性](#功能特性)
- [配置](#配置)
- [数据迁移](#数据迁移)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [贡献](#贡献)
- [许可证](#许可证)

## 安装

### 依赖

使用 Git 获取源码。Docker 部署需要 Docker 和 Docker Compose。本地运行、测试或在宿主机执行数据迁移时，请使用 Python 3.12+。

```bash
git clone https://github.com/JiangQianJiang/WeiboMonitor.git
cd WeiboMonitor
cp users.yaml.example users.yaml
```

启动前，按[配置](#配置)填写 `users.yaml`。本地运行或执行迁移前，再安装 Python 依赖。

```bash
python3.12 -m venv "$HOME/.venvs/weibo-monitor"
source "$HOME/.venvs/weibo-monitor/bin/activate"
python -m pip install -r requirements.txt
```

## 用法

### Docker

```bash
docker compose up -d --build
docker compose logs -f
```

数据库保存在 `./data/weibo_monitor.db`，日志保存在 `./log/`。

执行 `git pull` 后，按变更内容执行对应命令。

| 变更 | 命令 |
| --- | --- |
| 挂载的 Python 源码 | `docker compose restart` |
| `docker-compose.yml` | `docker compose up -d` |
| `requirements.txt` 或 `Dockerfile` | `docker compose up -d --build` |

### CLI

在项目根目录启动本地轮询。

```bash
python -m core.main
```

只检查一次微博更新时，运行 `python -m core.index`。本地数据库默认保存在项目根目录的 `weibo_monitor.db`。如需改用其他位置，将 `STATE_DIR` 设为已存在的目录。

## 功能特性

- 每 8 秒检查多个微博账号的最新动态，每个账号只读取第一页。
- Telegram 和 Server酱可独立启用，同时启用时并发推送。
- SQLite 保存每个账号的最新微博 ID 和推送记录。首次记录账号时，仅保存当前最新微博 ID。
- 任一渠道推送成功后，程序就会更新该账号的最新微博 ID、记录各渠道结果，然后继续推送下一条。本条微博没有渠道推送成功时，暂停该账号本轮剩余推送，下次轮询重试仍在第一页的待推送条目。

## 配置

以 [`users.yaml.example`](users.yaml.example) 为模板编辑 `users.yaml`。

| 字段 | 说明 |
| --- | --- |
| `cookie` | 微博 Cookie，登录微博后从浏览器开发者工具获取 |
| `accounts[].weiboid` | 要监控的微博用户 ID，建议用带引号的字符串填写 |
| `notification.enable_telegram` | 默认为 `true`，设为 `false` 可关闭 Telegram 推送 |
| `notification.enable_serverchan` | 默认为 `true`，设为 `false` 可关闭 Server酱推送 |
| `notification.tgbottoken` | 启用 Telegram 时填写 Bot Token |
| `notification.chatid` | 启用 Telegram 时填写聊天 ID |
| `notification.telegram_template` | 启用 Telegram 时填写 MarkdownV2 消息模板 |
| `notification.sendkey` | 启用 Server酱时填写 SendKey，支持新旧版格式 |

Telegram 模板支持 `{screen_name}`、`{text}`、`{region_name}`、`{source}` 和 `{url}`。程序会转义占位符的值，自定义模板中的字面字符需按 MarkdownV2 规则编写。

`users.yaml` 包含敏感凭据，请保存在本地。Git 已忽略该文件。

## 数据迁移

迁移和回滚前先停止 WeiboMonitor。Docker 部署可运行 `docker compose stop`。安装 Python 依赖后，在宿主机的项目根目录执行命令。

将旧版 `state.yaml` 中的账号进度导入 SQLite。迁移时会备份旧状态文件。

```bash
python -m state.migration migrate
```

Docker 部署需要将数据库写入 Compose 挂载的 `./data` 目录。

```bash
mkdir -p ./data
STATE_DIR=./data python -m state.migration migrate
```

数据库中有账号记录时，回滚命令将各账号的 `latest_id` 导出到 `state.yaml`，并覆盖已有的同名文件。

```bash
python -m state.migration rollback
```

Docker 部署从 `./data` 下的数据库导出。

```bash
STATE_DIR=./data python -m state.migration rollback
```

## 技术栈

Python 3.12+、asyncio、aiohttp、APScheduler、loguru、PyYAML、python-telegram-bot、aiosqlite

## 目录结构

```text
WeiboMonitor/
├── core/
│   ├── app.py              # 调度器、更新检测和推送编排
│   ├── config.py           # 加载和保存 users.yaml
│   ├── index.py            # 单次检查和阿里云函数入口
│   └── main.py             # 持续轮询入口
├── monitor/
│   └── weibo.py            # 异步获取微博数据
├── notifer/
│   └── notifer.py          # Telegram 和 Server酱推送
├── state/
│   ├── repository.py       # SQLite 持久化层
│   ├── schema.sql          # 数据库表结构
│   ├── migration.py        # 旧版状态迁移和回滚
│   └── store.py            # 持久化层的全局访问入口
├── tests/                  # 测试
├── data/                   # Docker 部署使用的数据库目录
├── log/                    # 运行日志目录
├── users.yaml.example      # 配置模板
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 贡献

欢迎在 [GitHub Issues](https://github.com/JiangQianJiang/WeiboMonitor/issues) 提问或报告问题，也欢迎提交 Pull Request。提交信息请遵循 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)。合并前请确认测试、代码检查和 CI 都已通过。完整要求见 [AGENTS.md](AGENTS.md)。

```bash
python -m pip install pytest pytest-asyncio
python -m pytest tests/ -v
```

## 许可证

[MIT](LICENSE) · 版权所有 © 2025–2026 JiangQianJiang
