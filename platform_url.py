"""Validate the platform origin passed to the external log collector."""
import re
from urllib.parse import urlsplit


PLATFORM_URL_EXAMPLE = 'https://192.0.2.10:31945'


def validate_platform_url(value):
    """Require an explicit port and preserve the address as entered (trimmed)."""
    url = str(value or '').strip()
    if not url:
        raise ValueError('请填写平台地址，例如 ' + PLATFORM_URL_EXAMPLE)
    if len(url) > 2000:
        raise ValueError('平台地址最多 2000 个字符')
    if '\\' in url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError('平台地址中不能含空格、换行或反斜杠，例如 ' + PLATFORM_URL_EXAMPLE)
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError('平台地址格式不正确，请填写 http:// 或 https://主机:端口，例如 ' + PLATFORM_URL_EXAMPLE) from None
    if parsed.scheme not in ('http', 'https') or not hostname:
        raise ValueError('平台地址格式不正确，请填写 http:// 或 https://主机:端口，例如 ' + PLATFORM_URL_EXAMPLE)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('平台地址只填写协议、主机和端口；用户名和密码请填在各自的输入框中')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('端口必须是 1–65535 之间的整数，例如 :31945') from None
    if port is None:
        raise ValueError('平台地址缺少端口，请在主机后补上实际端口，例如 ' + PLATFORM_URL_EXAMPLE)
    if not 1 <= port <= 65535 or not re.fullmatch(r'[0-9]+', parsed.netloc.rsplit(':', 1)[-1]):
        raise ValueError('端口必须是 1–65535 之间的整数，例如 :31945')
    if parsed.path == '/':
        raise ValueError('平台地址末尾不要加 /，请删除端口后面的 /，例如 ' + PLATFORM_URL_EXAMPLE)
    if parsed.path or '?' in url or '#' in url:
        raise ValueError('平台地址只填写到端口，后面不要加 /、路径、查询参数或 #，例如 ' + PLATFORM_URL_EXAMPLE)
    return url
