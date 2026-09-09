# 脱敏样例验证（v1.2）

输入为用户提供的 `log_sample.zip` 与新版目录/格式说明。仅用原始包的临时副本进行测试，原附件未修改。ZIP 未加入仓库；新增的自动回归用例通过脚本生成相同格式的虚构日志。

## 实际导入与原文核验

| 项目 | 结果 |
|---|---:|
| 内层 Node ZIP | 2 |
| Pod / Service | 2 / 2 |
| 日志类型 | access、root、wsf、rest |
| 日志文件 | 16（8 明文 + 8 GZIP） |
| 原始物理行 | 80 |
| 合并堆栈后的记录 | 62 |
| 识别时间的记录 | 62 |
| fileList 清单条目 | 16 |
| 清单缺失 / 未列入清单的实际日志 | 0 / 0 |
| 原 ZIP 逐条回读，与索引文本一致 | 62 / 62 |
| 解析 HTTP 请求 | 12，均为 200 |

修正前，`ns_alpha_pod-alpha-aaa111` 被分为 namespace=`ns`、Pod=`alpha_pod-alpha-aaa111`。修正后结合下一层 `pod-alpha-aaa111-ServiceA` 正确识别 namespace=`ns_alpha`、Pod=`pod-alpha-aaa111`；另一个节点同理。

## 请求关联检查

样例中的 `/api/rest/example/v2/query/chat-task`：

- 两个 Node 都有 access 记录，HTTP 200，耗时 3155 ms。
- 每个 Pod 的附近窗口同时包含 root WARN 和 rest 记录。
- rest ERROR 使用另一条 qtp 线程且 traceId 为空。默认同线程筛选会漏掉这条候选，因此默认保留全部线程与类型。
- 按流水号 `9125008448899317884` 检索，得到 4 条记录（两 Pod 的 root/rest）。
- 按流水号 `9124403017424371820` 检索，得到 8 条记录，包含业务 ERROR。
- access RouteID 与 root 出站鉴权 RequestId 并不相同；按完整 access RouteID 检索只命中 2 条 access，不将其他 RequestId 强行归入。

以上只证明检索与候选关联行为符合输入。由于样例已脱敏、裁剪，而且两个 Pod 含相同文本，不能据此确认真实跨服务调用关系，也不能断言无 traceId 的 rest ERROR 就是该 access 请求的原因。

## 验证方式

实际样例通过本地 HTTP 服务和 Skill CLI 检索；全量 62 条记录均从保留 ZIP 回读核验。使用模拟模型接口确认 API 问诊证据包含异步 rest 异常，但没有发送样例给真实外部模型。

另有 23 项自动测试，覆盖目录下划线、四类格式、RouteID/RequestId、完整流水号、多行堆栈、清单缺失提示、扫描回退、全量导出、旧版提示与已有网页终端功能。前端进行了 JS 语法检查，未执行浏览器截图或真实公司 Agent 联调。
