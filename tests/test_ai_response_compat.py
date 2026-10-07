"""Local HTTP regressions for real-world model response framing and diagnostics."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from ai_client import Cancelled, DEFAULT_CONFIG, ModelClient


def event(value):
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode('utf-8')


def chunk(content=None, finish=None, **delta):
    return dict(choices=[dict(index=0, delta=dict(content=content, **delta), finish_reason=finish)])


def json_reply(content='已完成', calls=None, finish='stop'):
    return dict(choices=[dict(index=0, message=dict(role='assistant', content=content,
                                                   tool_calls=calls), finish_reason=finish)])


def tool_call(**changes):
    return dict(dict(id='call_1', type='function',
                     function=dict(name='search_logs', arguments='{"q":"timeout"}')), **changes)


class WireModel:
    """Serve exactly the configured bytes; no network beyond loopback."""
    def __init__(self):
        self.raw = b''
        self.content_type = 'text/event-stream'
        self.requests = []
        self.hold_open = False
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                if owner.content_type is not None:
                    self.send_header('Content-Type', owner.content_type)
                self.end_headers()
                try:
                    self.wfile.write(owner.raw)
                    self.wfile.flush()
                    if owner.hold_open:
                        owner.release.wait(5)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs=dict(poll_interval=.01), daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ModelResponseCompatibilityTests(unittest.TestCase):
    secret = 'response-test-secret-never-display'
    private_body = 'private-upstream-response-do-not-display'

    def setUp(self):
        self.model = WireModel()
        self.addCleanup(self.model.close)
        self.texts = []

    def client(self, **changes):
        return ModelClient(dict(DEFAULT_CONFIG, base_url=self.model.url, api_key=self.secret,
                                model='fixture', **changes), threading.Event())

    def complete(self, **changes):
        self.texts = []
        return self.client(**changes).complete('system', [dict(role='user', content='question')],
                                               [], self.texts.append)

    def json_body(self, value):
        self.model.content_type = 'application/json'
        self.model.raw = json.dumps(value, ensure_ascii=False).encode('utf-8')

    def assert_diagnostic(self, code, **changes):
        with self.assertRaises(ValueError) as caught:
            self.complete(**changes)
        message = str(caught.exception)
        self.assertIn('模型返回格式不兼容', message)
        self.assertIn('[' + code + ']', message)
        for private in (self.secret, self.model.url, self.private_body):
            self.assertNotIn(private, message)
        return message

    def test_optional_null_stream_fields_do_not_discard_normal_text(self):
        cases = {
            'nullable usage choices': [dict(choices=None, usage=dict(total_tokens=4))],
            'nullable delta': [dict(choices=[dict(index=0, delta=None, finish_reason=None)])],
            'nullable tool_calls': [chunk(None, tool_calls=None)],
        }
        for name, prefix in cases.items():
            with self.subTest(field=name):
                self.model.raw = b''.join(event(item) for item in prefix) + event(chunk('正常文本', 'stop')) + b'data: [DONE]\n\n'
                result, limited = self.complete()
                self.assertEqual(result['content'], '正常文本')
                self.assertFalse(result.get('tool_calls'))
                self.assertFalse(limited)
        # A null final delta may itself carry the required terminal reason.
        self.model.raw = event(chunk('终止帧之前的文本')) + event(dict(choices=[dict(index=0, delta=None, finish_reason='stop')]))
        self.assertEqual(self.complete()[0]['content'], '终止帧之前的文本')

    def test_nullable_function_fragments_complete_only_with_valid_final_tool(self):
        fragments = [dict(index=0, id='call_1', type='function', function=None),
                     dict(index=0, id=None, function=dict(name='search_logs', arguments='{"q":')),
                     dict(index=0, function=None),
                     dict(index=0, function=dict(name=None, arguments='"timeout"}'))]
        self.model.raw = b''.join(event(chunk(None, tool_calls=[fragment])) for fragment in fragments)
        self.model.raw += event(dict(choices=[dict(index=0, delta=None, finish_reason='tool_calls')]))
        result, limited = self.complete()
        self.assertEqual(len(result['tool_calls']), 1)
        call = result['tool_calls'][0]
        self.assertEqual((call['id'], call['type'], call['function']['name']),
                         ('call_1', 'function', 'search_logs'))
        self.assertEqual(json.loads(call['function']['arguments']), {'q': 'timeout'})
        self.assertEqual(result['content'], '')
        self.assertFalse(limited)

    def test_sse_comments_empty_events_usage_and_crlf_are_not_answers(self):
        self.model.raw = (b'\xef\xbb\xbf: heartbeat\r\n\r\nevent: ping\r\ndata:\r\n\r\n' +
                          event(dict(choices=[], usage=dict(total_tokens=9))) +
                          b': keepalive\n\n' + event(chunk('有用的回复', 'stop')) +
                          event(dict(choices=None, usage=dict(total_tokens=10))) + b'data: [DONE]\r\n\r\n')
        result, _ = self.complete()
        self.assertEqual(result['content'], '有用的回复')
        self.assertEqual(self.texts[-1], '有用的回复')

    def test_multiple_data_lines_are_joined_within_one_sse_event(self):
        self.model.raw = (b'event: message\nid: 123\n' +
                          'data: {"choices": [\n'
                          'data: {"index": 0, "delta": {"content": "多行事件回复"},\n'
                          'data: "finish_reason": "stop"}]}\n\n'
                          'data: [DONE]\n\n'.encode('utf-8'))
        result, limited = self.complete()
        self.assertEqual(result['content'], '多行事件回复')
        self.assertFalse(limited)

    def test_sse_body_is_detected_when_gateway_labels_it_json_or_plain_text(self):
        self.model.raw = b': gateway heartbeat\n\n' + event(chunk('按实际内容解析', 'stop')) + b'data: [DONE]\n\n'
        for content_type in ('application/json', 'text/plain; charset=utf-8', None):
            for requested_stream in (True, False):
                with self.subTest(content_type=content_type, stream=requested_stream):
                    self.model.content_type = content_type
                    result, limited = self.complete(stream=requested_stream)
                    self.assertEqual(result['content'], '按实际内容解析')
                    self.assertFalse(limited)

    def test_known_json_text_blocks_are_concatenated_without_repr(self):
        self.json_body(json_reply([dict(type='text', text='第一段'), dict(type='text', text='\n第二段')]))
        for requested_stream in (False, True):
            with self.subTest(stream=requested_stream):
                result, limited = self.complete(stream=requested_stream)
                self.assertEqual(result['content'], '第一段\n第二段')
                self.assertEqual(self.texts[-1], result['content'])
                self.assertFalse(limited)

    def test_unknown_or_malformed_content_blocks_are_not_silently_ignored(self):
        for content in ([dict(type='image_url', image_url=self.private_body)],
                        [dict(type='text', text={'secret': self.secret})],
                        [None], [dict(type='text', text='known'), dict(type='unknown', text=self.private_body)]):
            with self.subTest(content=content):
                self.json_body(json_reply(content))
                self.assert_diagnostic('response_shape', stream=False)

    def test_html_json_and_protocol_errors_have_distinct_safe_diagnostics(self):
        private = self.secret + self.model.url + self.private_body
        cases = [
            ('response_html', 'text/html', ('<!doctype html><html><body>' + private + '</body></html>').encode()),
            ('response_json', 'application/json', ('{"choices": [' + private).encode()),
            ('response_protocol', 'application/json', json.dumps(dict(type='message',
                content=[dict(type='text', text=private)], stop_reason='end_turn')).encode()),
            ('response_protocol', 'application/json', json.dumps(dict(object='response', status='completed',
                output=[dict(type='message', content=[dict(type='output_text', text=private)])])).encode()),
            ('response_shape', 'application/json', json.dumps(dict(choices='invalid-' + private)).encode()),
        ]
        for code, content_type, raw in cases:
            with self.subTest(code=code, raw=raw[:40]):
                self.model.raw, self.model.content_type = raw, content_type
                self.assert_diagnostic(code, stream=False)

    def test_http_200_error_envelopes_are_not_empty_successful_answers(self):
        error = dict(message=self.private_body + self.secret + self.model.url, type='server_error')
        for stream in (False, True):
            with self.subTest(stream=stream):
                if stream:
                    self.model.content_type = 'text/event-stream'
                    self.model.raw = event(dict(error=error))
                else:
                    self.json_body(dict(error=error))
                self.assert_diagnostic('upstream_error', stream=stream)

    def test_sse_invalid_json_and_wrong_types_are_not_nullable_fields(self):
        self.model.raw = ('data: {"choices":' + self.private_body + self.secret + '\n\n').encode()
        self.assert_diagnostic('stream_json')
        for data in (dict(choices='not-an-array'), dict(choices=[dict(delta='not-an-object')]),
                     chunk(None, tool_calls='not-an-array')):
            with self.subTest(data=data):
                self.model.raw = event(data) + event(chunk('must not succeed', 'stop'))
                self.assert_diagnostic('stream_shape')

    def test_bad_tool_arguments_are_rejected_before_any_tool_can_execute(self):
        for arguments in ('{"q":' + self.private_body, '[]', 'null', 'true', '42', '"string"'):
            with self.subTest(arguments=arguments):
                self.json_body(json_reply(None, [tool_call(function=dict(name='search_logs', arguments=arguments))], 'tool_calls'))
                self.assert_diagnostic('tool_arguments', stream=False)
        for finish in ('length', 'max_tokens'):
            with self.subTest(truncated_finish=finish):
                # One closed argument object does not make a truncated tool list safe.
                self.json_body(json_reply(None, [tool_call()], finish))
                self.assert_diagnostic('tool_arguments', stream=False)

    def test_null_fragments_do_not_relax_final_tool_shape_validation(self):
        for call in (tool_call(id=''), tool_call(function=None),
                     tool_call(function=dict(name='', arguments='{}')),
                     tool_call(function=dict(name='search_logs', arguments={'q': self.secret})),
                     tool_call(type='unsupported')):
            with self.subTest(call=call):
                self.json_body(json_reply(None, [call], 'tool_calls'))
                self.assert_diagnostic('tool_shape', stream=False)

    def test_broken_frame_retains_already_received_text_without_echoing_body(self):
        self.model.raw = event(chunk('已收到的部分内容')) + ('data: ' + self.private_body + '\n\n').encode()
        self.assert_diagnostic('stream_json')
        self.assertTrue(self.texts)
        self.assertEqual(self.texts[-1], '已收到的部分内容')

    def test_stream_without_finish_reason_is_not_reported_as_success(self):
        self.model.raw = event(chunk('尚未完成的回复')) + event(dict(choices=None, usage=dict(total_tokens=2)))
        with self.assertRaisesRegex(ValueError, '中断|结束|完成'):
            self.complete()
        self.assertEqual(self.texts[-1], '尚未完成的回复')

    def test_cancel_interrupts_silent_stream_after_nullable_frames_and_text(self):
        self.model.raw = event(dict(choices=None, usage=dict(total_tokens=1))) + event(chunk('取消前的内容', tool_calls=None))
        # A mislabeled stream must be parsed live, not buffered until disconnect.
        self.model.content_type = 'application/json'
        self.model.hold_open = True
        client = self.client()
        received = threading.Event()
        errors = []

        def on_text(text):
            self.texts.append(text)
            if text:
                received.set()

        def run():
            try:
                client.complete('system', [dict(role='user', content='question')], [], on_text)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(received.wait(2), 'normal text after nullable frames must reach the UI while the stream is open')
            started = time.monotonic()
            client.cancel()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), 'cancel must interrupt a silent response without waiting for EOF')
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], Cancelled)
            self.assertEqual(self.texts[-1], '取消前的内容')
            self.assertIsNone(client.connection)
        finally:
            self.model.release.set()
            client.cancel()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
