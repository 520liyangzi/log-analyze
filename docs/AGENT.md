# 可选：外部 AI / CLI 只读查询

日常排查直接使用页面的 [原生 AI 对话](NATIVE_AI.md)。本指南面向需要自己运行外部 AI 或命令行脚本的用户；这些工具不依赖网页终端。v2.4 已移除旧 CMD 页面及其 API，LogScope 不再管理外部 AI 的进程、登录和 Session ID。

## 查询已经导入的日志

先启动 LogScope，上传 ZIP 并完成目录确认与索引。脚本查询已有索引，不负责上传、导入或启动服务。在源码目录运行：

```powershell
python skills/logscope/scripts/logscope.py rules
python skills/logscope/scripts/logscope.py datasets
python skills/logscope/scripts/logscope.py --dataset "替换为日志包ID" search --endpoint "/api/model/map" --access-only
python skills/logscope/scripts/logscope.py --dataset "替换为日志包ID" trace "9124859898865451127"
python skills/logscope/scripts/logscope.py context 123 --radius 15
python skills/logscope/scripts/logscope.py correlate 123 --seconds 30
python skills/logscope/scripts/logscope.py verify 123
```

将示例中的日志包 ID、日志记录 ID、接口和流水号替换为实际值。`datasets` 返回日志包 ID；搜索结果中的记录 ID 可用于 `context`、`correlate` 和 `verify`。EXE 内也包含该工具，例如：

```powershell
.\LogScope.exe --run-tool logscope datasets
.\LogScope.exe --run-tool logscope --dataset "替换为日志包ID" search --q "连接超时"
```

`--url` 和 `--dataset` 放在子命令前；查询条件放在子命令后。默认服务地址为 `http://127.0.0.1:8765`；此 CLI 只接受本机回环地址，应在运行 LogScope 的服务电脑上执行。更换端口时可传入 `--url http://127.0.0.1:8877`。也可使用 `LOGSCOPE_URL` 和 `LOGSCOPE_DATASET_ID` 环境变量，或在当前工作目录提供包含 `url`、`dataset` 的 `task.json`；显式命令行参数优先。

常用命令包括 `files`、`search`、`export`、`trace`、`record`、`context`、`verify`、`correlate`。使用 `search --help` 查看筛选项。JSON 响应保留来源、命中范围及分页信息；大量匹配需继续翻页或导出：

```powershell
python skills/logscope/scripts/logscope.py --dataset "替换为日志包ID" export --q "连接超时" --output matches.ndjson
```

导出路径已存在时拒绝覆盖。`--scan` 只扫描已导入文本，不能查找未导入的文件；`verify` 只核对原包解码文本与索引是否一致，不证明解析语义或根因；`correlate` 返回时间和线程等候选关系，不保证属于同一请求。报告应说明实际查阅范围，并区分日志事实与推测。

## 可选 Skill

Skill 仅帮助外部 AI 发现这些查询工具，不是页面对话的前置条件。先运行 LogScope 并导入日志，再按需要选择一个安装位置：

```powershell
python install_skill.py
python install_skill.py --project D:\your-project
python install_skill.py --target D:\company-agent\skills\logscope
```

默认安装到当前用户的 `~/.claude/skills/logscope`；`--project` 安装到指定项目的 `.claude/skills/logscope`；`--target` 指定最终 Skill 目录。目标已存在时拒绝覆盖，更新前先备份或移走旧目录。外部 AI 能否发现 Skill 由它自身的配置决定；也可以直接告诉它脚本位置和日志包 ID。

`rules` 命令读取页面保存的最新分析流程与业务规则，无需每次修改 SKILL.md。外部 AI 应在开始分析时明确读取规则，并用实际查询结果核实结论。它的对话和报告由该工具自行保存，不会自动导入 LogScope 原生会话。

## 可选的固定版本代码查询

页面代码定位直接使用原生对话的“项目代码”设置，会同步远程并固定 commit。独立 `project.py` 是另一个只读工具，**不会自行同步仓库，也不会由页面生成它的配置**。

如果确实需要在外部 AI 中调用它，先准备本机已有 Git 仓库及要分析的完整提交 SHA，在运行命令的目录创建 `code-task.json`：

```json
{
  "project_root": "D:\\project\\mate\\FMEMateService",
  "branch": "main",
  "commit": "替换为完整提交SHA"
}
```

确认 SHA 对应产生日志的部署版本；分支名称只作说明，查询固定到 `commit`，不包含未提交修改。在仓库源码目录运行以下示例时，`code-task.json` 也放在该工作目录；使用其他目录时把脚本路径改为绝对路径：

```powershell
python skills/logscope/scripts/project.py info
python skills/logscope/scripts/project.py grep "错误关键字" --path src --max 50
python skills/logscope/scripts/project.py show src/example.java --start 1 --end 120
```

EXE 可用 `LogScope.exe --run-tool project info` 调用同一工具。它提供 `info`、`tree`、`grep`、`show`、`log`，读取指定 commit 的 Git 对象；不切换分支、不运行项目代码，不执行构建或测试。使用时仍需本机可运行 Git。

## 旧网页终端数据

升级保留 `data/terminal-sessions/` 和 `data/terminal-config.json`，不自动删除旧文件。可以从磁盘直接查阅旧 `task.md`、`terminal.log` 和 `report.md`，但页面不能继续启动、恢复或删除这些旧会话，也不会将其自动转换为原生 AI 对话。原生历史存放在 `data/chat.sqlite3` 和 `data/chat-sessions/`，继续正常使用。
