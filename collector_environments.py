"""Locally persisted credentials for the external log collection script."""
import json
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit


class CollectorEnvironments:
    def __init__(self, directory):
        self.path = Path(directory) / 'collector-environments.json'
        self.lock = threading.RLock()
        if not self.path.exists():
            self._write({'environments': []})

    def _read(self):
        try:
            value = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('采集环境配置无法读取：' + str(exc)) from None
        if not isinstance(value.get('environments'), list):
            raise ValueError('采集环境配置格式错误')
        return value

    def _write(self, value):
        temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _public(value):
        result = {key: value[key] for key in ('id', 'name', 'url', 'user')}
        result['has_password'] = bool(value.get('password'))
        return result

    def list(self):
        with self.lock:
            return [self._public(item) for item in self._read()['environments']]

    def save(self, body):
        identifier = str(body.get('id', '')).strip()
        name = str(body.get('name', '')).strip()
        url = str(body.get('url', '')).strip()
        user = str(body.get('user', '')).strip()
        password = str(body.get('password', ''))
        if not name or len(name) > 80:
            raise ValueError('环境名称不能为空且最多 80 个字符')
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or len(url) > 2000:
            raise ValueError('平台地址应为完整的 http:// 或 https:// 地址')
        if not user or len(user) > 500 or len(password) > 2000:
            raise ValueError('用户名不能为空且最多 500 字符，密码最多 2000 字符')
        with self.lock:
            state = self._read()
            current = next((item for item in state['environments'] if item['id'] == identifier), None)
            if identifier and current is None:
                raise ValueError('要修改的环境不存在')
            if any(item['name'].casefold() == name.casefold() and item['id'] != identifier
                   for item in state['environments']):
                raise ValueError('已经存在同名环境')
            if current is None:
                if not password:
                    raise ValueError('新增环境时必须填写密码')
                current = {'id': uuid.uuid4().hex}
                state['environments'].append(current)
            elif not password:
                password = current.get('password', '')
            current.update(name=name, url=url, user=user, password=password)
            self._write(state)
            return self._public(current)

    def delete(self, identifier):
        identifier = str(identifier).strip()
        with self.lock:
            state = self._read()
            remaining = [item for item in state['environments'] if item['id'] != identifier]
            if len(remaining) == len(state['environments']):
                raise ValueError('要删除的环境不存在')
            state['environments'] = remaining
            self._write(state)
            return {'ok': True, 'id': identifier}

    def resolve(self, identifier):
        with self.lock:
            item = next((item for item in self._read()['environments'] if item['id'] == str(identifier)), None)
            if item is None:
                raise ValueError('选择的采集环境不存在，请刷新环境列表')
            return {key: item[key] for key in ('url', 'user', 'password')}
