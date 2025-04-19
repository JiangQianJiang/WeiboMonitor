# Weibo Monitor

Weibo Monitor 是一个用于监控微博更新的工具，能够定期检查指定微博账号的最新动态，并通过 Telegram 和 Server酱推送通知。项目使用 Python 编写，支持异步操作和任务调度。

## 功能特性

- 定期监控指定微博账号的最新动态。
- 支持通过 Telegram 和 Server酱推送通知。
- 使用 YAML 文件进行配置，支持多账号监控。
- 自动保存最新微博 ID，避免重复通知。
- 日志记录，便于调试和监控。

## 目录结构

```
project_root/
├── core/
│   ├── main.py        # 主逻辑：调度、配置加载、任务执行
│   └── index.py       # 入口：支持阿里云函数或其他部署方式
├── monitor/
│   └── weibo.py       # 微博监控逻辑
├── notifier/
│   └── notifer.py     # 通知发送逻辑
├── log/               # 日志文件目录（自动生成）
├── users.yaml         # 配置文件
├── users.yaml.example # 配置文件示例
└── README.md          # 项目说明文档
```

## 安装依赖

安装依赖：

```bash
pip install -r requirements.txt
```


## 配置

项目使用 `users.yaml` 文件进行配置。请按照以下步骤设置：

1. 复制示例配置文件：

   ```bash
   cp users.yaml.example users.yaml
   ```

2. 编辑 `users.yaml`，填写你的配置信息。以下是 `users.yaml.example` 的内容说明：

   ```yaml
   # 示例配置文件，用于微博监控和通知推送
   # 使用前，请复制此文件为 users.yaml，并填写正确的配置信息
   
   # 要监控的微博账号列表
   accounts:
     - weiboid: "1234567890"  # 微博用户ID，可在微博主页URL中找到
       latest_id: ""          # 最新微博ID，初次使用可留空，程序会自动更新
     - weiboid: "9876543210"  # 另一个微博用户ID
       latest_id: ""
   
   # 通知配置
   notification:
     # Telegram 消息模板，支持格式化字段：{screen_name}, {text}, {region_name}, {source}, {url}
     telegram_template: |
       *{screen_name}* 发表了新微博：
       {text}
       {region_name} | {source}
       [查看详情]({url})
     tgbottoken: "<YOUR_TELEGRAM_BOT_TOKEN>"  # Telegram Bot 的 Token
     chatid: "<YOUR_CHAT_ID>"                 # Telegram 聊天ID
     num: "<YOUR_SERVERCHAN_NUM>"             # Server酱推送的 num 参数
     sendkey: "<YOUR_SERVERCHAN_SENDKEY>"     # Server酱推送的 sendkey 参数
   ```

   - `weiboid`：微博用户 ID，可从微博主页 URL 获取（例如 `https://weibo.com/1234567890` 中的 `1234567890`）。
   - `tgbottoken`：通过 Telegram 的 BotFather 创建 Bot 后获取。
   - `chatid`：你的 Telegram 聊天 ID，可通过 `@userinfobot` 获取。
   - `num` 和 `sendkey`：Server酱推送的参数，可在 Server酱官网 注册获取。

## 运行

### 本地运行

1. 确保 `users.yaml` 已正确配置。
2. 运行主程序：

   ```bash
   python core/main.py
   ```

   程序将每 8 秒检查一次微博更新，并推送通知到配置的 Telegram 和 Server酱。


## 贡献

欢迎提交 Issue 或 Pull Request，帮助改进项目！

## 许可证

本项目采用 MIT 许可证