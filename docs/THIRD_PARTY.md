# 第三方依赖说明

LogScope 的搜索、索引和原生 AI 服务使用 Python 标准库，运行时无需安装第三方 Python 包。页面资源随项目提供，不依赖 CDN；Windows EXE 包含运行所需资源。

结合项目代码分析需要服务电脑另行安装 Git。通过 Git URL 下载、更新和读取仓库均调用本机 Git，鉴权与公司网络访问也由服务电脑的 Git 环境提供；浏览器访问者不必安装。

EXE 构建使用 PyInstaller，浏览器回归使用 Playwright。这些是构建与测试工具，具体安装方式和版本以 `build_exe.ps1` 及 GitHub Actions 工作流为准，不是用户运行 LogScope 的前置条件。

v2.3 及更早版本的网页终端曾包含 xterm.js、addon-fit，并在 Windows 使用 pywinpty。v2.4 已移除该功能、前端资源和依赖；旧版本的组件及许可证可在对应 Git 历史中查阅。

用户自行提供的 `collect_logs.py` 和外部 AI 工具可能有独立依赖，应按各自要求安装；LogScope 不包含这些工具的源码或运行环境。
