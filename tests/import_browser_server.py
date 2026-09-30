"""Generated ZIP fixture and isolated HTTP server for import-preview browser tests.

Uses no private logs, model endpoint, or credentials. The browser uploads the ZIP
through the real HTTP API; this server intentionally starts with an empty store.
"""
import io
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import make_server
from demo import root_line


def create_import_fixture(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    inner = io.BytesIO()
    prefix = 'prod_legacy-pod/legacy-service/legacy-pod-legacy-service/log/'
    excluded = 'prod_excluded-pod/excluded-service/excluded-pod-excluded-service/log/'
    with zipfile.ZipFile(inner, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + 'root.log', root_line(message='browser-import-legacy') + '\n')
        archive.writestr(excluded + 'root.log', root_line(message='browser-import-excluded') + '\n')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('node-browser.zip', inner.getvalue())
        for name, marker in [('root.log', 'custom-log'), ('root', 'extensionless-root'),
                             ('stdout', 'stdout'), ('special.abc', 'special-abc')]:
            archive.writestr('custom/logs/' + name,
                             root_line(message='browser-import-' + marker) + '\n' +
                             ('browser-import-plain-line\n' if name == 'special.abc' else ''))
        archive.writestr('custom/logs/readme.txt', 'browser-import-unselected-text\n')
    return target


def main():
    fixture = create_import_fixture(Path(__file__).resolve().parents[1] / 'test-results' / 'import-fixture.zip')
    with tempfile.TemporaryDirectory(prefix='logscope-import-browser-') as temporary:
        server = make_server(Path(temporary) / 'data', 8880)
        try:
            print('Import browser fixture: ' + str(fixture), flush=True)
            print('Import browser server ready: http://127.0.0.1:8880', flush=True)
            server.serve_forever()
        finally:
            server.server_close()
            server.store.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
