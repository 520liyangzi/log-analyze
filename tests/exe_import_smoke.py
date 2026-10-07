"""Exercise a running packaged EXE with a generated ZIP; stdlib only."""
import argparse
import io
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile


MARKER = 'packaged-exe-import-layout-smoke-marker'
MEMBER = 'custom-export/application-stream/service-session/output/events.abc'


def run(base):
    base = base.rstrip('/')

    def request(path, body=None, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        with urlopen(Request(base + path, data=data, headers={'Content-Type': content_type}), timeout=15) as response:
            return response.status, json.load(response)

    def wait_state(identifier, wanted):
        deadline = time.monotonic() + 30
        latest = None
        while time.monotonic() < deadline:
            _, rows = request('/api/datasets')
            latest = next((row for row in rows if row['id'] == identifier), None)
            if latest and latest['state'] == wanted:
                return latest
            if latest and latest['state'] == 'failed':
                raise AssertionError('Packaged import failed: ' + str(latest.get('error')))
            time.sleep(.1)
        raise AssertionError(f'Packaged import did not reach {wanted}: {latest}')

    for path, expected_type in (('/import-layout.js', 'javascript'), ('/import-layout.css', 'text/css'),
                                ('/analysis-rules.js', 'javascript')):
        with urlopen(base + path, timeout=15) as response:
            assert response.status == 200, path
            assert expected_type in response.headers['Content-Type'], path
            assert response.read(), 'Empty packaged asset: ' + path

    for path, payload in (('/terminal.js', None), ('/vendor/xterm.js', None),
                          ('/api/terminal/config', None), ('/api/terminal/start', {})):
        try:
            request(path, payload)
        except HTTPError as error:
            with error:
                assert error.code == 404, 'Retired terminal route returned ' + str(error.code) + ': ' + path
        else:
            raise AssertionError('Retired terminal route remains available: ' + path)

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MEMBER, '2026-09-30 10:00:00.000 INFO ' + MARKER + '\n')
    status, uploaded = request('/api/upload?name=exe-smoke.zip', archive_bytes.getvalue(), 'application/zip')
    assert status == 202 and uploaded['state'] == 'scanning', uploaded
    identifier = uploaded['id']
    reviewed = wait_state(identifier, 'review')
    assert reviewed['records'] == 0 and reviewed['files'] == 0, 'Scan must not create a searchable index'
    _, preview = request('/api/imports/preview?' + urlencode({'dataset': identifier}))
    assert len(preview['groups']) == 1, preview
    group = preview['groups'][0]
    assert any(file['path'] == MEMBER for file in group['files']), group
    fields = ('id', 'included', 'patterns', 'node', 'namespace', 'pod', 'service', 'kind', 'line_mode')
    selection = {field: group[field] for field in fields}
    selection.update(included=True, patterns='*.abc', node='smoke-node', namespace='smoke-ns',
                     pod='smoke-pod', service='smoke-service', kind='runtime', line_mode='lines')
    status, confirmed = request('/api/imports/confirm', dict(dataset=identifier, revision=preview['revision'],
                                groups=[selection], encoding='auto', offset='+0800', unit='ms'))
    assert status == 202 and confirmed['state'] == 'importing', confirmed
    assert wait_state(identifier, 'ready')['records'] == 1
    _, result = request('/api/search?' + urlencode({'dataset': identifier, 'q': MARKER}))
    assert result['summary']['total'] == 1, result
    record = result['rows'][0]
    assert record['node'] == 'smoke-node' and record['pod'] == 'smoke-pod', record
    assert record['kind'] == 'runtime' and record['path'] == MEMBER, record
    assert 'exe-smoke.zip' in record['source'] and MEMBER in record['source'], record
    _, evidence = request('/api/verify?' + urlencode({'id': record['id']}))
    assert evidence['verified'] and MARKER in evidence['raw'], evidence
    print('Packaged smoke passed: native assets, no terminal routes, scan-only review, edited layout, confirm, search and ZIP evidence.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Running LogScope server URL')
    run(parser.parse_args().url)
