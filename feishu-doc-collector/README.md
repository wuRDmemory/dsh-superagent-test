# 飞书每日文档盘点 / Feishu daily document digest

每天 09:00 扫描前一天的飞书群聊/私聊消息，把其中分享的飞书文档、表格、多维表格、文件和 PDF/Office 附件提取出来，按「项目 → 部门 → 文档类型」分类，写入一个飞书多维表格（Base），完成后给指定会话发一条摘要。

The user-facing flow: at 09:00 the job scans yesterday's chats, classifies each document into the Base, and sends a digest with the document titles and a link to the Base.

## 文件

| 文件 | 作用 |
|---|---|
| `daily_docs.py` | 主流程：扫描会话 → 提取文档 → 分类 → 去重 → 写入 Base → 发消息 |
| `feishu_scan_lib.py` | lark-cli 封装、会话/消息分页、消息内容解析 |
| `run_daily.sh` | cron 包装：切换 lark-cli 身份、运行、恢复身份 |
| `config.example.json` | 配置模板，复制为 `config.json` 后填写 |
| `.gitignore` | 忽略配置、状态、缓存和日志 |

不包含任何用户数据、Base token、群 ID 或文档内容；这些只放在本地的 `config.json` / `seen.json` / `chats.json`。

## 前置条件

- Python 3.8+，无第三方依赖。
- 已安装并配置 `lark-cli`，且能以**用户身份**访问飞书。
- 一个飞书多维表格（Base），表结构建议如下（字段名可调整，脚本按中文名写入）：
  - `文档标题`（文本）
  - `项目`（单选；选项由 `config.json#projects` 决定）
  - `部门`（单选；脚本会写入硬件部/软件部/测试部等）
  - `文档类型`（单选：会议纪要、测试文档、问题总结、认证/安规、方案/设计、需求文档、项目计划/汇报、流程/制度、人事/招聘、知识/资料、其他/待归类）
  - `格式`（单选：飞书文档、飞书表格、PDF、Excel、Word、PPT 等）
  - `链接`（文本，URL 样式）
  - `来源`（单选：群聊/私聊/话题群）
  - `来源群/人`（文本）
  - `最近出现`（日期）
  - `分享次数`（数字）
  - `状态`（单选：待确认/已确认/已归档）
  - `标签`（多选）
  - `备注`（文本）

## 授权

`daily_docs.py` 需要用户身份读取消息和写 Base。可用的最小 scope 集合（按飞书应用实际可用为准）：

```text
im:chat:read
im:message:readonly
im:message.group_msg:get_as_user
im:message.p2p_msg:get_as_user
base:app:create
base:table:create
base:table:update
base:table:delete
base:record:create
base:record:update
base:record:delete
base:record:retrieve
base:field:create
base:field:update
base:field:delete
base:field:read
base:view:read
base:view:write_only
drive:drive:readonly
drive:drive.metadata:readonly
docx:document:readonly
wiki:node:read
search:docs:read
offline_access
```

用 lark-cli 完成一次 device-flow 登录：

```bash
lark-cli auth login --scope "<把上面的 scope 用空格连接>" --no-wait --json
# 把 verification_url 发给用户确认，然后：
lark-cli auth login --device-code <device_code> --json
```

`offline_access` 用于刷新 token。refresh token 过期后（默认约 7 天）需要重新授权；脚本失败时会尝试发消息提示。

## 配置

```bash
cp config.example.json config.json
# 填写 base_token、table_id、base_url、chat_id
# projects 用来把标题/群名中的关键词映射到「项目」单选值
```

`config.json` 已在 `.gitignore` 中，不要提交。

## 运行

```bash
# 试运行：只扫描和打印摘要，不写 Base、不发消息
python3 daily_docs.py --date 2026-11-02 --dry-run

# 正式运行：默认扫描昨天
python3 daily_docs.py

# 限制会话数（调试用）
python3 daily_docs.py --date 2026-11-02 --limit-chats 20 --dry-run
```

`run_daily.sh` 会自动切换 lark-cli strict mode 到 `off`（让用户身份可用），运行结束后恢复为 `FEISHU_DAILY_RESTORE_MODE`（默认 `bot`）。如果你的环境不用 strict mode，设置 `FEISHU_DAILY_MANAGE_STRICT_MODE=0`。

## cron

```cron
CRON_TZ=Asia/Shanghai
0 9 * * * /path/to/feishu-doc-collector/run_daily.sh >> /path/to/feishu-doc-collector/cron.log 2>&1
```

示例中的路径替换为实际目录；脚本默认把 `seen.json`、`chats.json` 放在同目录。

## 去重

- 同一条消息/同一天内：按文档 token 或文件 key 去重，同一文档在多个群出现只计一条，`分享次数` 累加。
- 跨天：`seen.json` 记录已经进入 Base 的文档 key；后续扫描会跳过，避免重复插入。
- 限制：同一内容另存为新文档或重新上传成新文件时 token/key 会变，会被视为不同文档。如需内容级去重，可在 `daily_docs.py` 中加标题/文件哈希规则。

## 分类

分类是规则初分：

- 文档类型：按标题/群名中的关键词判断（会议纪要、测试文档、问题总结、认证/安规、方案/设计、需求文档、项目计划/汇报、流程/制度、人事/招聘、知识/资料、其他）。
- 项目：由 `config.json#projects` 的关键词映射，未命中用 `default_project`。
- 部门：按标题/群名关键词映射到硬件部/软件部/测试部/结构部/算法部/项目部/供应链部/人力资源等。

分类会随标题措辞变化，建议在 Base 里人工复核；也可以直接调整 `daily_docs.py` 中的关键词表。

## 排除规则

默认排除：

- 财务/商务、数据/报表类文档（发票、付款、预算、合同、总表、台账、盘点等）；
- 简历/候选人/应届生/实习类文件，以及面试/招聘群里的 PDF/Word 附件。

这些规则在 `daily_docs.py` 的 `excluded()` 中，可按团队习惯调整。

## 限制

- 只读取消息中出现的文档链接和文件附件，不抓取文档正文。
- 会话列表有本地缓存（默认 7 天刷新），新建会话最多延迟 7 天进入扫描范围。
- 每个会话最多翻 20 页（约 1000 条消息）；昨日超大会话可能截断。
- lark-cli 的 token 刷新依赖 `offline_access`；refresh token 过期后需重新授权。
- 脚本默认假设 lark-cli 的 strict mode 为 `bot`；如不同，修改 `FEISHU_DAILY_RESTORE_MODE` 或关闭管理开关。
