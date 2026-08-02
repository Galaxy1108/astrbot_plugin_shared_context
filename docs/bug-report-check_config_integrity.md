# [Bug] check_config_integrity 会清空插件配置中 dict 类型键的用户内容

> 面向 AstrBotDevs/AstrBot 的 issue 草稿（未发布），2026-08-02 实测于 AstrBot 4.27.0。

## What happened / 发生了什么

AstrBot 对插件 `_conf_schema.json` 中 `"type": "dict"` 的配置项存在数据丢失缺陷：用户在 WebUI（ObjectEditor 键值对编辑器）保存的键值对，在插件重载/重启后全部丢失，配置文件里该键被重置为空对象 `{}`。

根因在 `astrbot/core/config/astrbot_config.py` 的 `check_config_integrity()`：

1. 插件配置由 `star_manager.py` 以 schema 构造 `AstrBotConfig(..., schema=_conf_schema)`，`__init__` 里 `default_config = self._config_schema_to_default_config(schema)`——dict 类型键的参考默认值是 `{}`
2. 每次加载执行 `check_config_integrity(default_config, conf)`，对参考值为 dict 的键递归进入：

   ```python
   elif isinstance(value, dict):
       if not isinstance(conf[key], dict):
           new_conf[key] = value
           has_new = True
       else:
           child_has_new = self.check_config_integrity(value, conf[key], ...)
           new_conf[key] = conf[key]
           has_new |= child_has_new
   ```

3. 递归层 refer_conf = `{}`，用户添加的键被视为"参考配置中没有的配置项"（仅打印 `Config key removed`），最终：

   ```python
   conf.clear()
   conf.update(new_conf)   # new_conf 为空 → 用户内容被清空
   ```

4. `has_new=True` 时 `__init__` 还会 `save_config()` 把清空结果写回磁盘

即：任何插件使用 `dict` 类型存用户动态键值对，内容必然在加载/保存时被抹掉。前端 ObjectEditor 明确支持 dict 编辑（"修改键值对"弹窗、JSON 值类型校验），与后端行为矛盾。

核心配置不受影响的原因：核心的用户可编辑 dict（如 `custom_headers`）位于 `provider_sources`（list 类型）的元素内部，`check_config_integrity` 不递归 list，整包保留——这恰好提供了可用的规避模式。

## Reproduce / 如何复现？

最小复现（逻辑与部署版 4.27.0 逐行一致）：

```python
def check_config_integrity(refer_conf, conf, path=""):
    has_new = False
    new_conf = {}
    for key, value in refer_conf.items():
        if key not in conf:
            new_conf[key] = value; has_new = True
        elif conf[key] is None:
            new_conf[key] = value; has_new = True
        elif isinstance(value, dict):
            if not isinstance(conf[key], dict):
                new_conf[key] = value; has_new = True
            else:
                child = check_config_integrity(value, conf[key], path + "." + key if path else key)
                new_conf[key] = conf[key]; has_new |= child
        else:
            new_conf[key] = conf[key]
    for key in list(conf.keys()):
        if key not in refer_conf:
            print("Config key removed:", path + "." + key if path else key); has_new = True
    conf.clear(); conf.update(new_conf)
    return has_new

# 插件 _conf_schema.json: {"custom": {"type": "dict", "default": {}}}
refer = {"custom": {}}                       # schema 生成的参考默认值
conf  = {"custom": {"好友": ["umo1"]}}        # 用户在 WebUI 保存的内容
check_config_integrity(refer, conf)
print(conf)                                  # → {'custom': {}}  ← 内容被清空
```

复现步骤：

1. 插件 `_conf_schema.json` 定义 `"my_config": {"type": "dict", "default": {}}`
2. WebUI 插件配置中向 `my_config` 添加键值对并保存（保存瞬间文件里是有内容的）
3. 重载插件或重启 AstrBot
4. 配置文件中 `my_config` 变为 `{}`，WebUI 里内容消失，日志出现 `Config key removed: <插件名>.my_config.<键名>`

对照实验：若把同样内容放在 list 类型键的列表元素内部（核心 `provider_sources` 的写法），则不会丢失——证明问题只出在 dict 类型的递归清理。

## AstrBot version, deployment method

- AstrBot 4.27.0，Windows（阿里云 ECS）uv 工具安装部署（`uv tool install astrbot` + `astrbot run`）
- 提供商：deepseek；消息平台：aiocqhttp

## OS

Windows Server（OpenSSH 远程接入，PowerShell）

## Logs / 报错日志

完整 Debug 日志（360 行窗口，含插件更新、配置迁移、两次重载清除"汐月"组的全过程）见 gist：
https://gist.github.com/Galaxy1108/3e42f45c233af514c28cab180d391f3f

关键片段（插件每次重载时重复出现）：

```text
[2026-08-02 21:22:44.117] [Core] [INFO] [star.star_manager:1123]: Loading plugin astrbot_plugin_shared_context ...
[2026-08-02 21:22:44.147] [Core] [INFO] [config.astrbot_config:215]: Config key removed: custom_groups.share_groups.汐月
[2026-08-02 21:22:44.147] [Core] [INFO] [config.astrbot_config:221]: Config key order fixed: custom_groups.share_groups
[2026-08-02 21:23:34.243] [Core] [INFO] [star.star_manager:1123]: Loading plugin astrbot_plugin_shared_context ...
[2026-08-02 21:23:34.248] [Core] [INFO] [config.astrbot_config:215]: Config key removed: custom_groups.share_groups.汐月
[2026-08-02 21:23:34.248] [Core] [INFO] [config.astrbot_config:221]: Config key order fixed: custom_groups.share_groups
```

其中 `Config key removed: custom_groups.share_groups.汐月` 即用户在 WebUI 保存的组"汐月"被 `check_config_integrity` 清除，每次重载都会重复发生。

用户保存后、重载前的配置文件内容：

```json
{
  "custom_groups": {
    "enable_custom_groups": true,
    "cross_bot_share": true,
    "share_groups": { "汐月": ["qq-bot:FriendMessage:xxx"] }
  }
}
```

重载后被写回：

```json
{
  "custom_groups": {
    "enable_custom_groups": true,
    "cross_bot_share": true,
    "share_groups": {}
  }
}
```

## Are you willing to submit a PR?

Yes! 建议修复方向：`check_config_integrity` 对无 `items` 的 dict 类型键跳过递归（与 list 一致整包保留），或当 schema 为 dict 提供 `items` 时按 items 校验。

## 相关调研

- #8298「cmd_config.json 莫名被清空」（closed）：根因是 `save_config` 非原子写入，已被 PR #8793 修复——与本 issue 根因不同
- #7836「设置添加管理员 id，重启之后无法保存」（open）：现象相似但字段为 list 类型，根因待确认
- 上游 `origin/master` 的 `check_config_integrity` 逻辑与 4.27.0 一致，尚未修复
