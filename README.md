# CCMax 飞书测试机器人

飞书企业自建应用机器人（长连接）：接收受控群聊中 `@机器人` 发起的测试命令，经发起人二次确认后，**串行调用本机 CCMax 工作台**的截图对齐/全面测试与 Claude 透传测试，向原群回复摘要及 Markdown 报告，并在本地保存 JSON 和 Markdown。

> **这是机器人适配器的独立仓库，不包含 CCMax 工作台源码。** 必须先自行部署提供 `/api/screenshot-suite/run` 和 `/api/claude-passthrough/run` 的 CCMax 本地工作台；缺少工作台时只能进行配置检查和离线单元测试，不能运行真实测试。当前飞书应用如遇 `tenant_access_token` 返回 `21001`，还需由飞书平台核查；仓库发布不代表已经完成飞书线上接入。

## 快速开始

需要 Python 3.10+ 和已发布、启用机器人的飞书企业自建应用。先启用机器人、订阅 `im.message.receive_v1` 长连接事件，并按 [完整配置说明](docs/飞书机器人配置与使用.md) 开通发消息和上传文件权限。

```powershell
python -m pip install -r .\requirements-feishu.txt
# 在本机环境变量中安全配置以下五项；不要在源码、Git 提交或公开日志中存放真实值。
# FEISHU_APP_ID / FEISHU_APP_SECRET
# FEISHU_ALLOWED_OPEN_IDS / FEISHU_ALLOWED_CHAT_IDS / FEISHU_BOT_OPEN_ID
python .\ccmax_feishu_bot.py --check
# 另一个终端启动本机 CCMax 工作台（默认 http://127.0.0.1:8765）后：
python .\ccmax_feishu_bot.py
```

先在白名单群以 `@机器人 帮助` 做**无 Key 联调**。通过后，在可信且访问受控的测试群发送：

```text
@机器人 测试 https://your-gateway.example.com|YOUR_KEY|YOUR_MODEL
@机器人 确认 <机器人返回的令牌>
```

**安全与费用**：群消息中的 Key 可被群成员看到并留在聊天记录中；不要使用生产密钥。最多 56 次截图对齐/全面类请求 + 10 次透传请求，总计最多 66 次；请求数不等于费用上限。未经获准测试目标与明确预算，不要进行真实测试。黑盒报告不能证明上游模型身份或中转不存在隐藏指令。生成的报告、缓存、日志和本机凭证由 `.gitignore` 排除。

离线验证（不连接飞书、不调用模型）：

```powershell
python -m unittest test_ccmax_feishu_bot -q
python -m py_compile ccmax_feishu_bot.py
```

项目尚未指定开源许可证；公开可见不等于授权他人复制、修改或再分发。若需要开放复用，请由权利人选择并添加许可证。
