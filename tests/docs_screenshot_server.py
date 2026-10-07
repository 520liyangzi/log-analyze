"""Real LogScope UI, generated logs and a loopback-only mock model for README images.

Run from the repository root, then run ``node tests/docs_screenshots.cjs``.
This fixture never connects to a real model or a hosted Git repository.
"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_client import DEFAULT_CONFIG
from app import make_server
from demo import create_demo, TRACE
from test_chat import FakeModel, response
from chat_git_fixture import LocalGitFixture


def main():
    output = Path('test-results/docs')
    output.mkdir(parents=True, exist_ok=True)
    create_demo(output / 'demo-logs.zip')
    with tempfile.TemporaryDirectory(prefix='logscope-docs-') as temporary:
        root = Path(temporary)
        repository = LocalGitFixture(root / 'git')
        (output / 'fixture.json').write_text(json.dumps(repository.metadata()), 'utf-8')
        model = FakeModel()
        model.replies = [
            response('', [('search_logs', {'endpoint': '/api/model/map', 'status': '5xx'})]),
            response('**演示：模拟模型回复**\n已定位到 1 条 HTTP 500：`09:55:14.158`，耗时 **3051 ms**。\n来源：`node-a / model-service-7b8d9-x2k4m / access.log`。继续结合流水号与异常堆栈核对原因。'),
            response('', [('search_logs', {'trace': TRACE})]),
            response('**演示：模拟模型回复**\n同一流水号命中 **6 条记录**。`root.log` 中出现 `HikariPool` 获取连接超时，堆栈包含 `ModelService.java:125`。\n这说明存在连接获取超时；还需核查数据库负载与连接池占用，不能只凭这一条日志断言最终根因。\n可点击下方「项目代码设置」，关联部署分支继续核对代码。'),
        ]
        server = make_server(root / 'data', 8881)
        server.chats.config.path.write_text(json.dumps(dict(
            DEFAULT_CONFIG, base_url=model.url, api_key='documentation-mock-key',
            model='documentation-mock-model')), 'utf-8')
        try:
            print('README screenshot fixture ready: http://127.0.0.1:8881', flush=True)
            server.serve_forever()
        finally:
            server.server_close()
            server.store.pool.shutdown(wait=True)
            model.close()
            repository.close()


if __name__ == '__main__':
    main()
