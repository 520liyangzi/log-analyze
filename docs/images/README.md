# README 截图来源

这些图片是 LogScope 真实页面的浏览器截图。日志由仓库中的 `demo.py` 生成，项目仓库也在临时目录中生成；没有使用真实业务日志、私有仓库或模型凭据。

| 文件 | 展示内容 |
|---|---|
| `search.png` | 查询 `/api/model/map` 的 access 日志，筛选 `5xx`，定位 HTTP 500 和 3051 ms 请求 |
| `import-layout.png` | 上传嵌套 ZIP 后核对目录、日志归属与文件匹配规则 |
| `trace.png` | 流水号 `9124859898865451127` 的 6 条匹配记录和异常堆栈 |
| `ai-chat.png` | 同一会话中的两轮问题、真实索引工具查询和模拟模型回复 |
| `project-settings.png` | 在项目代码设置弹窗中选择本机生成的 Git 仓库和远程分支 |

AI 图片中标有“演示：模拟模型回复”。它用于说明交互过程，不是对真实模型能力的评测；项目设置中的 `127.0.0.1` 地址是专门为截图启动的本机演示 Git 服务。

## 重新生成

需要 Python 3.10+、Git、Node.js、Playwright 和 Chromium。在仓库根目录先安装浏览器依赖：

```bash
npm install --no-save --package-lock=false playwright@1.62.1
npx playwright install --with-deps chromium
```

第一个终端启动独立演示服务：

```bash
python tests/docs_screenshot_server.py
```

第二个终端执行浏览器操作和截图：

```bash
node tests/docs_screenshots.cjs
```

输出在 `test-results/docs/`。将上述五张 PNG 更新到本目录即可；不要复制 fixture 元数据或临时数据。脚本调用的模型、Git 和 LogScope 服务均位于本机回环地址，浏览器通过真实上传、确认、搜索和发送按钮操作页面。完成后关闭第一个终端中的演示服务。

GitHub 的 `Build Windows EXE` 工作流也会自动执行截图步骤，图片位于 `native-chat-browser` 构建产物的 `docs/` 目录。
