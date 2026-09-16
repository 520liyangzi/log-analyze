"""Small model transport. Credentials never enter chat state or browser responses."""
import http.client
import json
import socket
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
    'max_tool_rounds': 8,
    'max_context_chars': 100000,
    'default_project_path': r'D:\project\mate\FMEMateService',
}


class Cancelled(Exception):
    pass


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
        try:
            value = json.loads(self.path.read_text('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError()
            config = {**DEFAULT_CONFIG, **value}
            if config['provider'] not in ('openai', 'anthropic'):
                raise ValueError()
            if config['openai_token_parameter'] not in ('max_tokens', 'max_completion_tokens'):
                raise ValueError()
            for key in ('base_url', 'api_key', 'model', 'default_project_path'):
                if not isinstance(config[key], str) or '\n' in config[key] or '\r' in config[key]:
                    raise ValueError()
            if not isinstance(config['stream'], bool):
                raise ValueError()
            for key, low, high in (('timeout_seconds', 5, 600), ('max_output_tokens', 256, 16000),
                                   ('max_tool_rounds', 1, 20), ('max_context_chars', 20000, 500000)):
                if type(config[key]) is not int or not low <= config[key] <= high:
                    raise ValueError()
            parsed = urlsplit(config['base_url'])
            if config['base_url'] and (parsed.scheme not in ('http', 'https') or not parsed.hostname
                                       or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError()
            configured = all(config[key].strip() for key in ('base_url', 'api_key', 'model'))
        except (OSError, ValueError, TypeError):
            raise ValueError('本机 data/ai-config.json 格式或参数无效，请由维护者检查；配置内容不会返回页面。') from None
        if required and not configured:
            raise ValueError('模型尚未配置，请维护者填写本机 data/ai-config.json 的 base_url、api_key 和 model。')
        return config

    def public(self):
        try:
            config = self.load(False)
            configured = all(config[key].strip() for key in ('base_url', 'api_key', 'model'))
            return dict(configured=configured, default_project_path=config['default_project_path'],
                        message='共享模型已配置' if configured else '请维护者填写本机 data/ai-config.json；页面不提供密钥查看或编辑。')
        except ValueError as exc:
            return dict(configured=False, default_project_path=DEFAULT_CONFIG['default_project_path'], message=str(exc))


class ModelClient:
    def __init__(self, config, stop):
        self.config = config
        self.stop = stop
        self.connection = None
        self.socket = None
        self.lock = threading.Lock()

    def cancel(self):
        self.stop.set()
        with self.lock:
            connection = self.connection
            active_socket = self.socket
        if connection:
            try:
                if active_socket:
                    active_socket.shutdown(socket.SHUT_RDWR)
                elif connection.sock:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:
                pass

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
        factory = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        connection = factory(parsed.hostname, parsed.port, timeout=timeout)
        with self.lock:
            self.connection = connection
        text, calls, finish = '', {}, None
        started = time.monotonic()

        def add_text(delta):
            nonlocal text
            text += delta
            # Redact the accumulated text, not individual chunks (keys can span chunks).
            on_text(self.clean(text))

        try:
            if self.stop.is_set():
                raise Cancelled()
            body = json.dumps(self.payload(system, messages, tools), ensure_ascii=False).encode('utf-8')
            connection.request('POST', path, body=body, headers=headers)
            with self.lock:
                self.socket = connection.sock
            if self.stop.is_set():
                raise Cancelled()
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f'模型接口返回 HTTP {response.status}。请维护者检查配置、额度和工具调用支持；服务端原始错误不回显。')
            if 'text/event-stream' not in response.getheader('Content-Type', ''):
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('模型单次响应超过大小限制')
                data = json.loads(raw)
                if anthropic:
                    for block in data.get('content', []):
                        if block['type'] == 'text':
                            add_text(block['text'])
                        elif block['type'] == 'tool_use':
                            calls[len(calls)] = dict(id=block['id'], type='function', function=dict(name=block['name'], arguments=json.dumps(block['input'])))
                    finish = data.get('stop_reason')
                else:
                    choice = data['choices'][0]
                    add_text(choice['message'].get('content') or '')
                    calls = dict(enumerate(choice['message'].get('tool_calls') or []))
                    finish = choice.get('finish_reason')
            else:
                size = 0
                for line in response:
                    if self.stop.is_set():
                        raise Cancelled()
                    size += len(line)
                    if size > 2 * 1024 * 1024 or time.monotonic() - started > timeout:
                        raise ValueError('模型响应超过时间或大小限制，请重试或缩小问题范围')
                    if not line.startswith(b'data:'):
                        continue
                    payload = line[5:].strip()
                    if payload == b'[DONE]':
                        break
                    if not payload:
                        continue
                    data = json.loads(payload)
                    if data.get('error') or data.get('type') == 'error':
                        raise ValueError('模型流式响应报告错误，请稍后重试；原始错误不回显。')
                    if anthropic:
                        kind = data.get('type')
                        if kind == 'content_block_start' and data['content_block']['type'] == 'tool_use':
                            block = data['content_block']
                            calls[data['index']] = dict(id=block['id'], type='function', function=dict(name=block['name'], arguments=''))
                        elif kind == 'content_block_delta':
                            delta = data['delta']
                            if delta['type'] == 'text_delta':
                                add_text(delta['text'])
                            elif delta['type'] == 'input_json_delta':
                                calls[data['index']]['function']['arguments'] += delta['partial_json']
                        elif kind == 'message_delta':
                            finish = data['delta'].get('stop_reason') or finish
                    else:
                        for choice in data.get('choices', []):
                            if choice.get('index', 0) != 0:
                                continue
                            delta = choice.get('delta', {})
                            if delta.get('content'):
                                add_text(delta['content'])
                            for fragment in delta.get('tool_calls', []):
                                call = calls.setdefault(fragment['index'], dict(id='', type='function', function=dict(name='', arguments='')))
                                call['id'] += fragment.get('id') or ''
                                for key in ('name', 'arguments'):
                                    call['function'][key] += fragment.get('function', {}).get(key) or ''
                            finish = choice.get('finish_reason') or finish
            if self.stop.is_set():
                raise Cancelled()
            if finish is None:
                raise ValueError('模型连接提前中断，已保留收到的内容，请重试')
            if len(calls) > 8:
                raise ValueError('模型一次请求了过多工具，最多允许 8 个')
            result = dict(role='assistant', content=self.clean(text))
            if calls:
                result['tool_calls'] = [calls[k] for k in sorted(calls)]
                for call in result['tool_calls']:
                    if not call.get('id') or not isinstance(call.get('function', {}).get('arguments'), str):
                        raise ValueError('模型返回的工具调用格式不正确')
                    json.loads(call['function']['arguments'] or '{}')
            return result, finish in ('length', 'max_tokens')
        except (OSError, http.client.HTTPException, KeyError, TypeError, json.JSONDecodeError):
            if self.stop.is_set():
                raise Cancelled() from None
            raise ValueError('模型连接失败、超时或返回格式不兼容，请维护者检查模型配置；地址和密钥不会回显。') from None
        finally:
            connection.close()
            with self.lock:
                self.connection = None
                self.socket = None
