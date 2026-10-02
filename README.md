# zcode-import-session

把**另一台机器备份的 ZCode 会话**导入本机 ZCode 的命令行工具。单文件 Python 脚本，仅依赖标准库（Python 3.8+）。

适用场景：换电脑、重装系统、多机同步——把旧机器的 `~/.zcode` 目录拷过来，用本脚本把其中的会话合并进本机 ZCode，并可选择把会话里的旧工作目录路径改写成本机路径。

> 逆向整理自 ZCode Desktop 0.16.x（schema migration 0018）的实际存储结构，并在真实迁移中完整验证。
> ZCode 版本更新后存储结构若变化，脚本按列交集合并、不存在的表自动跳过，具备一定容错；schema 比本机新时会拒绝执行。

## 快速开始

```bash
# 1. 看看备份里有哪些会话
python zcode-import-sessions.py <备份目录> --list

# 2. 预演（不写任何文件）
python zcode-import-sessions.py <备份目录> --map "F:\old\path=D:\new\path" --dry-run

# 3. 正式导入
python zcode-import-sessions.py <备份目录> --map "F:\old\path=D:\new\path"
```

备份目录是旧机器的 `.zcode` 目录（或其拷贝），需至少包含：

```
<备份目录>/
├── cli/db/db.sqlite          # 会话主库（session / message / part 等）
└── v2/tasks-index.sqlite     # 任务索引（界面"会话"列表的数据源）
```

导入后：**完全退出 ZCode（托盘右键退出，仅关窗口是最小化）再启动**，然后在欢迎页"最近项目"中打开新目录——侧栏"项目"区显示的是**当前打开的工作区标签页**，不是"所有有会话的目录"，打开一次后其下即列出导入的会话。

### 示例：为多个不同目录的会话分别映射路径

备份里各会话的工作目录往往不相同。先用 `--list` 看每个会话的原始目录：

```text
$ python zcode-import-sessions.py E:\backup\zcode --list
备份 E:\backup\zcode 共 3 个会话:
  [2026-09-18] 周报与文档整理  (36 条消息)
      目录: F:\docs\research
  [2026-09-21] 爬虫脚本调试  (142 条消息)
      目录: F:\work\shop-app
  [2026-09-21] 商城后端重构  (2860 条消息)
      目录: F:\work\shop-app
```

然后**重复指定 `--map`**，一条映射一个旧目录；每个会话按自己记录的目录精确匹配各自的映射：

```bash
python zcode-import-sessions.py E:\backup\zcode \
  --map "F:\work\shop-app=D:\dev\shop-app" \
  --map "F:\docs\research=D:\docs\research"
```

`--dry-run` 的输出会直接展示每个会话映射后的落点，核对无误再去掉 `--dry-run` 正式执行：

```text
将导入的会话 (3 个):
  [2026-09-18] 周报与文档整理  (36 条消息)
      -> D:\docs\research
  [2026-09-21] 爬虫脚本调试  (142 条消息)
      -> D:\dev\shop-app
  [2026-09-21] 商城后端重构  (2860 条消息)
      -> D:\dev\shop-app
```

说明：映射与 `session.directory` **整串相等**才算匹配；没被任何映射覆盖的会话按原路径导入（预检时会列出并警告）；新旧路径都写完整绝对路径，Windows 路径整体加引号。

## 参数

| 参数 | 说明 |
|---|---|
| `<备份目录>` | 旧机器的 `.zcode` 目录或其拷贝 |
| `--map "旧=新"` | 工作目录映射，**可重复多次**，每个会话按自己记录的目录匹配各自的映射 |
| `--dry-run` | 只报告将做什么，不写任何文件（退出码 2） |
| `--list` | 仅列出备份中的会话（标题 / 目录 / 消息数），不导入 |
| `--skip-files` | 不复制 rollout 日志、artifacts、image-cache 附属文件 |
| `--home <dir>` | 本机 `.zcode` 目录，默认 `~/.zcode` |

`--map` 匹配规则：与 `session.directory` **整串相等**；多个映射按旧路径长度降序应用（前缀包含时先长后短）；不在任何映射中的会话按原路径导入（预检时会列出并警告）。建议先 `--list` 查看各会话的原始目录再决定映射。

## 脚本做了什么

1. **预检** — schema 迁移版本比对（备份比本机新则拒绝执行）、重复会话检测、映射目标存在性检查
2. **自动备份** — 用 SQLite backup API 对本机 `db.sqlite` / `tasks-index.sqlite` / `setting.json` 做一致性快照，存到 `~/.zcode/import-backups/<时间戳>/`，出错可整体还原
3. **合并会话库** — `session` / `session_entry` / `message` / `part` / `session_input` / `todo` / `session_target` / `tool_usage` / `turn_usage` / `model_usage` / `input_history` 共 11 张表，`INSERT OR IGNORE` 幂等合并，可重复运行
4. **合并任务索引** — `tasks` 表（主键含 `workspace_key`，因此路径翻译在 INSERT 阶段完成，重复运行不会产生旧键重复行，并自动清理历史残留）
5. **复制附属文件** — `cli/rollout/model-io-sess_*.jsonl`、`cli/artifacts/sess_*/`、`cli/image-cache/sess_*/`，已存在则跳过
6. **路径改写**（`--map` 时）— 覆盖同一路径的全部书写形式：
   - `F:\a\b`（明文）、`F:\\a\\b`（JSON 转义）、`F:\\\\a\\\\b`（嵌套 JSON，工具结果里常见，4/6/8 层都会处理）
   - `F:/a/b`（正斜杠）、`/f/a/b`（Git Bash / MSYS 风格，大小写盘符都处理）
   - 只匹配带盘符的**完整路径**，对话正文里单独出现的文件夹名（自然语言）不会误改
   - 同时更新 `session.directory/path`、`project_id`（按官方约定重新生成：`E:\work\demo` → `proj_e-work-demo`）、`tasks.workspace_key/path/meta_json/searchable_text`
7. **注册项目** — 把新目录加入 `~/.zcode/v2/setting.json` 的 `recentProjects`（排重、上限 10）
8. **验证** — 逐表行数对比、旧路径残留扫描、被修改 blob 的 JSON 有效性校验、`quick_check`、输出会话清单

## 已知边界（有意不迁移）

| 内容 | 原因 |
|---|---|
| `workflow_*` / `session_task_link` | 子代理工作流元数据，常规会话为空 |
| `v2/checkpoints` | 工作区快照，绑定原机器路径与加密密钥，跨机不可用 |
| `local_setting` / `permission` | 本机权限与设置，不适合跨机覆盖 |
| `cli/exec` | 运行时临时状态 |
| `session_input` 中 `runtime_command_N` 行 | id 为全局自增，跨机必然撞号，`INSERT OR IGNORE` 自动跳过；这些是后台命令通知账目，**不含对话内容**（对话在 `message`/`part` 表，完整导入） |

## ZCode 存储结构速查（逆向结论）

| 位置 | 作用 |
|---|---|
| `~/.zcode/cli/db/db.sqlite` | 会话主库：`session`（目录/标题/project_id）、`message`/`part`（对话内容，JSON blob）、usage 统计等 |
| `~/.zcode/v2/tasks-index.sqlite` | `tasks` 表 = 客户端"会话"列表数据源，主键 `(workspace_key, task_id)` |
| `~/.zcode/cli/rollout/` | 每会话一个 `model-io-sess_*.jsonl` 模型 IO 日志 |
| `~/.zcode/cli/artifacts/sess_*/` | 工具结果附件（图片、大输出） |
| `~/.zcode/v2/setting.json` | `recentProjects` = 欢迎页"最近项目"，**不等于**侧栏"项目"区 |
| 侧栏"项目"区 | = 当前打开的工作区标签页（workspacePurpose 区分 project/conversation） |

## 测试情况

在真实迁移（3 个会话、约 3.1 万行、含 4500 条消息的大会话）中验证：

- 与手工逐表导入的结果逐表对比：行数、内容完全一致
- 幂等性：重复运行全部为 0（无重复插入、无重复文件、无重复改写）
- 多映射：不同目录的会话各自落到正确的新路径，旧路径残留 0
- `--dry-run`：对真实数据零写入
- 路径改写后所有被修改 blob 均为合法 JSON，`quick_check` 通过

## 免责声明

本工具直接读写 ZCode 的本地数据库，**使用前请自行做好备份**（脚本也会自动快照到 `import-backups/`）。非官方工具，ZCode 版本升级后存储结构变化可能导致行为不符；如遇异常，用 `import-backups/` 下的快照覆盖回 `~/.zcode` 对应文件即可还原。
