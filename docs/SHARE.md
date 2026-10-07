# 做了个日志排查工具：丢进 ZIP，跨 Pod 搜日志，还能结合代码问 AI

排查线上问题时，经常会遇到这样的流程：下载一个日志包，里面套着多个节点的 ZIP；每个 ZIP 解压后再找 `access.log`、`root.log`，挨个搜索接口。找到报错时间后，再回到对应 Pod 的业务日志里翻异常。节点一多，很容易反复解压、漏搜，或者忘了某条日志到底来自哪个文件。

所以我做了 **LogScope**：一个可以在本机启动的中文日志分析工作台，把日志导入、跨节点搜索、请求追踪和 AI 排查放到同一个页面里。

- 项目地址：[520liyangzi/log-analyze](https://github.com/520liyangzi/log-analyze)
- Windows 下载：[LogScope.exe](https://github.com/520liyangzi/log-analyze/releases/download/windows-latest/LogScope.exe)
- 完整使用说明：[README](https://github.com/520liyangzi/log-analyze#readme)
- 直接试用的虚构日志包：[demo-logs.zip](https://raw.githubusercontent.com/520liyangzi/log-analyze/main/docs/examples/demo-logs.zip)

## 先把查日志这件事做顺手

上传外层 ZIP 后，程序会扫描其中的目录，包括嵌套 ZIP 和 GZIP 日志。先确认哪些目录要导入，必要时调整 Pod、服务和日志类型，再建立索引。不同服务的目录结构不一样，也可以在页面上改。

![导入时确认目录、文件匹配规则和日志归属](https://raw.githubusercontent.com/520liyangzi/log-analyze/main/docs/images/import-layout.png)

之后可以直接搜索接口或关键词，也可以把 Node、Pod、日志类型、文件名、时间、HTTP 状态、耗时等条件叠加使用。每条结果都会保留节点、Pod、文件、压缩包路径和原始行号，找到以后还能查看上下文、核验原包、导出结果。

![从全部节点的 access 日志定位 HTTP 500](https://raw.githubusercontent.com/520liyangzi/log-analyze/main/docs/images/search.png)

比如先搜 `/api/model/map`，筛选 `access` 和 `5xx`，找到报错请求后点「同 Pod 相邻日志」，就能继续查看附近的业务日志，不用再手动复制时间、切包、找文件。时间筛选框也支持直接粘贴日志里的毫秒时间。

有流水号时，可以单独追踪这一次请求。Java 异常堆栈会跟所属记录一起保留，结果按时间排列；没有流水号的 access 日志，则通过同 Pod、时间和可选线程寻找候选线索。

![同一流水号在两个 Pod 中的日志时间线](https://raw.githubusercontent.com/520liyangzi/log-analyze/main/docs/images/trace.png)

## AI 可以继续查日志，也可以看对应版本的代码

AI 排查直接在网页里对话，不需要再启动 Claude、codeagent 或另一个终端。维护者在本机 `data/ai-config.json` 配置模型地址、模型名和 API Key，页面上的使用者直接提问即可；模型需支持工具调用。

例如：

> 请检查 /api/model/map 的 HTTP 500。先定位 access 记录，再查看同 Pod 的业务日志和异常堆栈。列出发生时间、Pod、文件路径和流水号，并区分已确认事实、可能原因和还需要验证的事项。

AI 会调用已经建好的日志索引查询，并在页面上展示查询条件、耗时和证据。可以在同一会话继续问「有没有下游连接池异常？」「还有哪些 Pod 受影响？」，也可以同时开启多个排查任务。会话和查询记录自动保存，支持停止、回看、删除和导出报告。

![网页原生 AI 对话及可展开的日志查询记录](https://raw.githubusercontent.com/520liyangzi/log-analyze/main/docs/images/ai-chat.png)

需要结合业务实现时，点输入框上方的 **「项目代码设置」**，填 Git 仓库地址，更新代码并选择对应分支。仓库会保存到运行服务那台电脑的 `data/projects/`，同一地址复用已有仓库。

![通过项目代码设置选择仓库和分支](https://raw.githubusercontent.com/520liyangzi/log-analyze/main/docs/images/project-settings.png)

发送前可以预览完整任务，包括日志范围、规则和固定的代码 commit。AI 只读搜索这一版本的代码，不会自动修改项目或执行项目程序。分支最好和产生日志时部署的版本一致，避免拿最新代码解释旧问题。

上面的截图使用纯虚构日志，AI 回答来自本机模拟模型，用于展示操作流程，不代表某个真实模型的分析效果。

## 不配模型，也能先跑通一个例子

1. 下载 `LogScope.exe`，放到固定目录后双击；浏览器会打开本机页面。
2. 下载上面的 `demo-logs.zip`，点击「导入日志包」。
3. 检查目录预览后，点「确认并建立索引」。
4. 搜 `/api/model/map`，日志类型选 `access`，应有 **160 条**；状态再筛 `5xx`，剩 **1 条 HTTP 500，耗时 3051 ms**。
5. 点「同 Pod 相邻日志」，查看连接池获取连接超时及异常堆栈。
6. 在「流水号追踪」输入 `9124859898865451127`，应看到跨两个 Pod 的 **6 条记录**。
7. 搜 `gzip-history-hit`，可以验证历史 `.log.gz` 的搜索和完整来源路径。

搜索、导入、流水号追踪不需要 API Key。源码也可以用 Python 3.10+ 启动：

```bash
git clone https://github.com/520liyangzi/log-analyze.git
cd log-analyze
python app.py
```

浏览器打开 `http://127.0.0.1:8765` 即可。

## 还有几个实际使用时会用到的功能

- **在线采集**：可接入自己已有的 `collect_logs.py`，填 Pod 和时间后下载 ZIP，再进入目录确认。采集平台脚本需要自行提供，仓库不含通用平台采集器。
- **环境管理**：保存多套采集平台地址、用户名和密码，选择环境后回填。
- **组内共享**：在服务电脑用 `LogScope.exe --host 0.0.0.0` 启动，同事通过 `http://服务电脑IP:8765` 访问，共用模型和代码缓存。
- **磁盘清理**：默认在服务电脑本地时间每天 02:00 清理超过 72 小时的日志包及索引，包含原始 ZIP；AI 会话、配置和代码仓保留。需长期保留的原包请另行保存。

目前主要面向文本日志、ZIP/嵌套 ZIP 和 GZIP。未知日志格式可以先按文本搜索，时间、流水号等结构化字段能否识别仍取决于具体格式。AI 给出的原因也需要结合证据判断，时间相近不等于存在因果关系。

如果你也经常在多个 Pod 的日志之间来回找线索，可以先用演示包试一遍。有不适配的目录或日志格式，欢迎在仓库提 Issue，附上脱敏样例和预期结果。
