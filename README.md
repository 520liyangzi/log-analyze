# LogScope · 本地多节点日志分析

上传一个外层 ZIP，搜索全部节点的 `.log` / `.log.gz`，通过接口、Pod、时间、线程和流水号定位请求。中文界面，日志与索引保存在本机。

**Python 3.10+ 即可运行搜索；Windows 网页交互终端需要安装 requirements.txt 中的 pywinpty。无需 Node.js、Java、Docker 或外部数据库。** 前端为原生 HTML/CSS/JavaScript，后端为 Python 标准库 + SQLite；可断网运行（可选 AI 需要访问模型服务）。

## 1. Windows 启动

先安装 **Python 3.10 或更新版本**，安装时勾选 `Add python.exe to PATH`。已有 Python 可运行 `python --version` 检查。

```powershell
git clone https://github.com/520liyangzi/log-analyze.git
cd log-analyze
python -m pip install -r requirements.txt
python app.py
```

打开 **http://127.0.0.1:8765**。也可以双击仓库根目录的 **start.bat**，然后打开上述地址。窗口要保持运行，关闭窗口就会停止服务。

页面会保留上次选择的日志包、所在页面、搜索词、筛选条件、流水号以及 AI 问题。刷新浏览器后可继续填写；这些内容只保存在当前浏览器的本地存储中。

Linux / macOS：

```bash
git clone https://github.com/520liyangzi/log-analyze.git
cd log-analyze
python3 app.py
```

端口占用时：`python app.py --port 8877`，浏览器也改为 `http://127.0.0.1:8877`。

## AI 排查：填问题，开始分析

页面内置真实交互终端：Windows 使用 CMD + ConPTY，Linux/macOS 使用 PTY Shell。支持输入、方向键、权限确认、后续追问和 Ctrl+C，全部在页面里完成。

1. 上传并选择日志包，点击左侧 **AI 排查**。
2. 第一次展开 **启动设置**：默认命令 `claude`，公司版直接改为你的启动命令并保存。默认“自动传入任务”；如果公司命令不支持启动时接收问题，选择“兼容模式”。
3. 填写问题（也可点击示例），点击 **开始排查**。默认会随启动命令传入读取任务的提示，无需再粘贴长提示词。
4. 在终端完成 AI 自身的登录、工作目录信任或工具权限确认。兼容模式下，等 AI 进入对话界面再点击 **AI 就绪后发送任务**。
5. AI 会使用查询脚本搜索、关联、核验日志并回复；直接在同一终端继续追问。AI 写入 `report.md` 后页面自动显示报告，也可以下载。

**不需要安装 Skill，也不需要在页面填模型 API Key。** 使用你本机 AI 原有的登录、模型和环境配置，公司版本的实际命令兼容性需要本地验证。启动失败时，结束该终端，调整命令或兼容模式后重新开始。

**编辑分析规则** 可以修改通用流程和公司业务规则，支持历史版本、恢复默认及修改备注。保存影响新任务；已有任务保留创建时的问题、日志包和规则快照。想让已有对话采用新规则，确认 AI 已就绪后点击 **发送最新规则到本次对话**，再从 AI 回复确认它采用了新版本。工具不会自动判断模型已经采纳规则。

**预览任务** 可在启动前查看完整提示词；**查看本次任务** 可核对实际创建的版本。输入框中的问题是下一次新任务的草稿，修改它或切换左侧日志包，不会改变正在运行的会话。最多同时运行 3 个终端；刷新网页可重连，停止服务会结束进程，历史文件仍在磁盘。终端输出仅保留最近约 200 万字符，不是持久化录像。

Skill 仍可选安装，作为外部 AI 发现查询工具的入口；分析流程统一从页面维护。日志解析格式变化仍需修改解析器，不会因为更新提示词自动修正旧索引。独立的 **API 问诊** 保留原有的一次性证据分析方式，不使用本机终端规则编辑器，也不支持多轮自主调用工具。

升级到 v1.3：停止旧服务 → `git pull` → `python -m pip install -r requirements.txt` → `python app.py` → 刷新页面。**从 v1.2 升级不必重新导入日志**，新建 AI 任务即可。已导入的日志与本机配置保留；以后升级代码也不会覆盖你在页面保存的规则。

详细操作与兼容说明见 [AI 排查使用指南](docs/AGENT.md)。

## 2. 先跑一次演示

```powershell
python demo.py
```

生成 `demo-logs.zip`。在页面点击「导入日志包」，上传这个文件。它包含 **2 个 Node、2 个 Pod、8 份日志文件**，包含正常请求、HTTP 500、连接池超时堆栈、GZIP 历史日志。全部是生成的虚构数据。

按以下顺序试：

1. 搜索 `/api/model/map`：所有节点同时命中。
2. 日志类型选 `access`：应命中 160 条，分页可查看全部；「导出全部」导出 160 条原文与来源。
3. 更多筛选中状态填 `5xx`：应剩 1 条，耗时 3051 ms。
4. 点击该行「同 Pod 相邻日志」：自动选相同 Node/Pod，清空接口关键词，同时保留 root/rest/wsf 等所有类型；向前包含 access 耗时再加 5 秒，向后 5 秒。
5. 可点「限定同线程」或「扩大到前后 60 秒」；也可以自己改时间、类型、关键词。
6. ERROR 日志点击「追踪流水号」，或进入左侧追踪页输入 `9124859898865451127`：应显示跨 2 个 Pod 的 6 条记录，包含完整异常堆栈。
7. 搜索 `gzip-history-hit`：应在 2 个节点的历史 `.log.gz` 中命中，来源显示外层 ZIP、内层 ZIP 和 GZIP 文件路径。

还可以生成一份更接近所提供脱敏包的测试日志：

```powershell
python sample_logs.py
```

生成的 `generated-log-sample.zip` 完全是虚构数据，包含 2 个 Node、2 个 namespace/Pod/Service、access/root/rest/wsf 四类明文与 GZIP 日志，共 16 份文件。它包含 HTTP 500、HTTP 200 业务慢请求、异步线程 ERROR、19 位流水号、RouteID、Java 堆栈和历史压缩日志，可直接上传验证全局搜索与 AI 排查。

建议依次测试：精确接口 `/api/model/map`（2 条 access，其中 1 条 500、耗时 3012 ms）；流水号 `9125008448899317884`（跨两个 Pod 共 8 条）；关键词 `synthetic gzip history marker`（两个 Node 的 wsf GZIP）；接口 `/api/rest/example/v2/query/chat-task`（Node01 为 HTTP 200、3155 ms，附近存在异步线程 ERROR，应该标为候选关系）。

## v1.2：按提供的脱敏包修正

- namespace / Pod 通过 `pod-service` 目录交叉核对，支持 namespace 含下划线。
- 补齐 access 的 RouteID、响应字节、数字线程编号，root/rest 的 RequestId 与代码位置，wsf 的模块标识。
- 支持 Service 筛选、接口路径精确匹配、RouteID / RequestId 完整值搜索；原文仍完整保留。
- 核对 fileList 清单与实际导入文件，并统计物理行数和有/无时间的记录数。
- 关联默认保留所有日志类型与线程，不把 HTTP 200 直接判定为业务正常。

**从 v1.1 或更早版本升级到 v1.2 时，需要重新上传旧日志包**，才能补齐新字段并修正原索引的 Pod。旧数据保留并给出提示，不自动重写。v1.3 不改变解析格式，已有 v1.2 日志无需重传。实际样例验证记录见 [SAMPLE_VALIDATION.md](docs/SAMPLE_VALIDATION.md)。

## 3. 上传真实日志包

支持附件描述的动态目录名：

```text
任意外层.zip
├── fileList.txt
├── 任意node-a.zip
│   └── namespace_pod/
│       └── service/
│           └── pod-service/
│               └── log/
│                   ├── access.log
│                   ├── root.log
│                   ├── run.log
│                   └── root.2026-09-08.log.gz
└── 任意node-b.zip
    └── ...
```

`fileList.txt` 不是日志，不参与检索；程序实际遍历 ZIP 内容，不依赖清单准确性。只处理固定 `log/` 层下文件名含 `.log` 的文件；Node、namespace、Pod、service、日志类型均动态识别。内层 ZIP 文件名作为 Node 标识，原始名称始终保留在来源中。GZIP 不会展开到用户指定目录，内层 ZIP 使用临时文件，日志流式读取。v1.1 起会额外保留外层 ZIP，供「核验原始压缩包」回读；旧版已导入日志仍可检索，但需重新上传才可核验。

左侧会显示当前原始 ZIP 的大小。搜索结束后点击 **删除当前日志包**，确认后会删除该包的索引和原始 ZIP，并在后台压缩 SQLite 文件以释放磁盘空间。正在被 AI 终端使用的日志包需要先结束对应终端；历史任务和已经生成的报告继续保留。

时间筛选可以直接粘贴日志中的 `2026-09-08 15:15:30.243`，也兼容 `2026-09-08T15:15:30.243`、斜杠日期、方括号和 `+0800` / `+08:00` 时区。不填写时区就使用旁边选中的筛选时区。

导入选项：

- **编码**：默认先按 UTF-8 逐行解码，失败回退 GB18030；已知编码可明确指定 UTF-8 / GB18030（兼容 GBK）。无法解码的字符会显示替换符，并给出导入提示。
- **无时区日志**：默认 `+0800`，有显式时区的 root 日志优先使用它自己的时区。
- **access 最后一列**：新版说明明确为耗时，默认毫秒；兼容其他包时可改秒或微秒；界面统一显示 ms，原始文本完整保留。若该字段在你们系统里不是耗时，需要据真实字段定义调整解析器。

## 4. 搜索怎么用

| 需求 | 操作 |
|---|---|
| 全节点搜接口、关键字、时间字符串 | 输入搜索框，不选 Node / Pod / 类型 |
| 只搜一个节点 | 选 Node |
| 只搜一个 Pod | 选 Pod；选项包含 namespace，避免同名混淆 |
| 只搜 access / root / interface 等 | 选日志类型，类型从实际文件名生成 |
| Pod 与类型叠加 | 同时选择两个筛选 |
| 指定文件名 | 填 `root*.log*`、`access.log`；使用 SQLite GLOB，支持 `*`、`?`、`[]`，区分大小写 |
| 精确打开来源文件 | 文件目录点击「搜索此文件」，内部按文件 ID 限定，避免同名文件串搜 |
| 查 HTTP 错误 / 慢请求 | 更多筛选填 `500` / `5xx` / 最低耗时 ms |
| 根据 access 查正式日志 | 点击「同 Pod 相邻日志」，自动清空接口搜索词，保留所有日志类型，按含 access 耗时的时间范围检索 |
| 查一个请求 | 进入流水号追踪，完整 ID 精确匹配；64 位以上数字也按字符串处理 |
| 看异常堆栈 | 每条记录保留后续多行；搜索到堆栈内容时展示整条记录 |
| 原文核验 | 点击「核验原始压缩包」，回读原 ZIP 中对应文件/行并与索引原文比较 |
| 回到原文件查看前后内容 | 点击「查看上下文」，展示原文件相邻记录和原始行号 |
| 保存全部结果 | 点击「导出全部」，下载 NDJSON，每行 JSON 包含原文、行号和完整来源 |

所有搜索条件是 **AND** 关系；主搜索框是连续文本匹配（默认忽略英文大小写），不是正则、分词或多关键词 OR。空关键词可以浏览整个已选范围。分页每页 50 条，可跳页，**没有只保留前 N 条的截断**。总数是日志记录数，堆栈合并后的一条记录可以包含多行原始文本。

时间筛选包含起止边界，默认按 UTC+08:00 输入，也可选 UTC。界面会保留每条日志解析时采用的时区。时间排序基于实际时间戳；没有识别到时间的记录排在最后。节点时钟偏差不做自动校正。

SQLite 支持 FTS5 trigram 时自动使用连续子串索引，再检查原文；不支持时自动降级扫描，功能仍然可用，速度受日志规模影响。索引会占用额外磁盘；首次导入需要时间，后续不需要重新解压。

## 5. 流水号与关联的边界

root 按下面格式提取第一、第二个 ID，第一列 ID 作为流水号，第二列也是重复的 traceId；旧 API 字段名 `span` 仅兼容保留，不代表父子 Span：

```text
[2026-09-08 09:29:02.186 +0800] [9124859898865451127] [9124859898865451127] [INFO] [http-nio-uds-exec-9] [aaaaservice.java] [com.xxxx] [Map] [125] Successfully loaded model map,count: 5
```

access 格式支持普通双引号和反斜杠转义双引号：

```text
2026-09-08 09:55:14,158 INFO  162 [http-nio-uds-exec-7][ROOT][][c.h.c.t.a.l.AccessLogValveExt 28] \"GET /api/model/map HTTP/1.1\" 200 14 - 3
```

流水号页面只纳入被解析为同一完整流水号的记录。其他格式里仅仅出现某个 ID，可以用全局关键词搜索；暂不猜测所有可能的 ID 字段。时间线不假装知道父子服务调用关系。access 若没有流水号，使用同 Pod、时间窗口和可选线程查候选日志；HTTP 200 也可能对应 root/rest 的业务 WARN/ERROR；RouteID、RequestId 与 traceId 是不同字段。**线程复用、异步线程切换都可能造成误匹配或漏匹配**，因此同线程筛选默认不开启。

## 6. 可选 AI 配置（可以以后再试）

1. 页面右上角「模型配置」填写 Base URL 和模型名。地址例如 `https://your-gateway/v1`，程序追加 `/chat/completions`。
2. 在启动服务的终端设置 API Key，然后启动或重启：

```powershell
# PowerShell，只影响当前窗口；把下方占位值替换成真实密钥
$env:LOG_AI_API_KEY="your-api-key"
python app.py
```

```bash
# Linux/macOS
export LOG_AI_API_KEY='your-api-key'
python3 app.py
```

3. 进入 API 问诊，填写接口路径与问题，勾选本次日志发送确认，再开始分析。

第一版采用**确定性检索 + 模型分析**：按接口路径精确匹配 access（忽略查询参数），优先选择错误和慢请求；取最多 5 个请求关联同 Node/Pod、时间范围内所有类型和线程的日志，避免漏掉异步回调；最多扩展 20 个候选流水号，再将证据交给模型。为防止上下文过大：接口候选最多 100 条，每次关联或流水号检索最多 100 条，单条原文最多 4000 字符，总证据最多 60000 字符。界面明确显示证据数，AI 证据上限不影响普通搜索、分页和全量导出。

目前不做自由工具调用式智能体，也不声称自动找出的原因必然正确。只对 Bearer 凭证做基础脱敏，**不是完整敏感信息识别器**，请确认日志可发送给你的模型服务。API Key 不写入浏览器、仓库和配置文件；模型地址/名称保存在本机 `data/ai-config.json`。未主动发起 API 问诊，不会通过此适配器请求模型。另一个「AI 排查」入口由你启动的 Claude/公司 Agent 自行使用其模型配置。

## 7. 数据、限制与维护

默认只监听 `127.0.0.1`，供本机单人使用，不是公网多人服务。Host / Origin 校验限制第三方网页读取本地 API。无第三方 CDN 或前端遥测。

- 数据位置：`data/logs.sqlite3` 及 SQLite 伴随文件；重启保留导入记录。原 ZIP 位于 `data/archives/`，因此会额外占用压缩包大小的磁盘空间。
- 分析规则：`data/analysis-rules.json` 保存本机自定义流程、业务规则及全部历史版本。`data/terminal-config.json` 保存启动命令与传入方式。
- 终端任务：`data/terminal-sessions/<id>/` 存放任务、规则快照、查询工具和 Agent 报告。终端输出只在内存保留最近约 200 万字符；这不是持久化终端录像。Agent 自己的会话记录由其配置决定。
- 大日志包会同时占用保存的原始 ZIP 与 SQLite/FTS 索引空间。导入采用流式读取，v1.4 减少了每条记录一次多余的数据库写入；实际耗时仍取决于压缩后大小、解压后行数和磁盘速度。
- 更换目录：`python app.py --data D:\logscope-data`。
- 清空数据：先停止服务，再删除自己的 `data` 目录（也会删除模型地址配置）。
- ZIP 导入失败时整体回滚，显示明确错误，不把不完整结果冒充成功。
- 暂不支持加密 ZIP、7z、rar、tar、非约定目录层级；未识别的日志头仍保留原文供搜索。
- 没有毫秒/时间头的续行附在前一条日志后，避免拆散 Java 异常堆栈。
- 超大日志暂未做分布式搜索；导入串行执行。无关键词全量统计、导出和大量匹配时会较慢。

可通过环境变量调整资源上限（默认值如下）：

| 环境变量 | 默认 | 含义 |
|---|---:|---|
| `LOG_MAX_UPLOAD_GB` | 4 | 外层 ZIP 大小上限 |
| `LOG_MAX_EXPANDED_GB` | 20 | 累计读取的嵌套 ZIP 字节及日志解压字节上限，保守合计 |
| `LOG_MAX_RECORD_MB` | 8 | 单行或合并多行记录上限，超过则导入报错，不静默截断 |

嵌套深度最多 4 层，ZIP 条目最多 100000。数据、压缩包、密钥和本地配置均已列入 `.gitignore`，请不要强制提交真实日志。

## 8. 开发与验证

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m py_compile app.py analysis_rules.py demo.py terminal_bridge.py
```

可选安装 Node.js 后执行 `node --check dist/app.js` 检查前端语法；**运行项目不需要 Node.js**。

目录：

```text
app.py              HTTP API、ZIP/GZIP 导入、SQLite 搜索、解析和 AI 适配
 dist/index.html    中文工作台结构
 dist/style.css     响应式样式
 dist/app.js        上传、筛选、分页、时间线、关联、配置
 demo.py            生成可复现的模拟日志包
 sample_logs.py     生成接近脱敏附件结构的综合测试日志包
 tests/             搜索、核验、CLI、真实 PTY 输入输出与中断等测试
 analysis_rules.py  规则版本存储和任务提示词组装
 prompts/           默认分析流程和稳定工具说明
 terminal_bridge.py 本机真实终端后端
 dist/terminal.js   xterm.js 交互终端
 skills/logscope/   可选 Skill 入口和只读日志 CLI
 install_skill.py   安装 Skill 到 Claude 或指定目录
 docs/              原始目录约定和验证说明
 start.bat          Windows 启动
```

后续拿真实包试用时，如果某种格式未识别，提供一小段脱敏日志和完整文件路径，就可以针对解析器补充规则，不需要更改压缩包结构。
