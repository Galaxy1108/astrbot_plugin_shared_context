# Shared Context · 共享上下文

> [!IMPORTANT]
> 本项目代码完全由 AI 生成

让同一个机器人的**不同会话**共享 LLM 上下文。A 私聊说"我去吃饭了"，B 私聊时 AI 会知道（AI 不会向 B 透露 A 的身份和消息来源）。

> [!INFO]
> 本插件**不修改**任何会话的上下文。
> 它不写入会话历史、不修改 `system_prompt`、不改变 `/reset` 和会话隔离的行为；只在每轮 LLM 请求中**临时附加**其他会话的近期消息（`.mark_as_temp()`），请求结束即丢弃。

## 原理

插件被动记录同一机器人下所有会话的消息流水（用户消息 + 机器人回复），在每次 LLM 请求时把**其他会话**最近的消息作为临时上下文块注入（`.mark_as_temp()`）：

- 不进入任何会话的历史存储（`/reset`、WebUI 历史面板、会话隔离全部不受影响）
- 不修改 `system_prompt`（不破坏模型服务端提示词缓存）
- 当前会话自己的历史已在请求中，注入时自动排除，不重复

## 特性

- **默认即用**：开箱即用，同一机器人的所有会话互相共享
- **多共享组**：可配置多个组，仅组内会话互相共享（闭组）
- **跨机器人隔离（硬性底线）**：消息池按 `self_id` 分桶，不同机器人的上下文永不互通
- **体积有界**：池子条数、单轮注入字符数、单条截断长度、时间窗四层上限
- **持久化**：流水存 KV，插件重载/重启不丢失
- 隐私指令内置于注入块：模型不得向用户透露其他会话的消息内容与来源

## 安装

在 AstrBot WebUI 插件市场搜索 `shared_context` 安装，或：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Galaxy1108/astrbot_plugin_shared_context
```

然后在 WebUI 插件管理点"重载插件"。

## 配置（WebUI 可视化）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enable_custom_groups` | `false` | 关闭时（默认）同一机器人下的所有会话共享全部上下文 |
| `share_groups` | `{}` | 多共享组：键为组名，值为该组的 `unified_msg_origin` 数组（值类型选 json）。仅开关开启时生效 |
| `max_messages` | `50` | 共享池保留的最大消息条数 |
| `max_chars` | `3000` | 每轮 LLM 请求注入的字符数上限 |
| `max_message_chars` | `200` | 单条消息记录时的截断长度 |
| `time_window_minutes` | `0` | 只注入最近 N 分钟内的消息，0 表示不限 |
| `include_bot_replies` | `true` | 是否记录并共享机器人回复 |
| `skip_command` | `true` | 跳过以 `/` 开头的指令消息 |

### 自定义共享组

1. 开启 `enable_custom_groups`
2. 在 `share_groups` 中添加组：键为组名，值为 `unified_msg_origin` 数组
3. 向机器人发送 `/shared_umo`，可查看当前会话的 `unified_msg_origin`（格式 `platform:type:session_id`，如 `aiocqhttp:private:123456`、`telegram:group:789`）

```json
{
  "工作群": ["aiocqhttp:group:123456", "telegram:group:789"],
  "好友": ["aiocqhttp:private:111", "telegram:private:222"]
}
```

规则：

- 会话可属于多个组；所属所有组的成员并集（排除自身）会被注入
- 不属于任何组的会话：不记录、不接收共享（闭组）
- 留空 `{}` 或关闭开关 = 仍然共享该机器人的所有会话

## 与 enhance_mode 等群聊插件共存

- 群内感知（同会话消息注入回本群）是 AstrBot 内置群聊上下文感知 / [astrbot_plugin_astrbot_enhance_mode](https://github.com/Axi404/astrbot_plugin_astrbot_enhance_mode) 群聊历史增强的职责
- 本插件只注入**其他会话**的消息，注入时排除同会话记录，与上述插件不会重复注入
- 建议：启用 enhance_mode 群聊历史增强时，关闭 AstrBot 内置的群聊上下文感知功能，避免重复

## 注意

- 共享内容包含用户消息和（可选）机器人回复，机器人回复可能含用户私密信息，请按需关闭 `include_bot_replies`
- 每轮请求都会携带共享块，token 是固定开销，可用 `max_messages` / `max_chars` 控制
- 需要 AstrBot >= 4.9.2（插件 KV 存储）

## 常见问题

**Q: 我的两个机器人会不会串台？**
不会。消息池按 `self_id` 分桶，跨机器人共享在代码层面被禁止。

**Q: 群聊消息会被共享吗？**
会。群消息（含未唤醒机器人的闲聊）与机器人回复都会入池并共享给其他会话。开启"隔离会话"的群内成员之间也互相感知。

**Q: 改了配置要重启吗？**
配置在 WebUI 修改保存后，重载插件即可生效。
