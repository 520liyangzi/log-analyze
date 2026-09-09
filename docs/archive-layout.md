# 日志包目录层级结构说明

> **层级结构固定不变**，各层中的目录名、文件名是动态变化的（node名、pod名、service名等均不固定）。
> 日志类型不固定，不同 service 可能有不同的日志类型集合，以下为已知类型。

---

## 一、整体结构

日志包是一个 **两层嵌套zip** 结构：

```
{外层zip}.zip
├── fileList.txt              ← 纯文本文件，全量文件路径清单
└── {内层zip}.zip             ← 按node节点打包的zip（可能有多个）
    └── {namespace}_{pod}/
        └── {service}/
            └── {pod}-{service}/
                └── log/      ← 固定目录名
                    ├── *.log.gz
                    └── *.log
```

---

## 二、各层说明

| 层级 | 类型 | 说明 | 是否固定 |
|------|------|------|----------|
| 第0层 | zip | 外层打包zip | 名称不固定 |
| 第1层 | zip | 按node节点打包的zip，可能有多个 | 名称不固定 |
| 第2层 | 目录 | `{namespace}_{pod}` 下划线连接 | 名称不固定 |
| 第3层 | 目录 | `{service}` service名称 | 名称不固定 |
| 第4层 | 目录 | `{pod}-{service}` 短横线连接，与第2、3层对应 | 名称不固定 |
| 第5层 | 目录 | 日志目录，名称固定为 `log` | **固定** |
| 第6层 | 文件 | 日志文件 | 文件名不固定 |

---

## 三、fileList.txt

- 位于外层 zip 根目录下的纯文本文件
- 每行一条路径，格式：`{内层zip名}/{目录}/{目录}/{目录}/log/{日志文件名}`
- 包含所有日志文件的完整相对路径

---

## 四、log 目录下的文件类型

| 文件类型 | 说明 |
|----------|------|
| `*.log.gz` | 历史轮转日志，gzip 压缩，需解压后读取 |
| `*.log` | 当前活跃日志，明文，直接读取 |

> 同一 `log/` 目录下可能有多个 `.log.gz` 和多个 `.log`，文件名不固定。

---

## 五、已知日志类型及格式

### 5.1 access.log — 访问日志

**格式（单行）：**

```
{时间,yyyyMMdd HH:mm:ss,SSS} {级别} {线程ID} [{线程名}][ROOT][][{类名} {行号}] \"{HTTP方法} {路径} HTTP/1.1\" {状态码} {响应大小} {RouteID} {耗时ms}
```

**特点：**
- 日志级别有 INFO
- RouteID 在健康检查类请求中为 `-`，在业务请求中为 `RouteID-{标识}`
- 时间格式为逗号分隔毫秒：`2026-09-08 09:00:01,822`

**示例：**

```
2026-09-08 09:00:01,822 INFO  162 [http-nio-exec-7][ROOT][][c.h.c.t.a.l.AccessLogValveExt 28] "GET /rest/example/v1/healthcheck HTTP/1.1" 200 14 - 3
2026-09-08 09:27:12,727 INFO  162 [http-nio-exec-8][ROOT][][c.h.c.t.a.l.AccessLogValveExt 28] "GET /api/rest/example/v1/data?param=test HTTP/1.1" 200 105 RouteID-example-1788830832613-98 54
```

---

### 5.2 root.log — 主日志（最重要）

**格式（单行）：**

```
[{时间,yyyyMMdd HH:mm:ss.SSS +0800}] [{traceId}] [{traceId}] [{级别}] [{线程名}] [{文件名}] [{全限定类名}] [{方法名}] [{行号}] {消息}
```

**核心特点 — 流水号（traceId）：**

- 第2、3个方括号字段为 **流水号（traceId）**，两个值相同
- **无请求上下文时**（定时任务、后台线程）：为空 `[] []`
- **有请求上下文时**（接口调用）：填充流水号，如 `[9124403017424371820] [9124403017424371820]`
- 同一个接口请求的完整链路，所有日志行共享同一个 traceId
- **通过 traceId 可以搜索出一个接口请求的全部相关日志**，这是追踪接口流程的关键手段

**日志级别：** INFO、WARN、ERROR、DEBUG

**异常堆栈：**
- ERROR 级别日志后面可能紧跟 `at` 开头的异常堆栈行（缩进制表符 `\t`）
- 堆栈行不是标准日志格式，是 Java 异常栈的多行延续

**各种情况示例：**

无流水号 — 定时任务/后台线程：
```
[2026-09-08 09:00:00.000 +0800] [] [] [INFO] [scheduler-5] [ExampleJob.java] [com.example.service.ExampleJob] [runTask] [60] scheduled task begin
[2026-09-08 09:00:53.535 +0800] [] [] [WARN] [qtp1885052027-61] [WatchConnector.java] [com.example.watch.WatchConnector] [lambda$postRequest$7] [249] [SecretWatch] credential key update!
```

有流水号 — 接口请求链路（同一 traceId 贯穿）：
```
[2026-09-08 09:27:12.714 +0800] [9124389488277389415] [9124389488277389415] [INFO] [http-nio-exec-8] [HttpRest.java] [com.example.rest.HttpRest] [startProcess] [555] Send Msg-->: to ,path:/rest/plat/v1/auth,method:POST,RequestId:RouteID-example-260908092712-1335
[2026-09-08 09:27:15.789 +0800] [9124403017424371820] [9124403017424371820] [INFO] [http-nio-exec-9] [HttpRest.java] [com.example.rest.HttpRest] [startProcess] [555] Send Msg-->: to ,path:/rest/plat/v1/auth,method:POST,RequestId:RouteID-example-260908092715-1336
[2026-09-08 09:27:15.807 +0800] [9124403017424371820] [9124403017424371820] [INFO] [http-nio-exec-9] [DataService.java] [com.example.service.DataService] [queryData] [121] query data success, data: [{"id":1,"name":"item1"}]
```

有流水号 + ERROR：
```
[2026-09-08 09:27:16.005 +0800] [9124403017424371820] [9124403017424371820] [ERROR] [http-nio-exec-9] [ResultParser.java] [com.example.utils.ResultParser] [parseResult] [67] resultText is null
```

无流水号 + ERROR + 异常堆栈：
```
[2026-09-08 09:26:14.898 +0800] [] [] [ERROR] [consumer-thread-3] [ConsumerImpl.java] [com.example.mq.ConsumerImpl] [checkTopics] [728] checkTopics occur exception: com.example.mq.MqException: checkTopics fail, topic is not valid, topic: Example_Topic
	at com.example.mq.ConsumerImpl.checkTopics(ConsumerImpl.java:722)
	at com.example.mq.ConsumerImpl.subscribe(ConsumerImpl.java:151)
	at java.base/java.lang.Thread.run(Unknown Source)
```

有流水号 + WARN：
```
[2026-09-08 09:29:37.522 +0800] [9125008448899317884] [9125008448899317884] [WARN] [http-nio-exec-3] [AgentResult.java] [com.example.agent.AgentResult] [read] [208] [sys warn] [a1b2c3d4-e5f6-7890-abcd-ef1234567890] illegal business data: not starts with data:
```

---

### 5.3 wsf.log — WSF框架日志

**格式（单行）：**

```
{时间,yyyyMMdd HH:mm:ss,SSS} {级别} {线程ID} [{线程名}][ROOT][][{类名} {行号}] [{WSF模块}] {消息}
```

**特点：**
- 时间格式为逗号分隔毫秒（与 access.log 一致）
- 日志级别有 INFO
- 消息前有 `[WSF-xxx]` 模块标识

**示例：**

```
2026-09-08 09:00:01,819 INFO  162 [http-nio-exec-7][ROOT][][c.h.s.v.f.ParamCheckFilter 367] [WSF-ParamValidate] Init internal message.
2026-09-08 09:00:01,820 INFO  162 [http-nio-exec-7][ROOT][][c.h.s.v.m.FormProcessValidate 387] [WSF-ParamValidate] Form need validate parameter number.
```

---

### 5.4 rest.log — REST调用日志

**格式（单行）：**

```
[{时间,yyyyMMdd HH:mm:ss.SSS +0800}] [{traceId}] [{traceId}] [{级别}] [{线程名}] [{文件名}] [{全限定类名}] [{方法名}] [{行号}] {消息}
```

**特点：**
- 格式与 root.log 一致（方括号格式，带 traceId）
- 记录外部 REST 调用的请求和响应
- ERROR 级别可包含异常堆栈
- 可能有 IP:端口 信息

**示例：**

```
[2026-09-08 09:29:37.521 +0800] [9125008448899317884] [9125008448899317884] [INFO] [http-nio-exec-3] [RequestProcessor.java] [com.example.rest.RequestProcessor] [responseForStream] [133] received response for null
[2026-09-08 09:29:39.458 +0800] [] [] [ERROR] [qtp1961067370-73] [RequestProcessor.java] [com.example.rest.RequestProcessor$4] [onFailure] [153] Response failed 127.0.0.1:32018/rest/example/v1/chat java.nio.channels.AsynchronousCloseException: null
	at org.eclipse.jetty.client.InputStreamResponseListener.onContent(InputStreamResponseListener.java:122)
	at java.base/java.lang.Thread.run(Unknown Source)
```

---

## 六、日志格式总结

### 两种时间格式

| 格式 | 特点 | 出现在 |
|------|------|--------|
| `yyyyMMdd HH:mm:ss,SSS` | 逗号分隔毫秒，无时区，无方括号 | access.log、wsf.log |
| `yyyyMMdd HH:mm:ss.SSS +0800` | 点号分隔毫秒，带时区，方括号包裹 | root.log、rest.log |

### 两种日志行格式

| 格式 | 特点 | 出现在 |
|------|------|--------|
| 逗号时间格式 | `{时间} {级别} {线程ID} [{线程名}][ROOT][][{类名} {行号}] {消息}` | access.log、wsf.log |
| 方括号格式 | `[{时间}] [{traceId}] [{traceId}] [{级别}] [{线程名}] [{文件名}] [{类名}] [{方法名}] [{行号}] {消息}` | root.log、rest.log |

### traceId（流水号）规则

| 场景 | traceId 值 | 说明 |
|------|-----------|------|
| 定时任务/后台线程 | `[] []`（空） | 无请求上下文 |
| 接口请求 | `[数字串] [数字串]` | 有请求上下文，同一请求链路共享同一 traceId |

### 异常堆栈规则

- ERROR 级别日志后可能紧跟 `\tat` 开头的堆栈行
- 堆栈行以制表符 `\t` 缩进，不是独立日志条目
- 堆栈行格式：`\tat {全限定类名}.{方法名}({文件名}:{行号})`
- 堆栈可能包含 `java.base/` 前缀的 JDK 内部类