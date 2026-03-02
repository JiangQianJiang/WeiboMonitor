# Weibo Monitor

微博监控工具：定时轮询指定微博账号的最新动态，通过 Telegram 和 Server酱双渠道推送通知。

## 功能特性

- 定时监控多个微博账号的最新动态（8秒间隔）
- Telegram + Server酱双渠道并发推送
- 状态持久化与配置分离，自动去重
- 支持本地运行和阿里云函数计算部署

## 技术栈

Python 3.7+, asyncio + aiohttp, APScheduler, loguru, PyYAML, python-telegram-bot

## 目录结构

```
WeiboMonitor/
├── core/
│   ├── app.py            # 主应用类：调度器、配置加载、更新检测
│   ├── config.py         # 配置管理：加载/保存 users.yaml
│   ├── main.py           # 旧入口（已弃用，保留兼容）
│   └── index.py          # 阿里云FC部署入口
├── monitor/
│   └── weibo.py          # WeiboMonitor类：异步获取微博API
├── notifer/
│   └── notifer.py        # Notifer类：Telegram + Server酱推送
├── state/
│   ├── __init__.py
│   └── store.py          # 状态管理：latest_id 去重，持久化到 state.yaml
├── docs/                 # 项目文档
├── users.yaml.example    # 配置示例
├── requirements.txt
└── README.md
```

## 执行流程

调度器(8秒) → 并发检查所有账号(`asyncio.gather`) → 微博API → 对比 `latest_id` → 更新 `state.yaml` → 并发推送

## 安装

```bash
pip install -r requirements.txt
```

## 配置

复制示例配置并填写：

```bash
cp users.yaml.example users.yaml
```

关键字段说明：

```yaml
cookie: "<YOUR_WEIBO_COOKIE>"       # 微博 Cookie，从浏览器开发者工具获取

accounts:
  - weiboid: "1234567890"           # 微博用户ID

notification:
  telegram_template: |              # Telegram 消息模板
    *{screen_name}* 发表了新微博：
    {text}
    {region_name} | {source}
    [查看详情]({url})
  tgbottoken: "<BOT_TOKEN>"        # Telegram Bot Token
  chatid: "<CHAT_ID>"              # Telegram 聊天ID
  num: "<SERVERCHAN_NUM>"          # Server酱 num
  sendkey: "<SERVERCHAN_SENDKEY>"  # Server酱 sendkey
```

## 运行

本地运行：

```bash
python core/main.py
```

阿里云函数计算入口：`core/index.handler`

## 许可证

MIT
