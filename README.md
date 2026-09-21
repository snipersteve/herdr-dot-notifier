# Herdr DOT Notifier

Herdr 插件：后台 Agent 状态变为 `done` 时，将完成通知推送到 MindReset Dot 屏幕，并在落款显示 Claude Code 与 ChatGPT Codex 用量进度。

## 环境要求

- macOS
- Herdr `>= 0.8.2`
- 已在 Dot. App 中创建并加入轮播的 Text API 内容项
- 若显示 Claude 用量：已登录 Claude Code
- 若显示 GPT 用量：已通过 Pi 登录 OpenAI Codex

## 安装

```bash
herdr plugin install snipersteve/herdr-dot-notifier -y
CONFIG_DIR="$(herdr plugin config-dir show.herdr-dot-notifier)"
cp ~/.local/share/herdr/plugins/herdr-dot-notifier/config.example.json \
  "$CONFIG_DIR/config.json"
```

编辑 `config.json`，填写 Dot 设备 ID 和 Text API 任务 key：

```json
{
  "api_base": "https://dot.mindreset.tech",
  "device_id": "YOUR_DOT_DEVICE_ID",
  "task_key": "YOUR_TEXT_API_TASK_KEY",
  "task_alias": "Herdr Agent"
}
```

建议将 Dot API Key 存入 macOS Keychain，而不是配置文件：

```bash
security add-generic-password -U \
  -s show.herdr-dot-notifier.dot-api-key \
  -a "$USER" \
  -w 'YOUR_DOT_API_KEY'
chmod 600 "$CONFIG_DIR/config.json"
herdr plugin enable show.herdr-dot-notifier
```

也可在 `config.json` 中填写 `api_key`，但不建议提交或同步该文件。

## 工作方式

- 监听 Herdr 事件 `pane.agent_status_changed`
- 仅处理 `agent_status == "done"`
- 通过 `pane_id + state_change_seq` 去重
- 不使用 Agent 生成的任务标题，所有会话统一显示为“文件夹-Agent名称”（如 `obsidian-Pi`）
- 标题固定为 `Herdr通知`
- 正文固定两行：第一行 `已完成：文件夹-Agent`，第二行 `进行中：文件夹-Agent`
- 同名运行会话按数量显示（如 `obsidian-Pi×2`），没有则显示“进行中：无”
- 任一 Agent 状态变化时都会刷新运行中摘要，不再只在任务完成时生成一次快照
- `signature` 单行显示压缩后的用量摘要：省略 Claude 5 小时窗口和时间，仅保留 Claude 周用量、模型专项用量与 GPT 已用量，例如 `Claude 30%↓ Fable 45%↑ GPT 1%↑`
- 每个百分比后的箭头比较“已用比例”和当前限额窗口的“已流逝时间比例”：`↑` 表示用得更快，`↓` 表示用得更慢，`→` 表示基本同步（误差小于 0.5 个百分点）；`!` 表示 Claude 该限额已非 normal 级别

## Claude Code 用量

- 来源：`GET https://api.anthropic.com/api/oauth/usage`（Claude Code `/usage` 所用的非公开接口，可能变动）
- 凭据：只读 `~/.claude/.credentials.json` 的 `claudeAiOauth.accessToken`；**从不刷新 token**（刷新会轮换 refresh token、与 Claude Code 抢写凭据）。token 过期时显示“用量未知”，等 Claude Code 下次使用时自行刷新
- 缓存 5 分钟（状态目录 `claude_usage.json`，失败也缓存）；用量只随既有推送顺带更新，不单独触发刷屏
- 落款使用 `FusionPixel12` 12 号（屏幕 296×152）
- 可选配置（`config.json`）：`"claude_usage": false` 关闭；`"claude_credentials_path"` 改凭据路径；`"signature_style"` 可覆盖样式

## ChatGPT Codex 用量

- 来源：`GET https://chatgpt.com/backend-api/wham/usage`（Codex 使用的非公开接口，可能变动）
- 凭据：只读 `~/.pi/agent/auth.json` 的 `openai-codex.access` 和 `accountId`；**从不刷新 token、从不改写凭据文件**
- 显示已用比例，与 Claude 的百分比口径一致；内部按窗口长度识别 `GPT5h`、`GPT周` 或 `GPT`
- 独立缓存 5 分钟，失败时显示 `GPT ?`；箭头按接口返回的窗口长度和重置时间动态计算
- 可选配置（`config.json`）：`"chatgpt_usage": false` 关闭；`"openai_auth_path"` 改凭据路径

## 路径

- 插件：`~/.local/share/herdr/plugins/herdr-dot-notifier/`
- 配置：`~/.config/herdr/plugins/config/show.herdr-dot-notifier/config.json`
- 状态与日志：由 Herdr 通过 `HERDR_PLUGIN_STATE_DIR` 分配

配置文件可能含设备信息或 API 密钥，已被 `.gitignore` 排除，权限应保持为 `600`。

## 管理

```bash
herdr plugin list --plugin show.herdr-dot-notifier --json
herdr plugin log list --plugin show.herdr-dot-notifier --limit 20
herdr plugin disable show.herdr-dot-notifier
herdr plugin enable show.herdr-dot-notifier
herdr plugin unlink show.herdr-dot-notifier
```

## License

MIT
