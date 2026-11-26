# WeiboMonitor

监控多个微博账号的动态，并通过 Telegram 或 Server酱发送事件通知。

## 目录

- [安装](#安装)
- [用法](#用法)
- [功能特性](#功能特性)
- [配置](#配置)
- [数据迁移](#数据迁移)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [API](#api)
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

- 默认每 8 秒从各账号的时间线首页检查发博和转发。额外的活动检查约每分钟运行一次。持续运行时，每个账号的时间线、活动检查和推送独立调度。
- Telegram 和 Server酱可独立启用，同时启用时并发推送。
- 首次成功读取时保存基线。已有 SQLite 数据库会沿用发博进度，并在启动时自动建事件表。
- SQLite 在同一事务里保存观测快照和待推送事件。任一渠道成功后继续处理下一条。全部失败时下次重试，包括已滚出时间线首页的事件。连续失败 5 次后保留失败记录并继续处理下一条。两个渠道均关闭时，程序仍记录事件，待重新启用渠道后发送。
- 同一账号按入队顺序推送。SQLite 能协调并行运行的推送进程。通知已被渠道接收、服务却在写回结果前中断时，后续重试可能重复发送。

## 配置

以 [`users.yaml.example`](users.yaml.example) 为模板编辑 `users.yaml`。

| 字段 | 说明 |
| --- | --- |
| `cookie` | 微博 Cookie，登录微博后从浏览器开发者工具获取 |
| `accounts[].weiboid` | 要监控的微博用户 ID，建议用带引号的字符串填写 |
| `accounts[].events` | 要启用的事件列表。省略时启用 `post.created`、`post.reposted`，设为空列表可停用该账号的新事件 |
| `accounts[].keywords` | `keyword.matched` 必填，按不区分大小写的子字符串匹配 |
| `accounts[].comment_post_ids` | `comment.created` 必填，填写要查询评论区的数字微博 ID |
| `accounts[].following_page_limit` | 关注列表单轮最大页数，默认 20，允许 1–50 |
| `accounts[].activity_page_limit` | 每轮点赞列表或每条指定微博的评论列表最大页数，默认 2，允许 1–5 |
| `notification.enable_telegram` | 默认为 `true`，设为 `false` 可关闭 Telegram 推送 |
| `notification.enable_serverchan` | 默认为 `true`，设为 `false` 可关闭 Server酱推送 |
| `notification.tgbottoken` | 启用 Telegram 时填写 Bot Token |
| `notification.chatid` | 启用 Telegram 时填写聊天 ID |
| `notification.telegram_template` | 启用 Telegram 时填写 MarkdownV2 消息模板 |
| `notification.sendkey` | 启用 Server酱时填写 SendKey，支持新旧版格式 |

`post.created` 通知沿用 Telegram 模板，支持 `{screen_name}`、`{text}`、`{region_name}`、`{source}` 和 `{url}`。程序会转义占位符的值，自定义模板中的字面字符需按 MarkdownV2 规则编写。模板渲染后为空时发送微博链接。其他事件使用各自的内置消息格式。

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
│   ├── events.py           # 事件类型和序列化
│   ├── event.schema.json   # Event v1 的 JSON Schema
│   ├── index.py            # 单次检查和阿里云函数入口
│   └── main.py             # 持续轮询入口
├── monitor/
│   ├── events.py           # 快照比较和事件检测
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

## API

### Event v1

SQLite 的 `events.payload` 保存符合 [Event JSON Schema](core/event.schema.json) 的 JSON。各事件使用统一的结构。

```json
{
  "schema_version": 1,
  "event_id": "a6dd4c1a-742d-4ae0-9b36-eb111bae4b98",
  "type": "following.added",
  "account_id": "1234567890",
  "actor_id": "1234567890",
  "subject": { "type": "user", "id": "9876543210" },
  "occurred_at": null,
  "observed_at": "2026-11-25T04:00:00Z",
  "source": "/ajax/friendships/friends",
  "data": {
    "user": { "id": "9876543210", "screen_name": "示例账号" },
    "url": "https://weibo.com/u/9876543210"
  }
}
```

- `account_id` 是配置中的账号，`actor_id` 是可归属的行为账号，`subject` 是事件涉及的微博、用户或评论。删除事件的行为人未知。
- `occurred_at` 仅在接口有可解析的实际时间时填写，`observed_at` 始终记录检测时间。两者使用 UTC。
- `event_id` 在重试时保持一致。发博、转发、评论、点赞和关键词匹配按账号与对象去重。编辑、资料变化、关注变化和置顶变化可对同一对象多次发生。
- `events.delivered_at` 记录首次成功送达时间，`events.failed_at` 记录达到重试上限的时间，`events.delivery_attempts` 和 `event_push_log` 记录尝试次数和各渠道结果。

### 事件及覆盖范围

| 事件 | 数据与判定 |
| --- | --- |
| `post.created` | 时间线首页的新微博，`data.post` 为该条微博。 |
| `post.reposted` | 时间线包含 `retweeted_status` 的新条目，另记录 `data.original_post_id`。原帖 ID 缺失时为 `null`。 |
| `post.updated` | 比较连续可见的首页微博正文和媒体标识，保存 `data.before`、`data.after`。长文展开失败时保留先前的内容基线。 |
| `post.deleted` | 曾明确公开的微博从有效首页消失后，使用匿名移动端详情核验。两次核验都得到明确的不存在信号才入队，记录 `data.confirmations`。详情仍可见、权限受限或响应不明确时不发事件。 |
| `following.added` | 比较分页完整、无过滤且实际 UID 数与总数相符的两份名单，记录 `data.user`。 |
| `following.removed` | 同一 UID 连续两轮从有效完整名单消失才通知。过滤、请求失败或页数用尽会中断确认，并保留此前的有效基线。 |
| `profile.updated` | 比较昵称、头像和简介，保存 `data.before`、`data.after` 和已变字段 `data.fields`。 |
| `post.liked` | 只在已验证可读的点赞列表里，通知新进入上一次已知列表边界之前的条目。按微博 ID 记录首次观测，实际点赞时间未知。首轮、空页或列表之间无重叠时无法确认新增。 |
| `comment.created` | 从指定微博的一级评论读取作者 UID，仅记录监控账号发表、且在建立基线之后发布的评论。保存 `data.comment` 和 `data.post_id`。 |
| `post.pinned` | 比较完整桌面时间线或可核验的移动端时间线，跨来源保留最近已确认的置顶状态。残缺响应不能单独作为取消置顶的证据。没有置顶时 ID 为 `null`。 |
| `keyword.matched` | 从新发微博或转发的正文匹配账号的关键词，保存命中列表 `data.keywords`。展开长文失败时，只要该帖仍在首页就会继续检查。普通发博或转发通知可先发送。 |

接口读到的数据受 Cookie、账号可见性和页面范围影响。桌面端 Cookie 只发给微博桌面站。更换 Cookie 后，个人资料、关注、点赞、评论、编辑、删除和置顶检查重新建立该登录视角的基线。

## 贡献

欢迎在 [GitHub Issues](https://github.com/JiangQianJiang/WeiboMonitor/issues) 提问或报告问题，也欢迎提交 Pull Request。提交信息请遵循 [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)。合并前请确认测试、代码检查和 CI 都已通过。完整要求见 [AGENTS.md](AGENTS.md)。

```bash
python -m pip install pytest pytest-asyncio
python -m pytest tests/ -v
```

## 许可证

[MIT](LICENSE) · 版权所有 © 2025–2026 JiangQianJiang
