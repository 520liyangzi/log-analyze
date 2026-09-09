# 在网页终端里完成排查

LogScope 提供三个互补入口：手动搜索、本地 Agent 终端、可选模型 API 问诊。终端直接运行你机器上的 Claude 或公司版本，Skill 定义检索与证据核验流程，脚本提供可重复执行的查询接口。AI 不会自动提升日志解析准确率；关键是让它保留检索范围、翻页核对、回读原文，并区分事实和猜测。

## 第一次使用

Windows 10 1809+ / Windows 11，Python 3.10+：

```powershell
git pull
python -m pip install -r requirements.txt
python app.py
```

如果是第一次克隆，先按 README 操作。`pywinpty` 是 Windows 真实交互终端的必要依赖；缺少它时搜索功能仍可使用，但页面会明确提示终端不可用，不会降级成假终端。Linux/macOS 使用系统 PTY，无额外 Python 依赖。前端 xterm.js 已随仓库附带，无需 npm，不需要 CDN。

终端内的程序继承启动 LogScope 时的 PATH。原先在 CMD 能输入 `claude` 启动，就在页面启动命令填 `claude`。例如公司命令叫 `codeagent`，改成 `codeagent`；如果它只在某个已设置环境的 CMD 里可运行，就从那个 CMD 启动 `python app.py`。这不会替你安装 Claude 或公司工具。

操作顺序：上传日志包 → Agent 终端 → 填问题和启动命令 → 启动 → 在终端内完成 Agent 的首次提示 → 点击「发送排查任务」→ 阅读回复和继续追问。

「只打开 CMD / Shell」保留普通命令行，可直接输入启动命令、切换配置或运行查询脚本。启动命令是你明确填写的本机 shell 命令，不是模型 Base URL。登录地址如果需要在浏览器打开，按 Agent 提示复制地址并完成登录；终端仍保留等待输入。

## 任务怎么交给 Agent

每次终端创建独立目录 `data/terminal-sessions/<随机ID>/`：

- `task.json`：当前日志包 ID、名称、本机服务地址、Python 路径、用户问题。
- `task.md`：排查问题与完整 Skill 工作流，公司 Agent 不识别 Skill 目录也能读取。
- `CLAUDE.md`：指向任务文件，不覆盖你全局或其他项目的说明。
- `.claude/skills/logscope/`：本次使用的独立技能副本与查询脚本。
- `report.md`：Agent 按任务要求生成后，页面可以查看和下载。

「发送排查任务」只发送一条读取 `task.md` 的提示。等 Agent 已经进入聊天界面再点；在裸 CMD 提示符直接点会被 CMD 当作命令，而不会变成 AI 对话。之后像在原 CMD 一样打字、回车、用方向键、选择确认项或 Ctrl+C。工具权限确认由 Agent 原有机制处理，LogScope 不替你自动确认，也不增加绕过权限的参数。

任务创建后固定绑定当时日志包和问题。改左侧日志包或输入框不会改变已经运行的任务；要分析另一个包，请新建终端，或在终端明确告诉 Agent 另一个数据集 ID。页面刷新会重连该标签页上次选中的会话，不会新开一个同名进程。重新启动 LogScope 后，旧进程已经结束，历史任务与报告文件仍在磁盘。

终端是真实本机权限，不是沙箱；Skill 的只读要求是工作流程约束，不能限制一个任意 shell 程序。终端只监听本机 HTTP，通过 Host/Origin 校验拒绝跨站调用，不对外开放 WebSocket 或命令执行端口。输入不被单独记录；终端程序若自己回显内容，那会出现在终端画面中。当前屏幕输出在内存保留约 200 万字符，超出会提示缓存重置；完整业务日志可通过 CLI export 导出，Agent 自身对话记录由它自己的配置负责。

## 单独作为本地 Skill 使用

不从网页启动 Agent 也可以用。先运行 LogScope 并导入日志，再安装一次：

```powershell
# 默认安装到当前用户 ~/.claude/skills/logscope
python install_skill.py

# 或仅安装到指定项目
python install_skill.py --project D:\your-project

# 或安装到公司 Agent 的自定义技能目录，参数是最终 logscope 目录
python install_skill.py --target D:\company-agent\skills\logscope
```

已有目标目录时会拒绝覆盖；升级前先备份/移走旧的 logscope 目录，再重新运行。不会修改其他技能或 Claude 全局权限设置。普通 Claude 可用 `/logscope` 调用，或让它按 Skill 分析本机日志。公司版本如果不支持自动技能发现，让它读取安装目录内的 `SKILL.md`，按里面的脚本说明操作即可。

手动调用脚本：

```powershell
python skills/logscope/scripts/logscope.py datasets
python skills/logscope/scripts/logscope.py --dataset <日志包ID> files
python skills/logscope/scripts/logscope.py --dataset <日志包ID> search --q /api/model/map --access-only --status 5xx
python skills/logscope/scripts/logscope.py correlate <日志ID> --seconds 5 --kind root
python skills/logscope/scripts/logscope.py --dataset <日志包ID> trace 9124859898865451127
python skills/logscope/scripts/logscope.py verify <日志ID>
```

`<日志包ID>` 等是要替换的占位内容。命令也可在网页 CMD 中执行；用任务给出的 Python 路径和 `.claude/skills/logscope/scripts/logscope.py`。网页终端已设置 `LOGSCOPE_URL` / `LOGSCOPE_DATASET_ID`，一般无需手填这两个全局参数。

改了服务端口时用 `--url http://127.0.0.1:8877`，放在子命令前。`--dataset` 也放在子命令前，查询条件放在子命令后。结果是 JSON，返回总数、当前页、下一页和完整来源，便于 Agent 自主缩小范围或翻页。

## 如何核对「搜得准不准」

- `search`：默认使用现有索引与原文连续子串检查。
- `search --scan`：跳过 FTS，在已导入原文上扫描；不能发现未被导入的文件。
- `verify <id>`：回读保留的原始 ZIP，逐层找到文件/GZIP，对照行号读取原文，与索引内容比较。它验证文本一致性，不证明字段解析或根因推断正确。
- `context`：看异常堆栈和附近日志。
- `trace`：只匹配完整流水号。
- `correlate`：同 Node/namespace/Pod 的所有日志类型时间窗口候选，向前包含 access 耗时，可再加同线程；不是确定调用链。返回 association_reasons，说明只是同线程、同时间窗口，还是有相同 traceId / 请求标识。

v1.0 没保留原 ZIP，旧导入数据仍能搜，但 verify 会返回 `available=false`，需要重新上传。v1.1 开始成功导入时会保留外层 ZIP；新增占用等于原 ZIP 大小。

## 常见情况

| 现象 | 处理 |
|---|---|
| 提示缺少 pywinpty | 使用启动服务的同一 Python 执行 `python -m pip install -r requirements.txt`，然后重启 |
| 提示 claude 不是命令 | 检查安装与 PATH，或填写可执行文件完整路径（空格路径在 CMD 中加双引号） |
| 公司 CLI 不认 Skill | 直接让它读取任务目录中的 task.md，内含完整流程 |
| Agent 等待授权或登录 | 在网页终端里完成；LogScope 不绕过这些提示 |
| 终端显示很窄或布局错位 | 点全屏；页面会把尺寸同步给真实终端 |
| 输入要粘贴多行 | 用「粘贴」按钮，或 Ctrl+Shift+V；行为遵循程序的 bracketed paste 支持 |
| 点击发送任务后提示不是命令 | 当前还在 CMD，先输入启动命令，等 Agent 进入对话界面再发送 |
| 没有 report.md | 让 Agent 将完整分析写入当前目录 report.md，再点刷新报告 |
| 服务重启后会话不存在 | 新建终端；旧报告仍在 data/terminal-sessions 下 |

实现依据与接口参考：[Claude Skills](https://code.claude.com/docs/en/skills)、[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[xterm.js](https://xtermjs.org/)、[pywinpty](https://github.com/andfoy/pywinpty)。公司魔改版以实际行为为准。

## v1.2 格式与流程调整

HTTP 200 不证明业务成功，Skill 会继续查看 root/rest 中的 WARN/ERROR，包括不同线程的异步回调。RouteID、出站 RequestId 和 traceId 分别保留，不把值不同的标识强行拼接。WSF 只根据实际存在的时间/线程关联，不伪造 traceId。第二个重复 traceId 不作为父子 Span。

新增 CLI 条件：`--endpoint`（精确路径，忽略查询参数）、`--request-key`（完整值匹配 RouteID 或 RequestId）、`--route-id`、`--request-id`、`--thread-id`。页面同步提供对应字段与 Service 筛选。

更新项目后，请重新上传旧索引的日志包并新建 Agent 终端，以使用新解析字段和新版 Skill。`datasets` 的 audit 会给出清单核对和物理行/记录覆盖统计；脱敏样例中不同 Pod 的相同文本保留各自来源，不能据此推断服务调用关系。
