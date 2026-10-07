"""Local-only fake-model fixture for native chat browser regression. No external API."""
import json
import datetime as dt
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_client import DEFAULT_CONFIG
from app import make_server
from demo import create_demo
from test_chat import FakeModel, response
from chat_git_fixture import LocalGitFixture


def main():
    with tempfile.TemporaryDirectory(prefix='logscope-browser-') as temporary:
        root = Path(temporary)
        repository = LocalGitFixture(root / 'git')
        fixture_path = Path('test-results') / 'chat-git-fixture.json'
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(json.dumps(dict(control_url=repository.url)), 'utf-8')
        model = FakeModel()
        model.replies = [response('先根据接口定位异常请求。', [('search_logs', {'endpoint': '/api/model/map', 'status': '5xx'})]),
                         response('## 排查结论\n接口出现 HTTP 500，耗时 3051 ms。\n\n- 已通过日志索引定位异常请求。\n- 需要查看同 Pod 的异常堆栈，以确认根因。\n\n**时间关联本身不能证明代码根因。**'),
                         response('继续查看上下文可以验证该请求是否受下游连接池影响。'),
                         response('读取本轮固定版本的代码。', [('project_read', {'path': 'Service.java'})]),
                         response('代码版本已固定，Service.java 中 browser-code-v1 是本轮代码证据。')]
        server = make_server(root / 'data', 8879)
        old = server.store.submit(create_demo(root / 'old.zip'), '过期示例.zip')
        server.store.submit(create_demo(root / 'demo.zip'), 'demo.zip')
        # Wait behind both imports while leaving the worker alive for UI deletes.
        server.store.pool.submit(lambda: None).result(timeout=30)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=72)
        # Simulate an archive retained by an older LogScope version. The current
        # expiration policy removes ZIPs too, so this is deliberately legacy data.
        with server.store.connect() as db:
            version = db.execute('SELECT index_version FROM datasets WHERE id=?', (old,)).fetchone()[0]
            db.execute(f'DELETE FROM {server.store.fts_table(version)} WHERE rowid IN '
                       '(SELECT id FROM logs WHERE dataset=?)', (old,))
            db.execute('DELETE FROM logs WHERE dataset=?', (old,))
            db.execute('DELETE FROM files WHERE dataset=?', (old,))
            db.execute("UPDATE datasets SET completed_at=?,expired_at=?,state='expired' WHERE id=?",
                       ((cutoff - dt.timedelta(hours=1)).isoformat(), dt.datetime.now(dt.timezone.utc).isoformat(), old))
        server.chats.config.path.write_text(json.dumps(dict(DEFAULT_CONFIG, base_url=model.url,
                                                           api_key='fake-secret-123', model='test')), 'utf-8')
        try:
            print('Browser fixture ready: http://127.0.0.1:8879', flush=True)
            server.serve_forever()
        finally:
            server.server_close()
            model.close()
            repository.close()


if __name__ == '__main__':
    main()
