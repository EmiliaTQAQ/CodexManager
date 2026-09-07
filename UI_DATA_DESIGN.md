# Codex 模型管理窗口 — MVP 界面线框 & 数据结构设计（开发准备）

> 范围：纯管理窗口（无聊天）。目标窗口约 **960 × 640**，可缩放，最小 800×560。

---

## 1. 窗口布局总览

```
┌──────────────────────────────────────────────────────────────────────┐
│  ⓘ 状态栏(高 44)                                                     │
│   ● 当前: gpt-5.6-sol  @  apex_gpt        [打开 config] [回滚上一步]   │
├───────────────────────────────┬──────────────────────────────────────┤
│  Provider 列表 (宽 280)       │  详情 & 模型编辑区 (自适应)           │
│  ┌─────────────────────────┐  │  ┌────────────────────────────────┐  │
│  │ 🔍 搜索        [+ 新建] │  │  │  Provider 基本信息              │  │
│  ├─────────────────────────┤  │  │  名称   [apex_gpt        ]      │  │
│  │ ● deepseek              │  │  │  Base   [https://apexap…/v1 ]  │  │
│  │ ● apex_gpt  ●启用中  ✓  │  │  │  接口   (•)responses ( )chat    │  │
│  │ ○ apex_resp(旧)    ✗    │  │  │  Key    [sk-e371…82 掩码] 覆盖  │  │
│  │ ○ (空状态: 引导新建)     │  │  │  默认模型 [gpt-5.6-sol     ▾]   │  │
│  └─────────────────────────┘  │  └────────────────────────────────┘  │
│                               │  ┌────────────────────────────────┐  │
│  每项右侧角标:                 │  │  模型列表 (写 models.json)      │  │
│  ✓=启用中  ✗=上次验证失败      │  │  [⇅ 从 Provider 拉取 /v1/models] │  │
│                               │  │  ☑ gpt-5.6-sol  128K  high     │  │
│                               │  │  ☑ gpt-5.4      128K  high     │  │
│                               │  │  ☐ gpt-5.2      128K  high     │  │
│                               │  │  ☐ gpt-5.6-luna …(可编辑行)    │  │
│                               │  └────────────────────────────────┘  │
│                               │  ┌────────────────────────────────┐  │
│                               │  │  [+ 添加模型]                  │  │
│                               │  └────────────────────────────────┘  │
│                               │  ┌────────────────────────────────┐  │
│                               │  │  [启用此 Provider] [保存] [删除]│  │
│                               │  └────────────────────────────────┘  │
├───────────────────────────────┴──────────────────────────────────────┤
│  活动条(高 28): 空闲 | 切换中… | ✅ 已启用+验证通过 | ❌ 失败:原因 |      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 三个核心区域说明

### 2.1 状态栏（只读，唯一真相来源）
- 从 **真实 `~/.codex/config.toml`** 解析「当前 model + provider」，启动即读、切换后刷新；
- 「回滚上一步」：把上一份自动备份还原回去（禁用条件：无备份时置灰）；
- 数据源与后端 `status_snapshot()` 对齐。

### 2.2 Provider 列表（左栏）
- 行为：点击 = 选中并在右侧载入详情；双击 = 直接「启用」；
- 角标：`● 启用中`（当前生效）、`✓ 上次验证通过`、`✗ 上次验证失败`、空角标 = 从未启用/未验证；
- 操作：搜索框过滤 + 右上 `+ 新建`；删除需二次确认（且不允许删除"启用中"的那一个）。

### 2.3 详情 & 模型编辑区（右栏）
- **基本信息**：名称 / base_url / wire_api 单选 / API key / 默认模型下拉；
- **模型列表**：勾选 = 会写进 `models.json`（决定 Codex 下拉可见）；未勾选仅存在于本 Provider 配置；
  - 「从 Provider 拉取」：调 `/v1/models` 后**合并**进列表（已存在的不覆盖用户改名）；
  - 每行可点开编辑 `slug / display_name / context_window / reasoning 级别`；
- **主按钮**：`启用此 Provider`（写 config 并验证）、`保存`（只存本地 profile）、`删除`。

---

## 3. Provider 数据结构（本地存储）

本地数据文件：`~/Library/Application Support/CodexManager/providers.json`，权限 **0600**。
（Key 明文存于此文件但界面只显示掩码；后续可升级到 macOS Keychain —— v1.1 项。）

```jsonc
// providers.json — 顶层结构
{
  "version": 1,
  "active_provider_id": "apex_gpt",   // 记录"启用中"，供启动时高亮
  "providers": [ /* Provider[]，见下方示例 */ ]
}
```

### Provider 对象字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 唯一 slug，同时用作 `config.toml` 的 `[model_providers.<id>]` 段名 |
| `name` | string | ✅ | 界面显示名 |
| `base_url` | string | ✅ | 形如 `https://…/v1` |
| `wire_api` | `"responses"\|"chat"` | ✅ | 写进 config（注意：新版 Codex 只认 responses，chat 仅兼容旧版） |
| `api_key` | string | ✅ | 仅追加/覆盖写入；读取接口永远返回掩码 |
| `default_model` | string | ✅ | 启用时写入 config 顶部的 `model =` |
| `enabled_models` | string[] | ✅ | 勾选集；启用时决定写入 `models.json` 的模型 |
| `model_catalog` | ModelMeta[] | ⬜ | 可选详细元数据；为空则按最小模板生成 |
| `created_at` / `updated_at` | string | ⬜ | ISO 时间 |
| `last_check` | object | ⬜ | `{ ok, error, at }` 上次验证结果（驱动左栏 ✓/✗ 角标） |

### ModelMeta 字段（对应 models.json 条目）
`slug`(模型名) `display_name` `context_window` `max_context_window`
`default_reasoning_level` `supported_reasoning_levels` `supports_reasoning_summaries`

---

## 4. 启用 Provider 的写盘行为（后端契约）

点「启用此 Provider」时，后端按固定顺序做（**任一步失败即中止且自动还原**）：
1. 备份：`config.toml`、`auth.json`、`models.json` → 加时间戳副本；
2. 写 `config.toml`：顶部 `model=default_model`、`model_provider=id` + `[model_providers.<id>]` 块（含 key）；
3. 写 `models.json`：清空后仅含该 Provider `enabled_models` 对应目录（MVP 单选互斥，不合并）；
4. 验证：轻量调 `/v1/models` → 成功回填 `last_check={ok}`，失败回滚并回填错误；
5. 落库 `active_provider_id`，通知 UI 刷新状态栏与角标。

> 复用现有 `server.py`：`load_parsed/build_config/apply_config`（写 config）、
> `write_auth`、`backup`、`fetch_json + extract_model_ids`（拉模型/验证）、`status_snapshot`（状态栏）。

---

## 5. 空状态 & 边界（MVP 需覆盖）
- 无任何 Provider → 右栏全灰 + 引导文案「点击 + 新建」；
- base_url 填错 / key 无效 → 「拉取」与「启用」给出明确红✗原因，不写坏现有配置；
- 已启用项不允许删除；回滚按钮在无备份时禁用；
- 同一 Provider 的 `enabled_models` 为空时禁止启用（提示：至少勾选一个默认模型）。
