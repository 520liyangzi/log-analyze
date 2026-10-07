"""Small model transport. Credentials never enter chat state or browser responses."""
import http.client
import io
import json
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from analysis_rules import atomic_json


DEFAULT_CONFIG = {
    'provider': 'openai',
    'base_url': '',
    'api_key': '',
    'model': '',
    'stream': True,
    'timeout_seconds': 120,
    'max_output_tokens': 4096,
    'openai_token_parameter': 'max_tokens',
    'max_context_chars': 100000,
    'default_project_path': r'D:\project\mate\FMEMateService',
}


class Cancelled(Exception):
    pass


class _ResponseReader(io.RawIOBase):
    """Cancellable reads, including silent TLS streams and HTTP/1.0 responses.

    A short makefile() timeout cannot be retried safely after it times out.
    Instead, poll a nonblocking transport while preserving HTTPResponse's
    standard buffering, chunk decoding and content-length handling.
    """
    def __init__(self, transport, stop, deadline):
        super().__init__()
        self.transport, self.stop, self.deadline = transport, stop, deadline
        # Keep the socket alive if HTTPConnection detaches a closing response.
        # This file is only a lifetime lease; all reads happen below.
        self.lease = transport.makefile('rb', buffering=0)
        transport.setblocking(False)

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.stop.is_set():
                raise Cancelled()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Model response deadline exceeded')
            write = False
            try:
                # Read first: TLS may already have decrypted bytes buffered.
                return self.transport.recv_into(buffer)
            except ssl.SSLWantWriteError:
                write = True
            except (BlockingIOError, ssl.SSLWantReadError):
                pass
            select.select([] if write else [self.transport], [self.transport] if write else [], [], min(.2, remaining))

    def close(self):
        if not self.closed:
            self.lease.close()
        super().close()


class _ResponseSocket:
    def __init__(self, transport, stop, deadline):
        self.transport, self.stop, self.deadline = transport, stop, deadline

    def makefile(self, mode):
        return io.BufferedReader(_ResponseReader(self.transport, self.stop, self.deadline))


class ModelConfig:
    def __init__(self, directory):
        self.path = directory / 'ai-config.json'
        if not self.path.exists():
            atomic_json(self.path, DEFAULT_CONFIG)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def load(self, required=True):
        def invalid(reason):
            return ValueError(f'本机 data/ai-config.json 配置无效：{reason}；配置内容不会返回页面。')

        try:
            raw = self.path.read_text('utf-8-sig')
        except UnicodeError:
            raise invalid('文件必须使用 UTF-8 编码（支持 BOM）') from None
        except OSError:
            raise invalid('无法读取文件，请检查文件是否存在及读取权限') from None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Only positions are safe to expose: decoder messages or source lines
            # may include the address, credentials, or other configuration values.
            raise invalid(f'JSON 语法错误，第 {exc.lineno} 行、第 {exc.colno} 列；请检查双引号、逗号和路径反斜杠转义') from None
        except ValueError:
            raise invalid('JSON 数值无效，请检查数字长度和格式') from None
        if not isinstance(value, dict):
            raise invalid('JSON 顶层必须是对象')
        config = {**DEFAULT_CONFIG, **value}
        if config['provider'] not in ('openai', 'anthropic'):
            raise invalid('provider 必须是 openai 或 anthropic；兼容 OpenAI 的接口使用 openai')
        if config['openai_token_parameter'] not in ('max_tokens', 'max_completion_tokens'):
            raise invalid('openai_token_parameter 必须是 max_tokens 或 max_completion_tokens')
        for key in ('base_url', 'api_key', 'model', 'default_project_path'):
            if not isinstance(config[key], str) or '\n' in config[key] or '\r' in config[key]:
                raise invalid(f'{key} 必须是字符串，且不能包含换行或回车')
        if not isinstance(config['stream'], bool):
            raise invalid('stream 必须是 true 或 false，不能加引号')
        if type(config['max_output_tokens']) is not int or config['max_output_tokens'] <= 0:
            raise invalid('max_output_tokens 必须是正整数，不能加引号；实际支持上限由模型接口决定')
        for key, low, high in (('timeout_seconds', 5, 600), ('max_context_chars', 20000, 500000)):
            if type(config[key]) is not int or not low <= config[key] <= high:
                raise invalid(f'{key} 必须是 {low}–{high} 之间的整数，不能加引号')

        # Copy/paste often leaves normal or non-breaking spaces after /v1.
        # Normalize only the URL; never rewrite credentials or the source file.
        config['base_url'] = config['base_url'].strip()
        if config['base_url']:
            if any(char.isspace() or not char.isprintable() for char in config['base_url']):
                raise invalid('base_url 中间不能包含空白或控制字符')
            try:
                parsed = urlsplit(config['base_url'])
                port = parsed.port
            except ValueError:
                raise invalid('base_url 的主机或端口格式无效；端口必须是 1–65535 的整数') from None
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise invalid('base_url 必须是包含主机的 http:// 或 https:// 地址')
            if port is not None and not 1 <= port <= 65535:
                raise invalid('base_url 的端口必须是 1–65535 的整数')
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise invalid('base_url 不能包含用户名、密码、查询参数或片段')
            if not parsed.path.isascii():
                raise invalid('base_url 路径中的非 ASCII 字符必须先进行 URL 百分号编码')
        configured = all(config[key].strip() for key in ('base_url', 'api_key', 'model'))
        if required and not configured:
            raise ValueError('模型尚未配置，请维护者填写本机 data/ai-config.json 的 base_url、api_key 和 model。')
        return config

    def public(self):
        try:
            config = self.load(False)
            configured = all(config[key].strip() for key in ('base_url', 'api_key', 'model'))
            return dict(configured=configured, default_project_path=config['default_project_path'],
                        message='共享模型已配置（仅本地校验，连接在发送时验证）' if configured else '请维护者填写本机 data/ai-config.json；页面不提供密钥查看或编辑。')
        except ValueError as exc:
            return dict(configured=False, default_project_path=DEFAULT_CONFIG['default_project_path'], message=str(exc))


class ModelClient:
    def __init__(self, config, stop):
        self.config = config
        self.stop = stop
        self.connection = None
        self.lock = threading.Lock()

    def cancel(self):
        self.stop.set()
        # The response reader checks this flag at most every 200 ms. Only the
        # worker closes the connection, avoiding cross-thread SSL races.

    def clean(self, text):
        text = str(text)
        for secret in (self.config['api_key'], self.config['base_url']):
            if secret:
                text = text.replace(secret, '[模型配置已隐藏]')
        return text

    def payload(self, system, messages, tools):
        config = self.config
        if config['provider'] == 'openai':
            body = dict(model=config['model'], stream=config['stream'],
                        messages=[{'role': 'system', 'content': system}, *messages])
            body[config['openai_token_parameter']] = config['max_output_tokens']
            if tools:
                body['tools'] = [{'type': 'function', 'function': tool} for tool in tools]
            return body
        converted = []
        for item in messages:
            if item['role'] == 'tool':
                role, blocks = 'user', [{'type': 'tool_result', 'tool_use_id': item['tool_call_id'], 'content': item['content']}]
            else:
                role, blocks = item['role'], []
                if item.get('content'):
                    blocks.append({'type': 'text', 'text': item['content']})
                for call in item.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                                   'input': json.loads(call['function']['arguments'])})
            if blocks:
                if converted and converted[-1]['role'] == role:
                    converted[-1]['content'].extend(blocks)
                else:
                    converted.append({'role': role, 'content': blocks})
        body = dict(model=config['model'], stream=config['stream'], max_tokens=config['max_output_tokens'],
                    system=system, messages=converted)
        if tools:
            body['tools'] = [dict(name=t['name'], description=t['description'], input_schema=t['parameters']) for t in tools]
        return body

    def complete(self, system, messages, tools, on_text):
        if self.stop.is_set():
            raise Cancelled()
        parsed = urlsplit(self.config['base_url'])
        anthropic = self.config['provider'] == 'anthropic'
        suffix = '/messages' if anthropic else '/chat/completions'
        path = parsed.path.rstrip('/')
        if not path.endswith(suffix):
            path += suffix
        headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream' if self.config['stream'] else 'application/json'}
        if anthropic:
            headers.update({'x-api-key': self.config['api_key'], 'anthropic-version': '2023-06-01'})
        else:
            headers['Authorization'] = 'Bearer ' + self.config['api_key']
        timeout = self.config['timeout_seconds']
        # SSE framing can outweigh generated text. Scale the wire budget with
        # the configured output allowance; 4096 tokens keeps the original 2 MiB.
        response_limit = max(2 * 1024 * 1024, self.config['max_output_tokens'] * 512)
        # http.client connects directly: environment/system proxy settings are
        # unused rather than routing internal endpoints through a configured proxy.
        factory = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        connection = factory(parsed.hostname, parsed.port, timeout=timeout)
        with self.lock:
            self.connection = connection
        text, calls, finish = '', {}, None
        anthropic_inputs = {}
        response_phase = 'response_shape'
        started = time.monotonic()
        connection.response_class = lambda sock, *args, **kwargs: http.client.HTTPResponse(
            _ResponseSocket(sock, self.stop, started + timeout), *args, **kwargs)
        response = None

        def incompatible(code, message):
            raise ValueError(f'模型返回格式不兼容 [{code}]：{message}；地址、密钥和原始响应不会回显。') from None

        def optional(value, expected, empty, code):
            if value is None:
                return empty
            if not isinstance(value, expected):
                incompatible(code, '响应字段类型不符合当前接口协议')
            return value

        def parse_payload(raw, code):
            try:
                data = json.loads(raw)
            except UnicodeError:
                incompatible(code, '响应 JSON 编码无效，请检查模型网关返回的编码')
            except (ValueError, TypeError):
                incompatible(code, '响应不是完整有效的 JSON，请检查模型网关返回格式')
            if not isinstance(data, dict):
                incompatible(response_phase, '响应 JSON 顶层必须是对象')
            if ('error' in data and data['error'] is not None) or data.get('type') == 'error':
                incompatible('upstream_error', '模型服务返回错误，请维护者检查服务端日志、请求参数与额度')
            kind = data.get('type')
            if (data.get('object') == 'response' or (isinstance(kind, str) and kind.startswith('response.'))
                    or ('output' in data and 'choices' not in data and 'content' not in data)):
                incompatible('response_protocol', '当前不支持 Responses API，请使用 Chat Completions 或 Anthropic Messages 接口')
            anthropic_kinds = ('message', 'message_start', 'message_delta', 'message_stop',
                               'content_block_start', 'content_block_delta', 'content_block_stop', 'ping')
            if (anthropic and 'choices' in data) or (not anthropic and (kind in anthropic_kinds or 'stop_reason' in data)):
                incompatible('response_protocol', '返回协议与 provider 不一致，请检查 provider 和模型接口路径')
            return data

        def add_text(delta):
            nonlocal text
            if delta is None:
                return
            if isinstance(delta, list):
                if any(not isinstance(block, dict) or block.get('type') != 'text'
                       or not isinstance(block.get('text'), str) for block in delta):
                    incompatible(response_phase, 'content 数组仅支持明确的 text 文本块')
                delta = ''.join(block['text'] for block in delta)
            if not isinstance(delta, str):
                incompatible(response_phase, 'content 必须是文本、空值或受支持的文本块数组')
            text += delta
            # Redact the accumulated text, not individual chunks (keys can span chunks).
            on_text(self.clean(text))

        def stream_events():
            size, first_line, data_lines = 0, True, []
            while True:
                line = response.readline(response_limit - size + 1)
                if self.stop.is_set():
                    raise Cancelled()
                if not line:
                    if data_lines:
                        yield b'\n'.join(data_lines)
                    return
                size += len(line)
                if size > response_limit or time.monotonic() - started > timeout:
                    raise ValueError('模型响应超过时间或大小限制，请重试或缩小问题范围')
                if first_line:
                    line = line.removeprefix(b'\xef\xbb\xbf')
                    first_line = False
                line = line.rstrip(b'\r\n')
                if not line:
                    if data_lines:
                        yield b'\n'.join(data_lines)
                        data_lines = []
                    continue
                if line.startswith(b':'):
                    continue
                field, _, value = line.partition(b':')
                if field == b'data':
                    # SSE removes at most one separator space, then joins all
                    # data fields in the event with newlines before JSON parsing.
                    data_lines.append(value[1:] if value.startswith(b' ') else value)

        try:
            if self.stop.is_set():
                raise Cancelled()
            body = json.dumps(self.payload(system, messages, tools), ensure_ascii=False).encode('utf-8')
            connection.request('POST', path, body=body, headers=headers)
            if self.stop.is_set():
                raise Cancelled()
            response = connection.getresponse()
            if response.status != 200:
                if response.status in (401, 403):
                    reason = '认证或访问权限被拒绝，请检查 api_key、模型权限及服务端访问限制'
                elif response.status == 404:
                    reason = '接口路径不存在，请检查 base_url 与服务端的模型接口路径'
                elif response.status == 407:
                    reason = '服务入口要求代理认证；程序使用直连，请检查网关和 base_url 是否正确'
                elif response.status == 429:
                    reason = '请求频率或额度受限，请稍后重试并检查服务端配额'
                elif 500 <= response.status <= 599:
                    reason = '模型服务或网关异常，请检查服务端状态后重试'
                else:
                    reason = '请检查模型配置、接口协议和工具调用支持'
                raise ValueError(f'模型接口返回 HTTP {response.status}：{reason}；服务端原始错误不回显。')
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            # A bounded peek preserves incremental reads when a gateway labels
            # an SSE stream application/json. It never consumes or logs the body.
            prefix = response.peek(256)[:256].removeprefix(b'\xef\xbb\xbf').lstrip()
            if prefix.startswith(b'<'):
                incompatible('response_html', '接口返回了 HTML 或标记页面，请检查模型地址、登录页面或网关入口')
            is_stream = content_type == 'text/event-stream' or prefix.startswith((b'data:', b'event:', b':'))
            if not is_stream:
                raw = response.read(response_limit + 1)
                if len(raw) > response_limit:
                    raise ValueError('模型单次响应超过大小限制')
                data = parse_payload(raw, 'response_json')
                if anthropic:
                    for block in data.get('content', []):
                        if block['type'] == 'text':
                            add_text(block['text'])
                        elif block['type'] == 'tool_use':
                            calls[len(calls)] = dict(id=block['id'], type='function', function=dict(name=block['name'], arguments=json.dumps(block['input'])))
                    finish = data.get('stop_reason')
                else:
                    choice = data['choices'][0]
                    message = choice['message']
                    if message.get('function_call') is not None:
                        incompatible('tool_shape', '不支持旧式 function_call，请使用 function 类型的 tool_calls')
                    add_text(message.get('content'))
                    calls = dict(enumerate(optional(message.get('tool_calls'), list, [], 'tool_shape')))
                    finish = choice.get('finish_reason')
            else:
                response_phase = 'stream_shape'
                for payload in stream_events():
                    if payload.strip() == b'[DONE]':
                        break
                    if not payload.strip():
                        continue
                    data = parse_payload(payload, 'stream_json')
                    if anthropic:
                        kind = data.get('type')
                        if kind == 'content_block_start' and data['content_block']['type'] == 'tool_use':
                            block = data['content_block']
                            calls[data['index']] = dict(id=block['id'], type='function', function=dict(name=block['name'], arguments=''))
                            anthropic_inputs[data['index']] = block.get('input')
                        elif kind == 'content_block_delta':
                            delta = data['delta']
                            if delta['type'] == 'text_delta':
                                add_text(delta['text'])
                            elif delta['type'] == 'input_json_delta':
                                calls[data['index']]['function']['arguments'] += delta['partial_json']
                        elif kind == 'message_delta':
                            finish = data['delta'].get('stop_reason') or finish
                    else:
                        for choice in optional(data.get('choices'), list, [], 'stream_shape'):
                            if choice.get('index', 0) != 0:
                                continue
                            delta = optional(choice.get('delta'), dict, {}, 'stream_shape')
                            if delta.get('function_call') is not None:
                                incompatible('tool_shape', '不支持旧式 function_call，请使用 function 类型的 tool_calls')
                            if delta.get('content') is not None:
                                add_text(delta['content'])
                            for fragment in optional(delta.get('tool_calls'), list, [], 'stream_shape'):
                                if (not isinstance(fragment, dict) or type(fragment.get('index')) is not int
                                        or fragment['index'] < 0 or fragment.get('type') not in (None, 'function')):
                                    incompatible('tool_shape', '工具调用片段必须包含有效索引，并使用 function 类型')
                                call = calls.setdefault(fragment['index'], dict(id='', type='function', function=dict(name='', arguments='')))
                                identifier = optional(fragment.get('id'), str, '', 'tool_shape')
                                call['id'] += identifier
                                function = optional(fragment.get('function'), dict, {}, 'tool_shape')
                                for key in ('name', 'arguments'):
                                    call['function'][key] += optional(function.get(key), str, '', 'tool_shape')
                            finish = choice.get('finish_reason') or finish
            if self.stop.is_set():
                raise Cancelled()
            if finish is None:
                raise ValueError('模型连接提前中断，已保留收到的内容，请重试')
            if not isinstance(finish, str):
                incompatible(response_phase, '完成原因必须是文本')
            if finish == 'function_call' or (finish in ('tool_calls', 'tool_use') and not calls):
                incompatible('tool_shape', '模型声明了工具调用，但未返回受支持的完整工具调用')
            if calls and finish in ('length', 'max_tokens'):
                incompatible('tool_arguments', '模型输出达到长度上限，工具调用可能不完整，未执行工具；请调整输出额度后重试')
            if len(calls) > 8:
                raise ValueError('模型一次请求了过多工具，最多允许 8 个')
            result = dict(role='assistant', content=self.clean(text))
            if calls:
                result['tool_calls'] = [calls[k] for k in sorted(calls)]
                identifiers = set()
                for index, initial in anthropic_inputs.items():
                    if calls[index]['function']['arguments'] == '' and isinstance(initial, dict):
                        calls[index]['function']['arguments'] = json.dumps(initial)
                for call in result['tool_calls']:
                    if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                        incompatible('tool_shape', '工具调用缺少 function 对象')
                    function = call['function']
                    if (not isinstance(call.get('id'), str) or not call['id'].strip() or call['id'] in identifiers
                            or call.get('type') != 'function' or not isinstance(function.get('name'), str)
                            or not function['name'].strip() or not isinstance(function.get('arguments'), str)):
                        incompatible('tool_shape', '工具调用缺少有效的唯一 ID、函数名称或字符串参数')
                    identifiers.add(call['id'])
                    try:
                        arguments = json.loads(function['arguments'])
                    except (ValueError, TypeError):
                        incompatible('tool_arguments', '工具参数不是完整有效的 JSON 对象，未执行工具')
                    if not isinstance(arguments, dict):
                        incompatible('tool_arguments', '工具参数必须是 JSON 对象，未执行工具')
            return result, finish in ('length', 'max_tokens')
        except (OSError, http.client.HTTPException, KeyError, TypeError, IndexError,
                AttributeError, UnicodeError, json.JSONDecodeError) as exc:
            if self.stop.is_set():
                raise Cancelled() from None
            # Never forward exception text: network and parser errors can embed
            # private hostnames, URL components, credentials, or response bodies.
            if isinstance(exc, ssl.SSLCertVerificationError):
                reason = '模型 TLS 证书校验失败，请检查服务端证书和本机信任的证书链'
            elif isinstance(exc, ssl.SSLError):
                reason = '模型 TLS 连接失败，请检查 HTTPS 协议、端口和服务端 TLS 配置'
            elif isinstance(exc, socket.gaierror):
                reason = '模型主机名解析失败，请检查 base_url 主机名、DNS 和公司网络或 VPN'
            elif isinstance(exc, TimeoutError):
                reason = '模型直连超时，请检查服务地址、端口、公司网络或 VPN，以及 timeout_seconds'
            elif isinstance(exc, ConnectionRefusedError):
                reason = '模型直连被拒绝，请检查服务是否启动、端口和防火墙规则'
            elif isinstance(exc, (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
                reason = '模型连接被服务端或网关中断，请检查服务状态后重试'
            elif isinstance(exc, OSError):
                reason = '模型直连失败，请检查公司网络或 VPN、路由和防火墙；程序不使用系统或环境代理'
            elif isinstance(exc, http.client.HTTPException):
                reason = '模型 HTTP 通信失败，请检查 base_url 协议、端口和网关响应'
            elif isinstance(exc, UnicodeError):
                reason = '模型请求或响应编码无效，请检查 URL、密钥字符与接口响应编码'
            else:
                incompatible(response_phase, '响应结构缺少必要字段或字段类型错误，请检查 provider 和接口协议')
            raise ValueError(f'{reason}；地址、密钥和原始响应不会回显。') from None
        finally:
            if response is not None:
                response.close()
            connection.close()
            with self.lock:
                self.connection = None
