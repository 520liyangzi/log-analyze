"""Bounded ZIP layout preview and confirmed, source-preserving import rules.

Paths in a ZIP are identifiers, never filesystem extraction destinations.  A
preview does not parse/index the full log bodies.  Nested ZIPs alone must be
streamed to temporary files so their central directories can be inspected.
"""
import codecs
import copy
import fnmatch
import gzip
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import tempfile
import zipfile


DEFAULT_PATTERNS = '*.log*,*.txt*,*.out*,*.jsonl*,messages*,syslog*,stdout*,stderr*'
MAX_ENTRIES = 100000
MAX_GROUPS = 5000
MAX_PLAN_BYTES = 24 * 1024 * 1024
SNIFF_BYTES = 1024
SAMPLE_BYTES = 4096
MAX_SAMPLE_BYTES = 1024 * 1024
STAMP = re.compile(r'^\[?(?:\d{4}-\d\d-\d\d|\d{8})[ T]\d\d:\d\d:\d\d', re.M)
EDITABLE = {'included', 'patterns', 'node', 'namespace', 'pod', 'service', 'kind', 'line_mode'}


def legacy_meta(name, chain):
    """Compatibility parser: preserve the original field order and behavior."""
    parts = PurePosixPath(name.replace('\\', '/')).parts
    indices = [i for i, part in enumerate(parts) if part == 'log']
    if not indices:
        return None
    index = indices[-1]
    if index < 3:
        return None
    namespace_pod, service = parts[index - 3:index - 1]
    pod_service = parts[index - 1]
    suffix = '-' + service
    expected_pod = pod_service[:-len(suffix)] if pod_service.endswith(suffix) else ''
    if expected_pod and namespace_pod.endswith('_' + expected_pod):
        pod = expected_pod
        namespace = namespace_pod[:-len(expected_pod) - 1]
    else:
        namespace, sep, pod = namespace_pod.rpartition('_')
        if not sep:
            pod, namespace = namespace_pod, ''
    filename = parts[-1]
    if '.log' not in filename.lower():
        return None
    kind = re.split(r'[.\-_](?=\d)|\.log', filename, maxsplit=1, flags=re.I)[0]
    return dict(node=PurePosixPath(chain[1] if len(chain) > 1 else chain[0]).stem,
                namespace=namespace, pod=pod, service=service, kind=kind,
                filename=filename, archive=' → '.join(chain), path=name,
                source=' → '.join([*chain, name]))


def _normalized(name):
    return str(PurePosixPath(name.replace('\\', '/')))


def _directory(name):
    parent = str(PurePosixPath(_normalized(name)).parent)
    return '' if parent == '.' else parent


def _kind(name):
    filename = PurePosixPath(name.replace('\\', '/')).name
    kind = re.split(r'[.\-_](?=\d)|\.(?:log|txt|out|jsonl)(?:\b|\.)', filename, maxsplit=1, flags=re.I)[0]
    return re.sub(r'\.gz$', '', kind, flags=re.I)


def infer_meta(name, chain):
    """Infer labels conservatively; uncertain labels remain editable guesses."""
    legacy = legacy_meta(name, chain)
    if legacy and _legacy_confidence(name, legacy)[0] == 'high':
        return legacy, *_legacy_confidence(name, legacy)
    parts = PurePosixPath(name.replace('\\', '/')).parts
    directory_parts = parts[:-1]
    inferred = dict(node=PurePosixPath((chain[1] if len(chain) > 1 else chain[0]).replace('\\', '/')).stem,
                    namespace='', pod='', service='', kind=_kind(name), filename=parts[-1],
                    archive=' → '.join(chain), path=name, source=' → '.join([*chain, name]))
    # The legacy directory can also contain txt/out files.  Only its labels are
    # borrowed; the real file name and its immutable source path are preserved.
    probe = legacy_meta('/'.join([*directory_parts, '__preview__.log']), chain)
    if probe and _legacy_confidence(name, probe)[0] == 'high':
        for key in ('node', 'namespace', 'pod', 'service'):
            inferred[key] = probe[key]
        confidence, reason = _legacy_confidence(name, probe)
        return inferred, confidence, reason + '；文件类型需要确认'
    for index, part in enumerate(directory_parts[:-1]):
        field = {'namespaces': 'namespace', 'namespace': 'namespace', 'pods': 'pod', 'pod': 'pod',
                 'services': 'service', 'service': 'service', 'containers': 'service', 'container': 'service'}.get(part.lower())
        if field:
            inferred[field] = directory_parts[index + 1]
    markers = [i for i, part in enumerate(directory_parts) if part.lower() in ('log', 'logs')]
    if markers:
        parents = directory_parts[:markers[-1]]
        if len(parents) >= 2 and not inferred['pod'] and not inferred['service']:
            inferred.update(service=parents[-2], pod=parents[-1])
        elif parents and not inferred['service']:
            inferred['service'] = parents[-1]
    elif directory_parts and not inferred['service']:
        inferred['service'] = directory_parts[-1]
    return inferred, 'low', '按目录名称推测；请确认节点、Pod、服务，无法确定的命名空间留空'


def _legacy_confidence(name, meta):
    parts = PurePosixPath(name.replace('\\', '/')).parts
    index = max(i for i, part in enumerate(parts) if part == 'log')
    if (parts[index - 1] == meta['pod'] + '-' + meta['service']
            and parts[index - 3] == meta['namespace'] + '_' + meta['pod']):
        return 'high', '识别到原有 namespace_pod/service/pod-service/log 目录规则'
    return 'low', '沿用旧目录规则推测，目录名称未完全匹配，请确认 Pod、命名空间和服务'


def _patterns(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError('文件匹配规则必须是 1～2048 字符的字符串')
    patterns = [part.strip() for part in value.split(',') if part.strip()]
    if not patterns or len(patterns) > 64 or any('/' in p or '\\' in p or any(ord(c) < 32 for c in p) for p in patterns):
        raise ValueError('文件匹配规则最多 64 个，使用逗号分隔文件名通配符，不要填写目录')
    return patterns


def _matches(name, patterns):
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)


def _decode(raw, encoding):
    # The byte prefix may end halfway through a valid multi-byte character.
    # Incremental decoding avoids misclassifying UTF-8 as GB18030 at that cut.
    if encoding == 'auto':
        try:
            return codecs.getincrementaldecoder('utf-8-sig')().decode(raw, final=False)
        except UnicodeDecodeError:
            return codecs.getincrementaldecoder('gb18030')(errors='replace').decode(raw, final=False)
    return codecs.getincrementaldecoder(encoding)(errors='replace').decode(raw, final=False)


def _text_sample(raw, encoding):
    if not raw:
        return True, ''
    # A log file with a misleading extension is not eligible if its initial
    # bytes are binary.  Newlines/tabs/form-feed are valid text controls.
    text = _decode(raw, encoding)
    bad = sum(ord(char) < 32 and char not in '\r\n\t\f' for char in text)
    return '\x00' not in text and bad <= max(1, len(text) // 100) and text.count('\ufffd') <= max(1, len(text) // 100), text


def scan_archive(path, name, encoding='auto', progress=None):
    if encoding not in ('auto', 'utf-8', 'gb18030'):
        raise ValueError('日志编码必须为 auto、utf-8 或 gb18030')
    if not isinstance(name, str) or not name:
        raise ValueError('压缩包名称不能为空')
    maximum = int(os.getenv('LOG_MAX_EXPANDED_GB', '20')) * 1024 ** 3
    plan = dict(version=1, groups=[], warnings=[], entries=0, bytes=0, sample_bytes=0)
    groups = {}
    metadata_bytes = 0
    started_samples = {}
    default_patterns = _patterns(DEFAULT_PATTERNS)

    def publish(current=''):
        if progress:
            progress(dict(entries=plan['entries'], groups=len(groups), bytes=plan['bytes'], current=current,
                          stage='scanning', message='正在扫描目录并读取少量样例，尚未建立索引'))

    def walk(archive, chain, depth=0):
        nonlocal metadata_bytes
        if depth > 4:
            raise ValueError('压缩嵌套层数超过 4 层')
        seen = set()
        for item in archive.infolist():
            plan['entries'] += 1
            if plan['entries'] > MAX_ENTRIES:
                raise ValueError('压缩包文件条目超过 100000，请分批上传')
            if item.is_dir():
                continue
            normalized = _normalized(item.filename)
            if normalized in seen:
                raise ValueError('压缩包包含重名成员（含斜杠等价路径），无法唯一确认来源：' + item.filename)
            seen.add(normalized)
            if item.flag_bits & 1:
                raise ValueError('暂不支持加密 ZIP')
            plan['bytes'] += item.file_size
            if plan['bytes'] > maximum:
                raise ValueError('压缩包声明的累计展开大小超过 LOG_MAX_EXPANDED_GB 限制，请分批上传')
            if item.filename.lower().endswith('.zip'):
                if depth >= 4:
                    raise ValueError('压缩嵌套层数超过 4 层')
                publish(item.filename)
                with archive.open(item) as source, tempfile.TemporaryFile() as temp:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > item.file_size or copied > maximum:
                            raise ValueError('嵌套压缩包超过展开限制')
                        temp.write(chunk)
                    temp.seek(0)
                    with zipfile.ZipFile(temp) as nested:
                        walk(nested, [*chain, item.filename], depth + 1)
                continue
            directory = _directory(item.filename)
            key = (tuple(chain), directory)
            if key not in groups:
                if len(groups) >= MAX_GROUPS:
                    raise ValueError(f'日志目录超过 {MAX_GROUPS} 个，请拆分压缩包后上传')
                meta, confidence, reason = infer_meta(item.filename, chain)
                identifier = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]
                groups[key] = dict(id=identifier, archive_chain=list(chain), directory=directory, file_count=0,
                                   bytes=0, files=[], included=False, patterns=DEFAULT_PATTERNS,
                                   **{field: meta[field] for field in ('node', 'namespace', 'pod', 'service')},
                                   kind='', line_mode='lines', confidence=confidence, reason=reason, sample='')
                started_samples[key] = 0
                plan['groups'].append(groups[key])
                metadata_bytes += len(json.dumps(groups[key], ensure_ascii=False).encode('utf-8'))
            group = groups[key]
            filename = PurePosixPath(normalized).name
            candidate_name = _matches(filename, default_patterns) and filename.lower() != 'filelist.txt'
            # Every member is lightly sniffed, so manually selecting '*' cannot
            # accidentally index an image/class/JAR as log text.
            can_sample = started_samples[key] < 3 and plan['sample_bytes'] < MAX_SAMPLE_BYTES
            read_size = SAMPLE_BYTES if can_sample else SNIFF_BYTES
            if can_sample:
                read_size = min(read_size, MAX_SAMPLE_BYTES - plan['sample_bytes'])
            failure = ''
            try:
                with archive.open(item) as source:
                    if item.filename.lower().endswith('.gz'):
                        with gzip.GzipFile(fileobj=source) as uncompressed:
                            raw = uncompressed.read(read_size)
                    else:
                        raw = source.read(read_size)
                text, sample = _text_sample(raw, encoding)
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                text, sample, failure = False, '', '无法读取压缩日志样例，请检查文件是否损坏'
            candidate = candidate_name and text
            file = dict(name=filename, path=item.filename, bytes=item.file_size, candidate=candidate, text=text)
            if failure:
                file['reason'] = failure
            elif not text:
                file['reason'] = '样例为二进制内容，无法作为文本日志导入'
            elif filename.lower() == 'filelist.txt':
                file['reason'] = '清单文件，默认不导入；可明确修改匹配规则后选择'
            elif not candidate_name:
                file['reason'] = '非常见日志文件名，可修改匹配规则后选择'
            if can_sample and text and sample:
                # Only expose bounded text samples; preserve every file's
                # metadata or fail explicitly if a plan is too large.
                file['sample'] = sample
                started_samples[key] += 1
                plan['sample_bytes'] += len(raw)
                if not group['sample'] or candidate:
                    group['sample'] = sample
            if candidate and STAMP.search(sample):
                group['line_mode'] = 'auto'
            group['files'].append(file)
            group['file_count'] += 1
            group['bytes'] += item.file_size
            group['included'] = group['included'] or candidate
            metadata_bytes += len(json.dumps(file, ensure_ascii=False).encode('utf-8'))
            if metadata_bytes > MAX_PLAN_BYTES:
                raise ValueError('目录预览清单超过 24 MB，请拆分压缩包后上传；未截断或遗漏文件')
            if plan['entries'] % 100 == 0:
                publish(item.filename)

    try:
        with zipfile.ZipFile(path) as archive:
            walk(archive, [name])
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError('ZIP 无法扫描：压缩包损坏、加密或使用了不支持的压缩算法') from exc
    if not plan['groups']:
        raise ValueError('压缩包中没有可预览的文件')
    if not any(group['included'] for group in plan['groups']):
        plan['warnings'].append('没有自动选中的常见文本日志，请查看目录和样例，勾选目录并修改文件匹配规则。')
    if any(group['confidence'] == 'low' for group in plan['groups']):
        plan['warnings'].append('部分目录的节点、Pod 和服务为推测值，请确认；原始归档链和文件路径始终保留。')
    if plan['sample_bytes'] >= MAX_SAMPLE_BYTES:
        plan['warnings'].append('样例展示已达到 1 MB 上限；文件清单完整，未展示样例的文件仍已做文本检查。')
    if len(json.dumps(plan, ensure_ascii=False).encode()) > MAX_PLAN_BYTES:
        raise ValueError('目录预览清单超过 24 MB，请拆分压缩包后上传；未截断或遗漏文件')
    publish('扫描完成')
    return plan


def validate_edits(plan, edits):
    """Apply only mutable labels/selection to the server-owned preview plan."""
    if not isinstance(edits, list) or len(edits) > MAX_GROUPS:
        raise ValueError('目录规则必须为分组修改列表')
    result = copy.deepcopy(plan)
    groups = {group['id']: group for group in result['groups']}
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get('id'), str) or edit['id'] not in groups:
            raise ValueError('目录规则包含不存在的分组')
        identifier = edit['id']
        if identifier in seen:
            raise ValueError('目录规则包含重复分组')
        seen.add(identifier)
        if set(edit) - EDITABLE - {'id'}:
            raise ValueError('归档来源、目录和文件清单不可改写；只能修改选择、匹配规则及分类标签')
        for field, value in edit.items():
            if field == 'id':
                continue
            if field == 'included':
                if not isinstance(value, bool):
                    raise ValueError('目录是否导入必须为 true 或 false')
            elif field == 'line_mode':
                if value not in ('auto', 'lines'):
                    raise ValueError('日志分行方式只能为 auto 或 lines')
            elif field == 'patterns':
                _patterns(value)
                value = value.strip()
            else:
                if not isinstance(value, str) or len(value) > 300 or any(ord(char) < 32 for char in value):
                    raise ValueError('节点、命名空间、Pod、服务和类型必须为不超过 300 字符的单行文字')
                value = value.strip()
            groups[identifier][field] = value
    return result


def build_resolver(plan):
    """Return (metadata, line_mode) only for confirmed text members.

    A resolver never trusts editable strings for source paths.  The archive
    chain and raw filename must match the server's original scan metadata.
    """
    lookup = {}
    for group in plan['groups']:
        if not group['included']:
            continue
        patterns = _patterns(group['patterns'])
        for file in group['files']:
            if not file['text'] or not _matches(file['name'], patterns):
                continue
            # fileList.txt is opt-in: broad default patterns should not turn
            # manifest contents into log records alongside actual logs.
            if file['name'].lower() == 'filelist.txt' and group['patterns'] == DEFAULT_PATTERNS:
                continue
            chain, name = group['archive_chain'], file['path']
            key = (tuple(chain), name)
            if key in lookup:
                raise ValueError('目录规则包含重复来源')
            meta = dict(node=group['node'], namespace=group['namespace'], pod=group['pod'], service=group['service'],
                        kind=group['kind'] or _kind(name), filename=file['name'], archive=' → '.join(chain),
                        path=name, source=' → '.join([*chain, name]))
            lookup[key] = (meta, group['line_mode'])

    def resolve(name, chain):
        found = lookup.get((tuple(chain), name))
        # Avoid accidental per-record parser edits mutating subsequent lookups.
        return (dict(found[0]), found[1]) if found else None

    resolve.file_count = len(lookup)
    return resolve
