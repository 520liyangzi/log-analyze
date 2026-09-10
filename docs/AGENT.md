# AI 排查使用指南

推荐使用“页面提示词 + 稳定查询脚本 + 本机 AI”的方式。你维护排查经验，程序负责可重复的查询和来源追溯，本机 Claude 或公司 AI 负责多轮搜索与分析。Skill 只是可选入口，不需要安装才能使用页面。

## 第一次运行

Windows 10 1809+ / Windows 11，Python 3.10+：

```powershell
git pull
python -m pip install -r requirements.txt
python app.py
```

首次克隆见 README。打开 `http://127.0.0.1:8765`，上传日志包，进入左侧 **AI 排查**。

1. 展开 **启动设置**。命令默认 `claude`；公司命令如果叫 `codeagent`，填写 `codeagent` 并保存。参数也可写在同一行。
2. 保持默认“自动传入任务”。如果公司命令不接受启动问题，改为“兼容模式”。公司版恢复模板默认是 `{command} --sessions {session_id}`，配置会保存在本机。
3. 填写问题，例如“检查 /api/model/map 为什么慢，看看不同 Pod 是否有异常”。需要结合代码时，在问题下方读取本机 Git 项目并选择分支；也可以留空只查日志。点击 **生成任务并预览**，核对完整 `task.md` 和固定 commit 后，再点 **确认并启动 AI**。
4. 在下方终端完成登录、目录信任和工具权限确认。默认任务已经传入，不必再点击发送。兼容模式等待 AI 进入对话界面后，点击 **AI 就绪后发送任务**。
5. 看 AI 查询并回复，直接继续输入追问。AI 写入 `report.md` 后页面会在约 5 秒内自动显示，也支持手动刷新和下载。需要追到代码时，在报告下选择本机 Git 项目和分支，再预览并确认第二阶段任务。

程序继承启动 LogScope 时的 PATH、环境变量及 AI 登录配置。如果公司工具只能在特定 CMD 环境使用，从那个 CMD 启动 `python app.py`。不会替你安装 Claude，也不会改变它使用哪个模型。网页终端不依赖页面的 API 模型配置。

Windows 终端需要 `pywinpty`；缺少时搜索仍能用，页面会给出安装提示。Linux/macOS 用系统 PTY。xterm.js 已随项目附带，无需 npm 或 CDN。

## 经常变化的规则怎么维护

点击 **编辑分析规则**：

| 区域 | 适合填写 |
|---|---|
| 分析流程 | 通用搜索方法、判断方式、报告格式 |
| 公司业务规则 | 某接口慢请求阈值、错误码含义、已知异步链路、服务约定 |
| 修改说明 | 这次更新的原因，便于以后找回 |

默认流程可直接使用，保留 HTTP 200 检查业务失败、全类型与异步线程关联、精确流水号、原包核验、分页覆盖和事实/推测区分等规则。公司业务规则默认为空，不把某次样例观察直接当作你们的业务事实。

保存后生成新版本。历史版本可载入编辑，保存时再生成一个新版本；恢复默认也需要保存才生效，不删除历史。多页面同时编辑时，旧页面不能覆盖新版本，会提示先刷新版本列表再合并。关掉编辑窗口保留本页未保存草稿；刷新页面会丢失未保存的规则草稿。问题草稿和最近的项目路径保存在当前浏览器。

规则保存在 `data/analysis-rules.json`，重启或更新代码不会覆盖自定义内容。“恢复默认”会使用当前代码携带的默认流程，并清空业务规则。备份数据目录即可同时保留规则历史。

每次任务保存创建时的规则版本。修改规则默认只影响新任务。已有任务需要更新时，先确认 AI 可以接收输入，再点 **发送最新规则到本次对话**。程序写入独立更新文件，并发送让 AI 读取的指令；是否采纳以它的回复和报告为准，页面不把“指令已发出”当作“模型已理解”。

格式变化造成字段未解析时，应修复解析器并重新导入，不能只修改分析提示词。

## 自动启动与兼容模式

默认自动模式相当于在 CMD 中输入：

```text
claude "Read task.md in the current directory. Use its rules and query tools to investigate the question. Reply in Chinese and write report.md."
```

只有这条固定英文提示会追加为参数，问题和规则只写入文件。问题中的引号、换行、`&`、`$()` 等不会被拼成 shell 命令。参数模式依据 [Claude CLI 的交互式初始问题用法](https://code.claude.com/docs/en/cli-reference)，公司魔改版本若不兼容可手动发送；不会用固定延时猜测 AI 是否启动完成。

命令以你填写的 shell 命令执行，路径有空格时按 CMD 习惯加双引号；不要把业务问题写进启动命令。默认不添加跳过权限确认的参数。若需要在外部浏览器登录，按 AI 给出的地址操作，回到页面终端继续。

**只打开 CMD / Shell** 可直接输入命令，或用脚本手动查询。裸 CMD 中点击“发送任务”会被当成命令，所以先运行 AI、等它进入对话后再发送。

## 任务文件与会话

每个新任务独立存放在 `data/terminal-sessions/<ID>/`：

- `task.json`：日志包、问题、本机服务地址、Python 路径、初始规则版本。
- `task.md`：完整问题、规则和工具说明；公司 AI 即使不支持 Skill 也能读取。
- `rules.json`：本次规则版本的原始快照。
- `tools/logscope.py`：自包含的只读查询脚本。
- `tools/project.py`：代码定位阶段使用的只读 Git 查询脚本。
- `CLAUDE.md`：指向本次任务，不覆盖其他项目或全局说明。
- `.claude/skills/logscope/`：可选的轻量发现入口及兼容脚本。
- `rule-updates/` 与 `rule-updates.json`：显式准备的规则更新和版本记录，初始 task.md 不变。
- `report.md`：AI 生成的报告。页面按纯文本显示，不执行其中的 HTML。
- `code-task.md` / `code-task.json`：用户预览并确认后的代码定位任务，固定到当时的 commit。
- `session.json`：窗口状态、AI Session ID、规则更新和代码任务元数据。
- `terminal.log`：终端返回的完整显示记录，便于刷新页面或重启后回看。

启动 AI 前必须经过任务预览；**仅预览 task.md** 不启动 AI、不向模型发日志。**查看本次任务** 查看已创建的快照，避免把后来修改的问题或规则误认为正在使用的版本。

## 日志结论后继续定位代码

推荐在启动任务前就选择项目和分支。项目会写入 `task.md` 并固定到预览时的 commit；AI 必须先用日志索引收敛证据，再根据接口、类名、方法、错误码或堆栈自行判断是否调用 `tools/project.py grep/show/tree/log`。报告分别记录日志事实和代码证据，不允许用代码猜测替代日志核验。

如果启动时没有选择项目，或日志结果指向另一个项目，`report.md` 生成后仍会显示第二阶段入口。填写项目绝对路径、读取分支并预览 `code-task.md` 后确认发送。两种方式都不会 checkout，也不会影响未提交修改；分支在预览后移动时会要求重新预览。

这一阶段只读代码，不执行构建、测试和项目脚本。若要分析未提交改动，需要先自行提交到一个分支；远程最新分支也需要先在项目目录完成 `git fetch`，LogScope 不会替你联网更新仓库。

切换左侧日志包不会改变正在运行的任务。分析新问题时创建新任务，或者在终端里明确追问。页面上方的排查窗口可切换任务，最多同时运行 3 个真实进程，已结束的历史任务不限数量；切走窗口不会结束后台任务。选择历史窗口只显示任务状态，不会自动把旧 CMD 输出写入终端；点击 **查看终端记录** 才回放，点击 **恢复这个对话** 才启动恢复命令。

公司 AI 输出中若出现 `Session ID: <UUID>` 或 `sessions <UUID>`，页面会自动保存；也可把 UUID 粘贴到 **AI Session ID** 后手动保存。服务重启或任务结束后点击 **恢复这个对话**，默认执行：

```text
<你的启动命令> --sessions a0c43b85-1ca0-41e3-8e99-15648dd3ec17
```

如果工具语法不同，在启动设置修改恢复命令模板；标准 Claude 使用 `{command} --resume {session_id}`。LogScope 只能重新调用工具的会话恢复能力，原会话是否存在、能否恢复由本机 AI 决定。

历史任务不再需要时可点击 **删除会话**。运行中的任务必须先结束；二次确认后会删除整个任务目录，包括任务快照、Session ID、终端记录和报告，且无法恢复。日志包和其他排查任务不受影响。

终端具有本机当前用户的权限，提示词中的只读要求不是 OS 沙箱。HTTP 只监听 127.0.0.1，并检查 Host/Origin。页面回放最多读取最近约 2 MB，但 `terminal.log` 会保存终端返回的完整内容；输入若被程序回显也会落盘，因此不要在终端输入不应保存的明文秘密。AI 自己的聊天存储遵循其配置。

## 可选：在外部 AI 里使用 Skill

先运行 LogScope 并导入日志，再按需要安装：

```powershell
python install_skill.py
python install_skill.py --project D:\your-project
python install_skill.py --target D:\company-agent\skills\logscope
```

上面是三个可选位置，不必全部执行。目标目录存在会拒绝覆盖，升级前先备份/移走旧目录。默认安装到当前用户 `~/.claude/skills/logscope`。普通 Claude 可用 `/logscope`；公司工具是否发现 Skill 以实际版本为准。

外部 Skill 通过 CLI 的 `rules` 命令读取页面保存的分析流程和公司业务规则，因此不再需要修改 SKILL.md 来更新排查经验。已有页面任务仍使用自己的版本快照，不自动拉取最新规则。

```powershell
python skills/logscope/scripts/logscope.py rules
python skills/logscope/scripts/logscope.py datasets
python skills/logscope/scripts/logscope.py --dataset YOUR_DATASET_ID search --endpoint /api/model/map --access-only
python skills/logscope/scripts/logscope.py correlate 123 --seconds 30
python skills/logscope/scripts/logscope.py --dataset YOUR_DATASET_ID trace 9124859898865451127
python skills/logscope/scripts/logscope.py verify 123
```

替换示例数据集 ID、日志 ID、接口和流水号；在页面创建的任务目录使用 `tools/logscope.py`。`--url` 和 `--dataset` 放在子命令前，其他参数放在子命令后。查询 JSON 明确给出命中总数、has_more、下一页以及完整来源。

`verify` 核对原包解码文本与索引一致，不证明解析语义或根因。`--scan` 只扫描已导入文本，不能找出未导入的文件。`correlate` 返回时间/线程等候选，不保证同一请求。大量匹配可以翻页或 `export`，报告需明确实际审阅范围。

## 常见情况

| 现象 | 处理 |
|---|---|
| claude 或公司命令不存在 | 检查本机安装与 PATH；从可运行该命令的 CMD 启动 LogScope |
| 公司命令不接受启动参数 | 结束终端，改为兼容模式后重新开始 |
| AI 只启动，没有开始查 | 完成登录/确认；进入对话后点击发送任务，查看它是否读取 task.md |
| AI 不识别 Skill | 不影响页面任务；让它读取 task.md，按工具说明运行脚本 |
| 改规则后旧对话没变化 | 保存规则后，显式发送最新规则，并从 AI 回复确认 |
| 规则保存提示版本冲突 | 刷新版本列表，保留你的草稿，载入最新版本再合并 |
| 报告没有出现 | 让 AI 保存当前目录 report.md；写完后页面自动更新 |
| 终端太小 | 点全屏，尺寸会同步给真实终端 |
| 服务重启后进程消失 | 在历史窗口查看记录；保存过 Session ID 时点击“恢复这个对话” |

升级到 v1.8 不需重传日志。旧 AI 任务会显示在历史窗口，但升级前没有记录的终端正文和 Session ID 无法补回。v1.1 或更早的数据若显示解析器升级提示，需要重新上传以补齐字段；v1.0 未保留原 ZIP 的数据也需要重新上传才能原包核验。
