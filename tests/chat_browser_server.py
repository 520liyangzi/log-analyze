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


def main():
    with tempfile.TemporaryDirectory(prefix='logscope-browser-') as temporary:
        root = Path(temporary)
        model = FakeModel()
        model.replies = [response('先根据接口定位异常请求。', [('search_logs', {'endpoint': '/api/model/map', 'status': '5xx'})]),
                         response('## 排查结论\n接口出现 HTTP 500，耗时 3051 ms。\n\n- 已通过日志索引定位异常请求。\n- 需要查看同 Pod 的异常堆栈，以确认根因。\n\n**时间关联本身不能证明代码根因。**'),
                         response('继续查看上下文可以验证该请求是否受下游连接池影响。')]
        server = make_server(root / 'data', 8879)
        old = server.store.submit(create_demo(root / 'old.zip'), '过期示例.zip')
        server.store.submit(create_demo(root / 'demo.zip'), 'demo.zip')
        server.store.pool.shutdown(wait=True)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=72)
        with server.store.connect() as db:
            db.execute('UPDATE datasets SET completed_at=? WHERE id=?', ((cutoff - dt.timedelta(hours=1)).isoformat(), old))
        result = server.store.expire_indexes(cutoff)
        assert result['expired_ids'] == [old] and not result['deferred'], result
        server.chats.config.path.write_text(json.dumps(dict(DEFAULT_CONFIG, base_url=model.url,
                                                           api_key='fake-secret-123', model='test')), 'utf-8')
        try:
            print('Browser fixture ready: http://127.0.0.1:8879', flush=True)
            server.serve_forever()
        finally:
            server.server_close()
            model.close()


if __name__ == '__main__':
    main()
