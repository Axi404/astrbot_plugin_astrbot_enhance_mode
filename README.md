# AstrBot Enhance Mode

**Version**: `v0.2.4`  
**Author**: `阿汐`

`astrbot_plugin_astrbot_enhance_mode` 是 AstrBot 的群聊增强插件，提供 React 群聊上下文、主动回复、标签解析、封禁控制、Memory RAG、网页浏览与可视化 WebUI。

## Update Notes (v0.2.4)

- 主动回复 `model_choice` 新增独立模型配置：`active_reply.model_choice_provider_id`。
- 当配置的 `model_choice_provider_id` 无效时，自动回退到当前会话默认 Provider，并输出告警日志。
- 配置 schema 与文档已同步，WebUI 可直接选择该 Provider。
- 新增联网搜索工具 `grok_web_search`（可在 `web_search` 配置分组中启用并指定专用 provider，不跟随当前会话 provider）。
- `grok_web_search` 改为直连 provider 的 `api_base/key/model` 发起请求，并支持 `request_mode` 与 `base_url_override` 配置。
- 新增基于 `agent-browser` 的网页浏览工具组，可在 `browser_tool` 配置分组中启用。

## Design Philosophy

本插件把 Bot 设计为“具有人格与边界的行动主体”，目标不是把 Bot 做成被动问答器，而是让它拥有连续、可执行的一套互动生活。

1. 回复（Reply）
- Bot 决定何时说话、如何说话，并通过 `<mention/>`、`<quote/>` 表达互动意图。

2. 拒绝回复（Refuse）
- Bot 可以通过 `<refuse/>` 主动不发送本轮回复。
- 这是行为边界，不是异常状态。

3. 忽略某人消息（Ban）
- 这里的 `ban` 是 Bot 侧的“忽略/不处理”策略，不是平台管理员禁言。
- 不要求 Bot 拥有群管理权限，也不会对平台侧用户状态做修改。

4. 记忆与回忆（Memory RAG）
- Bot 可以写入经历、按条件检索回忆，并在跨会话/跨群场景维持人格连续性。
- `ignore_group_id=true` 用于跨群读取，服务于“同一人格的一体化记忆”。

这四类能力共同构成了 Bot 的完整生命周期：表达、克制、选择性互动、持续成长。

## Features

### Group Chat Enhancement

- React 模式（群聊上下文增强总开关）
- 群聊历史增强（可注入发送者 ID、角色标签、消息编号）
- 图片转述（可选，默认记录 `[Image]`，由工具按需生成描述并回填历史）
- 角色显示（在 system reminder 注入 `admin/member`）

### Active Reply

- `probability` 概率触发
- `model_choice` 模型判定触发（支持人格面具占位符）
- 白名单控制（按 `unified_msg_origin` 或群号）

### Output Tags

- `<mention id="..."/>`：转为平台 At 组件
- `<quote id="..."/>`：转为平台引用组件
- `<refuse/>`：触发拒绝发送，清空结果链

### Ban Control

- 运行时拦截被封禁用户消息
- LLM Tools:
  - `enhance_get_ban_list_status`
  - `enhance_ban_user`
  - `enhance_unban_user`

### Memory RAG

- LLM Tools:
  - `grok_web_search`
  - `enhance_use_image`
  - `enhance_memory_rag_write`
  - `enhance_memory_rag_read`
- Embedding Provider 独立配置（不是聊天模型 Provider）
- 时间显示与时间解析统一使用 AstrBot 全局 `timezone`（默认 `Asia/Shanghai`）
- 按角色、时间、群范围过滤
- 支持 `ignore_group_id=true` 跨群读取

### Browser Tool

- LLM Tools:
  - `enhance_browser_open`
  - `enhance_browser_snapshot`
  - `enhance_browser_action`
  - `enhance_browser_wait`
  - `enhance_browser_close`
- 基于 `agent-browser`，支持真实网页打开、DOM/可访问性快照、点击、填写、等待与读取
- 默认按 `unified_msg_origin` 隔离浏览器 session，可选自动持久化 cookies/localStorage

### Memory RAG WebUI

- 独立 HTTP 服务
- 登录认证（支持固定密码或启动时随机密码）
- 统计、筛选、分页、详情、删除
- Cleanup：将旧记录规范化为新时间元数据并回写存储
- 管理命令：`/enhance rag-webui`
- 依赖：`fastapi`、`uvicorn`（已在 `requirements.txt` 声明，插件加载时自动安装）

## Installation

1. 将插件目录放到 `data/plugins/`
2. 重启 AstrBot
3. 在插件配置页面启用需要的能力

如果你要启用网页浏览工具，还需要在宿主机额外安装 `agent-browser`：

```bash
npm install -g agent-browser
agent-browser install
```

## Recommended Builtin Settings

为避免能力重叠，建议：

- 关闭内置群聊上下文：`group_icl_enable`
- 关闭内置主动回复：`active_reply.enable`
- 关闭内置引用回复：`reply_with_quote`
- 保持内置识别开启：`identifier`

## Configuration

配置分组（键名与 `_conf_schema.json` 一致）：

- `group_features`
- `group_history_enhancement`
- `active_reply`
- `web_search`
- `browser_tool`
- `memory_rag`
- `memory_rag_webui`
- `global_settings`

### `group_features`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `react_mode_enable` | bool | `false` | React 模式总开关，群历史增强和主动回复都依赖它 |
| `role_display` | bool | `true` | 注入用户角色（admin/member） |
| `mention_parse` | bool | `true` | 解析 `<mention/>` 与 `<quote/>` |
| `ban_control_enable` | bool | `true` | 启用封禁工具和运行时拦截 |
| `ban_max_duration_sec` | int | `2592000` | 单次封禁时长上限（秒） |
| `ban_allow_admin` | bool | `false` | 是否允许封禁管理员 |

### `group_history_enhancement`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用群聊历史增强 |
| `max_messages` | int | `300` | 每个会话保留的历史条数 |
| `include_sender_id` | bool | `true` | 历史中包含发送者 ID |
| `include_role_tag` | bool | `true` | 历史中包含角色标签 |
| `image_caption` | bool | `false` | 启用图片描述能力与按需转述工具（历史默认仍记录为 `[Image]`） |
| `image_caption_provider_id` | string | `""` | 图片转述提供商 ID，空则默认 |
| `image_caption_prompt` | string | `"Please describe the image using Chinese."` | 图片转述提示词 |

### `active_reply`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用主动回复 |
| `mode` | string | `"probability"` | `probability` 或 `model_choice` |
| `possibility` | float | `0.1` | 概率触发时生效 |
| `model_stack_size` | int | `8` | `model_choice` 栈长度 |
| `model_history_messages` | int | `0` | `model_choice` 额外历史条数 |
| `model_choice_provider_id` | string | `""` | `model_choice` 判定模型的提供商 ID，空则使用当前会话默认提供商 |
| `model_choice_prompt` | string | schema 默认值 | 判定提示词，支持占位符 |
| `whitelist` | string | `""` | 逗号分隔来源/群号白名单 |

### `memory_rag`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用 Memory RAG 工具 |
| `embedding_provider_id` | string | `""` | Embedding Provider ID，空则自动选择第一个可用 embedding provider |
| `default_recall_k` | int | `20` | 默认语义召回条数 |
| `max_return_results` | int | `200` | 单次读取返回上限 |

### `web_search`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用 `grok_web_search` 工具 |
| `provider_id` | string | `""` | 专用于联网搜索的 provider ID（必填，不会回退到当前会话 provider） |
| `system_prompt` | string | schema 默认值 | 搜索系统提示词（默认要求返回 JSON） |
| `timeout_sec` | float | `60` | 单次搜索调用超时秒数 |
| `request_mode` | string | `"auto"` | `auto/responses/chat_completions`；`auto` 会先尝试 `responses`，失败再回退 `chat_completions` |
| `base_url_override` | string | `""` | 可选覆盖请求 Base URL；留空使用 provider 的 `api_base` |
| `show_sources` | bool | `false` | 是否在工具结果中输出来源 |
| `max_sources` | int | `5` | 来源输出上限，`0` 表示不限制 |

### `browser_tool`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用 `agent-browser` 包装工具 |
| `command` | string | `"agent-browser"` | CLI 命令；也可写成 `npx agent-browser` |
| `timeout_sec` | float | `60` | Python 包装层单次命令总超时 |
| `headed` | bool | `false` | 是否显示真实浏览器窗口 |
| `persist_session` | bool | `true` | 是否按聊天来源自动保存/恢复 cookies 与 localStorage |
| `content_boundaries` | bool | `true` | 是否启用输出内容边界，降低 prompt injection 风险 |
| `max_output_chars` | int | `12000` | 传给 `agent-browser --max-output`；`0` 表示不限制 |
| `allowed_domains` | string | `""` | 逗号分隔的允许域名模式 |
| `idle_timeout_ms` | int | `300000` | 浏览器空闲自动关闭时间；`0` 表示不设置 |

### `memory_rag_webui`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用 WebUI 服务 |
| `host` | string | `127.0.0.1` | 监听地址 |
| `port` | int | `8899` | 监听端口 |
| `access_password` | string | `""` | 登录密码，空则自动生成并写日志 |
| `session_timeout` | int | `3600` | 会话超时（秒） |

### `global_settings`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `lru_cache.max_origins` | int | `500` | 最大来源缓存数 |
| `timeouts.image_caption_sec` | float | `45` | 图片转述超时（秒） |
| `timeouts.model_choice_sec` | float | `45` | 模型判定超时（秒） |

## Usage

### Output Tags

```text
<mention id="user_id"/>
<quote id="msg_id"/>
<refuse/>
```

### WebUI Command

```text
/enhance rag-webui
```

## LLM Tools

### Ban Tools

1. `enhance_get_ban_list_status(user_id="", max_results=20)`
2. `enhance_ban_user(user_id, duration="10m")`
3. `enhance_unban_user(user_id)`

`duration` 支持 `s/m/h/d`。

### Web Search Tool

#### `grok_web_search`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `query` | string | Yes | - | 实时联网检索查询文本 |

### Browser Tools

#### `enhance_browser_open`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `url` | string | Yes | - | 要打开的绝对 URL |

#### `enhance_browser_snapshot`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `interactive_refs` | bool | No | `true` | 是否返回类似 `@e2` 的交互 ref，建议保持开启 |

#### `enhance_browser_action`

推荐流程：
1. 先 `enhance_browser_open(url)`
2. 再 `enhance_browser_snapshot()` 读取 ref
3. 然后对 ref 执行 `click/fill/type/...`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `action` | string | Yes | - | `click` / `fill` / `type` / `press` / `get_text` / `get_html` / `get_value` / `get_title` / `get_url` / `scroll` |
| `target` | string | No | `""` | 选择器或 snapshot ref，例如 `@e2`；`press` 时填按键；`scroll` 时填方向 |
| `value` | string | No | `""` | `fill/type` 的文本，或 `scroll` 的像素数 |

#### `enhance_browser_wait`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `mode` | string | No | `"load"` | `load` / `selector` / `text` / `url` / `time` |
| `target` | string | No | `"networkidle"` | mode 对应的等待目标 |
| `state` | string | No | `"visible"` | 仅 `selector` 模式使用：`visible` / `hidden` / `attached` / `detached` |

#### `enhance_browser_close`

无参数。关闭当前聊天来源对应的浏览器 session。

### Memory RAG Tools

#### `enhance_use_image`

统一图片工具：默认同时执行两件事：
1. 将目标图片作为真实图像输入附加到当前推理上下文（多模态模型可直接看图）
2. 生成图片描述并回填历史中的 `[Image] -> [Image: ...]`

你也可以按参数关闭其中一项：
- `attach_to_model=false`：仅回填历史（不塞图）
- `write_to_history=false`：仅塞图（返回描述，不写历史）

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `message_id` | string | Yes | - | 目标图片所在消息 ID（对应历史中的 `#msg...`） |
| `image_index` | int | No | `1` | 第几张图片（从 `1` 开始） |
| `attach_to_model` | bool | No | `true` | 是否将图片塞入本轮模型上下文 |
| `write_to_history` | bool | No | `true` | 是否将描述写回历史 `[Image]` |
| `prompt` | string | No | `""` | 本次调用覆盖默认图片描述提示词 |

#### `enhance_memory_rag_write`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `content` | string | Yes | - | 记忆文本 |
| `related_role_ids` | string | Yes | - | 角色 ID（JSON 数组字符串或逗号分隔） |
| `memory_time` | string | No | `""` | Unix/ISO 时间 |
| `group_scope` | string | No | `""` | 完整群范围，如 `default:123456` |
| `group_id` | string | No | `""` | 群号 |
| `platform_id` | string | No | `""` | 平台 ID |
| `extra_metadata_json` | string | No | `"{}"` | 额外元数据 JSON |

#### `enhance_memory_rag_read`

| Param | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `query` | string | No | `""` | 检索词 |
| `related_role_ids` | string | No | `""` | 角色 ID（JSON 数组字符串或逗号分隔） |
| `role_match_mode` | string | No | `"any"` | `any` / `all` |
| `start_time` | string | No | `""` | 开始时间（Unix/ISO） |
| `end_time` | string | No | `""` | 结束时间（Unix/ISO） |
| `group_scope` | string | No | `""` | 完整群范围 |
| `group_id` | string | No | `""` | 群号 |
| `platform_id` | string | No | `""` | 平台 ID |
| `sort_by` | string | No | `"relevance"` | `relevance` / `time` |
| `sort_order` | string | No | `"desc"` | `desc` / `asc` |
| `max_results` | int | No | `10` | 请求返回条数 |
| `embedding_recall_k` | int | No | `0` | `<=0` 时回退 `memory_rag.default_recall_k` |
| `ignore_group_id` | bool | No | `false` | `true` 时不自动套当前群范围，可跨群读取 |

## Memory RAG Behavior

1. `query` 为空时不生成查询向量，按时间排序返回。
2. `embedding_recall_k <= 0` 时使用 `memory_rag.default_recall_k`（默认 `20`）。
3. 最终返回条数受 `memory_rag.max_return_results`（默认 `200`）裁剪。
4. `ignore_group_id=false` 时自动注入当前会话群范围；`true` 时可跨群读取。
5. `role_match_mode=all` 时必须包含全部角色；`any` 时命中任一角色。

跨群读取示例：

```json
{
  "query": "日记",
  "related_role_ids": "[\"3406402603\"]",
  "sort_by": "time",
  "sort_order": "desc",
  "ignore_group_id": true,
  "max_results": 10
}
```

## WebUI API

基础路由：

- `GET /`
- `GET /api/health`
- `POST /api/login`
- `POST /api/logout`
- `GET /api/stats`
- `POST /api/cleanup`
- `GET /api/memories`
- `GET /api/memories/{memory_id}`
- `DELETE /api/memories/{memory_id}`

## Data Storage

插件数据目录：

```text
data/plugin_data/astrbot_plugin_astrbot_enhance_mode/
```

数据库文件：

- `ban_list.db`
- `memory_rag.db`

## Project Structure

```text
astrbot_plugin_astrbot_enhance_mode/
├── main.py
├── plugin_config.py
├── runtime_state.py
├── tag_utils.py
├── ban_control.py
├── memory_rag_store.py
├── requirements.txt
├── webui/
│   ├── __init__.py
│   └── server.py
├── static/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── _conf_schema.json
├── metadata.yaml
└── README.md
```

## Development

在插件目录执行：

```bash
ruff format .
ruff check .
```
