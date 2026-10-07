import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from ai_client import Cancelled, DEFAULT_CONFIG, ModelClient, ModelConfig


class LocalEndpoint:
    """Local model or proxy trap; never forwards traffic outside the test."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.raw = None
        self.raw_content_type = 'application/json'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, body))
                response = dict(choices=[dict(message=dict(content='local model reply'), finish_reason='stop')])
                if owner.raw is not None:
                    raw, content_type = owner.raw, owner.raw_content_type
                elif body.get('stream'):
                    response['choices'][0]['delta'] = response['choices'][0].pop('message')
                    raw = ('data: ' + json.dumps(response) + '\n\ndata: [DONE]\n\n').encode()
                    content_type = 'text/event-stream'
                else:
                    raw, content_type = json.dumps(response).encode(), 'application/json'
                self.send_response(owner.status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The client deliberately closes over-budget responses.

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs=dict(poll_interval=.01), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = ModelConfig(Path(self.temp.name))
        self.values = dict(base_url='http://model.private.invalid:8538/v1',
                           api_key='unit-secret-do-not-display', model='unit-model')

    def write(self, **changes):
        values = dict(self.values, **changes)
        self.config.path.write_text(json.dumps(values, ensure_ascii=False), 'utf-8')
        return values

    def assert_private_error(self, expected):
        with self.assertRaisesRegex(ValueError, expected) as caught:
            self.config.load()
        message = str(caught.exception)
        for secret in (self.values['api_key'], self.values['base_url'], 'model.private.invalid',
                       'bad-port-private', 'unit-model'):
            self.assertNotIn(secret, message)
        public = self.config.public()
        self.assertEqual(set(public), {'configured', 'default_project_path', 'message'})
        self.assertFalse(public['configured'])
        self.assertEqual(public['message'], message)

    def test_url_normalizes_unicode_edge_whitespace_without_rewriting_file(self):
        values = dict(self.values, base_url=' \u00a0' + self.values['base_url'] + '\u00a0 \t',
                      default_project_path=r'D:\project\mate\FMEMateService')
        original = '\ufeff' + json.dumps(values, ensure_ascii=False)
        self.config.path.write_text(original, 'utf-8')
        loaded = self.config.load()
        self.assertEqual(loaded['base_url'], self.values['base_url'])
        self.assertEqual(loaded['default_project_path'], values['default_project_path'])
        self.assertEqual(loaded['api_key'], self.values['api_key'])
        self.assertEqual(self.config.path.read_text('utf-8'), original)
        self.assertTrue(self.config.public()['configured'])

    def test_internal_http_urls_ipv6_and_escaped_path_remain_supported(self):
        for url in ('http://10.0.0.1:8538/v1', 'http://[::1]:8538/v1',
                    'https://model.internal/v1/chat/completions',
                    'http://model.internal:1/%E6%A8%A1%E5%9E%8B/v1',
                    'http://model.internal:65535/v1', 'http://模型.internal/v1'):
            with self.subTest(url=url):
                self.write(base_url=url)
                self.assertEqual(self.config.load()['base_url'], url)

    def test_invalid_url_diagnostics_do_not_echo_private_values(self):
        for url in ('model.private.invalid/v1', 'ftp://model.private.invalid/v1',
                    'http://model.private.invalid:bad-port-private/v1',
                    'http://model.private.invalid:65536/v1', 'http://model.private.invalid:0/v1',
                    'http://[broken-private/v1', 'http://@model.private.invalid/v1',
                    'http://unit-secret-do-not-display@model.private.invalid/v1',
                    self.values['base_url'] + '?key=unit-secret-do-not-display',
                    self.values['base_url'] + '#unit-secret-do-not-display',
                    'http://model.private.invalid/v 1', 'http://model.private.invalid/v\u00a01',
                    'http://model.private.invalid/v\t1', 'http://model.private.invalid/v\u200b1',
                    'http://model.private.invalid/v\x001', 'http://model.private.invalid/模型'):
            with self.subTest(url=url):
                self.write(base_url=url)
                self.assert_private_error('base_url')

    def test_whitespace_only_url_is_unconfigured(self):
        self.write(base_url=' \u00a0\t')
        self.assertEqual(self.config.load(False)['base_url'], '')
        self.assertFalse(self.config.public()['configured'])
        with self.assertRaisesRegex(ValueError, '模型尚未配置'):
            self.config.load()

    def test_string_enum_and_boolean_errors_identify_only_known_fields(self):
        cases = [('provider', 'unit-secret-do-not-display'), ('provider', []),
                 ('openai_token_parameter', 'unit-secret-do-not-display'),
                 ('stream', 'true'), ('stream', 1), ('base_url', None),
                 ('api_key', 123), ('api_key', 'unit-secret-do-not-display\n'),
                 ('model', {}), ('default_project_path', 'D:\nprivate')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.write(**{field: value})
                self.assert_private_error(field)

    def test_numeric_limits_and_strict_integer_types_are_preserved(self):
        for field, low, high in (('timeout_seconds', 5, 600),
                                 ('max_tool_rounds', 1, 20), ('max_context_chars', 20000, 500000)):
            for value in (str(low), float(low), True, None, low - 1, high + 1):
                with self.subTest(field=field, value=value):
                    self.write(**{field: value})
                    self.assert_private_error(f'{field} 必须是 {low}–{high}')
            for value in (low, high):
                self.write(**{field: value})
                self.assertEqual(self.config.load()[field], value)

    def test_output_token_budget_accepts_positive_integers_without_application_ceiling(self):
        self.write()
        self.assertEqual(self.config.load()['max_output_tokens'], 4096)
        for value in (1, 255, 16000, 16001, 65536, 131072):
            with self.subTest(value=value):
                self.write(max_output_tokens=value)
                self.assertEqual(self.config.load()['max_output_tokens'], value)

    def test_output_token_budget_rejects_zero_negative_and_noninteger_values(self):
        for value in (0, -1, True, False, 1.0, 65536.0, '65536', None, [], {}):
            with self.subTest(value=value):
                self.write(max_output_tokens=value)
                self.assert_private_error('max_output_tokens.*正整数')

    def test_json_error_positions_and_windows_escape_hint_are_safe(self):
        self.config.path.write_text('{' + '\n"api_key": "unit-secret-do-not-display",\n}', 'utf-8')
        self.assert_private_error('JSON 语法错误，第 3 行、第 1 列')
        self.config.path.write_text(r'{"default_project_path":"D:\project\mate"}', 'utf-8')
        self.assert_private_error('路径反斜杠转义')

    def test_bad_encoding_top_level_and_unreadable_file_have_safe_diagnostics(self):
        self.config.path.write_text(json.dumps(self.values), 'utf-16')
        self.assert_private_error('UTF-8')
        self.config.path.write_text('[]', 'utf-8')
        self.assert_private_error('顶层必须是对象')
        self.config.path.unlink()
        self.assert_private_error('无法读取文件')

    def test_normalization_does_not_change_other_fields(self):
        self.write(api_key=' unit-secret-do-not-display ', model=' unit-model ',
                   default_project_path=' project path ')
        config = self.config.load()
        self.assertEqual(config['api_key'], ' unit-secret-do-not-display ')
        self.assertEqual(config['model'], ' unit-model ')
        self.assertEqual(config['default_project_path'], ' project path ')


class ModelConnectionDiagnosticsTests(unittest.TestCase):
    secret = 'unit-secret-do-not-display'

    def client(self, url, **changes):
        return ModelClient(dict(DEFAULT_CONFIG, base_url=url, api_key=self.secret,
                                model='unit-model', **changes), threading.Event())

    def complete(self, client):
        return client.complete('system', [dict(role='user', content='question')], [], lambda text: None)

    def assert_sanitized(self, message, url):
        self.assertNotIn(self.secret, message)
        self.assertNotIn(url, message)
        self.assertNotIn('private-error-marker', message)

    def test_normalized_local_http_request_bypasses_environment_proxy(self):
        with LocalEndpoint() as model, LocalEndpoint() as proxy, tempfile.TemporaryDirectory() as directory:
            proxy.status = 407
            config = ModelConfig(Path(directory))
            env = {key: proxy.url for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                               'http_proxy', 'https_proxy', 'all_proxy')}
            env.update(NO_PROXY='', no_proxy='')
            with mock.patch.dict(os.environ, env):
                for stream in (False, True):
                    config.path.write_text(json.dumps(dict(base_url='\u00a0 ' + model.url + ' \u00a0',
                                                          api_key=self.secret, model='unit-model', stream=stream)),
                                           'utf-8')
                    loaded = config.load()
                    result, limited = self.complete(ModelClient(loaded, threading.Event()))
                    self.assertEqual(result['content'], 'local model reply')
                    self.assertFalse(limited)
            self.assertEqual([path for path, body in model.requests], ['/v1/chat/completions'] * 2)
            self.assertEqual(proxy.requests, [])

    def test_large_output_budget_reaches_each_protocol_unchanged(self):
        with LocalEndpoint() as model, LocalEndpoint() as proxy, tempfile.TemporaryDirectory() as directory:
            config = ModelConfig(Path(directory))
            proxy.status = 407
            env = {key: proxy.url for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                               'http_proxy', 'https_proxy', 'all_proxy')}
            env.update(NO_PROXY='', no_proxy='')
            with mock.patch.dict(os.environ, env):
                for provider, parameter in (('openai', 'max_tokens'), ('openai', 'max_completion_tokens'),
                                             ('anthropic', 'max_completion_tokens')):
                    with self.subTest(provider=provider, parameter=parameter):
                        config.path.write_text(json.dumps(dict(base_url=model.url, api_key=self.secret,
                                                              model='unit-model', stream=False, provider=provider,
                                                              openai_token_parameter=parameter,
                                                              max_output_tokens=65536)), 'utf-8')
                        model.raw = (json.dumps(dict(content=[dict(type='text', text='local model reply')],
                                                    stop_reason='end_turn')).encode()
                                     if provider == 'anthropic' else None)
                        result, limited = self.complete(ModelClient(config.load(), threading.Event()))
                        self.assertEqual(result['content'], 'local model reply')
                        self.assertFalse(limited)
                        path, payload = model.requests[-1]
                        expected = 'max_tokens' if provider == 'anthropic' else parameter
                        self.assertEqual(payload[expected], 65536)
                        self.assertNotIn('max_output_tokens', payload)
                        self.assertNotIn('max_completion_tokens' if expected == 'max_tokens' else 'max_tokens', payload)
                        self.assertEqual(path, '/v1/messages' if provider == 'anthropic' else '/v1/chat/completions')
            self.assertEqual(proxy.requests, [])

    @staticmethod
    def large_sse():
        # SSE framing/metadata can exceed 2 MiB even when generated text is short.
        event = dict(choices=[dict(index=0, delta=dict(content='x'), finish_reason=None)],
                     padding='p' * 32768)
        frame = ('data: ' + json.dumps(event) + '\n\n').encode()
        end = ('data: ' + json.dumps(dict(choices=[dict(index=0, delta={}, finish_reason='stop')]))
               + '\n\ndata: [DONE]\n\n').encode()
        return frame * 65 + end

    def test_large_output_budget_accepts_more_than_two_mib_of_sse_framing(self):
        with LocalEndpoint() as model:
            model.raw, model.raw_content_type = self.large_sse(), 'text/event-stream'
            self.assertGreater(len(model.raw), 2 * 1024 * 1024)
            result, limited = self.complete(self.client(model.url, stream=True, max_output_tokens=65536))
            self.assertEqual(result['content'], 'x' * 65)
            self.assertFalse(limited)
            self.assertEqual(model.requests[-1][1]['max_tokens'], 65536)

    def test_large_output_budget_accepts_more_than_two_mib_of_json(self):
        with LocalEndpoint() as model:
            content = 'x' * (2 * 1024 * 1024 + 128)
            model.raw = json.dumps(dict(choices=[dict(message=dict(content=content), finish_reason='stop')])).encode()
            result, limited = self.complete(self.client(model.url, stream=False, max_output_tokens=65536))
            self.assertEqual(result['content'], content)
            self.assertFalse(limited)

    def test_default_output_budget_still_rejects_oversized_responses(self):
        with LocalEndpoint() as model:
            cases = [('text/event-stream', self.large_sse()),
                     ('text/event-stream', b'data: ' + b'p' * (2 * 1024 * 1024 + 128) + b'\n'),
                     ('application/json', b'p' * (2 * 1024 * 1024 + 128))]
            for content_type, raw in cases:
                with self.subTest(content_type=content_type, single_line=b'\n\n' not in raw):
                    model.raw, model.raw_content_type = raw, content_type
                    client = self.client(model.url, stream=content_type == 'text/event-stream')
                    self.assertEqual(client.config['max_output_tokens'], 4096)
                    with self.assertRaisesRegex(ValueError, '响应.*大小限制'):
                        self.complete(client)

    def test_http_status_diagnostics_hide_response_body(self):
        with LocalEndpoint() as model:
            model.raw = f'{self.secret} {model.url} private-error-marker'.encode()
            for status, expected in ((401, '认证'), (403, '权限'), (404, '接口路径'),
                                     (407, '代理认证'), (429, '额度'), (500, '服务或网关'),
                                     (503, '服务或网关'), (400, '接口协议')):
                with self.subTest(status=status):
                    model.status = status
                    with self.assertRaisesRegex(ValueError, expected) as caught:
                        self.complete(self.client(model.url, stream=False))
                    self.assertIn(f'HTTP {status}', str(caught.exception))
                    self.assert_sanitized(str(caught.exception), model.url)

    def test_network_diagnostics_hide_underlying_exception_text(self):
        private = f'{self.secret} http://model.private.invalid private-error-marker'
        cases = [(socket.gaierror(-2, private), '主机名解析失败'),
                 (TimeoutError(private), '直连超时'),
                 (ConnectionRefusedError(private), '直连被拒绝'),
                 (ConnectionResetError(private), '连接被服务端或网关中断'),
                 (ssl.SSLCertVerificationError(1, private), 'TLS 证书校验失败'),
                 (ssl.SSLError(1, private), 'TLS 连接失败'),
                 (OSError(private), '模型直连失败'),
                 (http.client.BadStatusLine(private), 'HTTP 通信失败')]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client = self.client('http://model.private.invalid/v1', stream=False)
                with mock.patch.object(http.client.HTTPConnection, 'request', side_effect=error):
                    with self.assertRaisesRegex(ValueError, expected) as caught:
                        self.complete(client)
                self.assert_sanitized(str(caught.exception), client.config['base_url'])
                self.assertIsNone(client.connection)

    def test_invalid_model_responses_have_sanitized_format_or_encoding_errors(self):
        with LocalEndpoint() as model:
            for raw, expected in ((b'private-error-marker', '返回格式不兼容'),
                                  (b'{"choices":[]}', '返回格式不兼容'),
                                  (b'{"choices":[{"message":[]}]}', '返回格式不兼容'),
                                  (b'\xffprivate-error-marker', '编码无效')):
                with self.subTest(raw=raw):
                    model.raw = raw
                    with self.assertRaisesRegex(ValueError, expected) as caught:
                        self.complete(self.client(model.url, stream=False))
                    self.assert_sanitized(str(caught.exception), model.url)

    def test_cancellation_still_takes_precedence_over_connection_failure(self):
        client = self.client('http://model.private.invalid/v1', stream=False)

        def cancelled_request(*args, **kwargs):
            client.cancel()
            raise TimeoutError('private-error-marker')

        with mock.patch.object(http.client.HTTPConnection, 'request', side_effect=cancelled_request):
            with self.assertRaises(Cancelled):
                self.complete(client)
        self.assertIsNone(client.connection)


if __name__ == '__main__':
    unittest.main()
