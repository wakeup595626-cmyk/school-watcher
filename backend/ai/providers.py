"""Official native HTTP adapters. Exactly one request; never follow redirects."""
from dataclasses import dataclass, field
import codecs
import json
import time
from urllib.parse import urlsplit
import requests
from urllib3.exceptions import (ConnectTimeoutError, NameResolutionError, NewConnectionError,
                                ProtocolError, ReadTimeoutError, SSLError)

STREAM_TOTAL_SECONDS = 600
CONNECTION_ERROR_CODES = frozenset({'dns_resolution_failed', 'connect_failed', 'connect_timeout',
    'connection_lost', 'read_timeout', 'network_result_unknown', 'response_deadline'})

ENDPOINTS = {
    'deepseek': {'default': 'https://api.deepseek.com/chat/completions'},
    'dashscope': {
        'cn-beijing': 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
        'ap-southeast-1': 'https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
        'us-east-1': 'https://dashscope-us.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
    },
    'ark': {'cn-beijing': 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'},
    'bigmodel': {'default': 'https://open.bigmodel.cn/api/paas/v4/chat/completions'},
    # 'custom' is the administrator's own OpenAI-compatible endpoint; its URL
    # comes from the saved profile instead of this fixed table.
    'custom': {'default': None},
}
# Familiar model-family labels for UI; provider IDs still select the official API.
PROVIDER_NAMES = {'deepseek': 'DeepSeek', 'dashscope': '通义千问',
                  'ark': '豆包', 'bigmodel': 'GLM', 'custom': '自定义（OpenAI 兼容）'}
DEFAULT_REGIONS = {'deepseek': 'default', 'dashscope': 'cn-beijing', 'ark': 'cn-beijing',
                   'bigmodel': 'default', 'custom': 'default'}


class ProviderError(Exception):
    def __init__(self, code, *, uncertain=False, retryable=False, request_id=None,
                 content='', usage=None, diagnostics=None):
        super().__init__(code)
        self.code, self.uncertain, self.retryable, self.request_id = code, uncertain, retryable, request_id
        self.content, self.usage, self.diagnostics = content, usage or {'known': False}, diagnostics or {}


@dataclass(frozen=True)
class ProviderResult:
    content: str
    finish_reason: str
    usage: dict
    request_id: str | None
    diagnostics: dict = field(default_factory=dict)


def endpoint_for(provider, region):
    try:
        url = ENDPOINTS[provider][region]
    except KeyError:
        raise ValueError('不支持的服务商或官方地域') from None
    if url is None:
        # Custom endpoints live on the profile; they never come from a request URL.
        raise ValueError('自定义服务使用配置中保存的接口地址')
    return url


def normalize_custom_endpoint(raw):
    """Validate and normalize a user-supplied OpenAI-compatible endpoint.

    Accepts either a base URL ('https://host/v1') or the full chat path
    ('https://host/v1/chat/completions'). Raises ValueError for anything else.
    """
    if not isinstance(raw, str):
        raise ValueError('请填写自定义接口地址')
    value = raw.strip()
    if not value or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('请填写有效的接口地址（不含空格或控制字符）')
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError('接口地址不是有效的 URL') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('接口地址必须以 http:// 或 https:// 开头并包含主机名')
    if parts.username is not None or parts.password is not None:
        raise ValueError('接口地址不能包含用户名或密码')
    if parts.query or parts.fragment:
        raise ValueError('接口地址不能包含查询参数或片段')
    if parts.hostname != parts.hostname.strip() or any(ord(c) < 33 for c in parts.netloc):
        raise ValueError('接口地址包含无效字符')
    path = (parts.path or '').rstrip('/')
    if path.endswith('/chat/completions'):
        normalized_path = path
    else:
        normalized_path = path + '/chat/completions'
    host = parts.hostname
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    netloc = host + ((':' + str(parts.port)) if parts.port else '')
    return parts.scheme + '://' + netloc + normalized_path


def request_url_for(profile):
    """Resolve the dispatch URL for a saved profile snapshot.

    Fixed providers always use the official table; the custom provider uses its
    own validated, stored address so a key can never be redirected elsewhere.
    """
    if profile.get('provider') == 'custom':
        return normalize_custom_endpoint(profile.get('base_url') or '')
    return endpoint_for(profile['provider'], profile['region'])


def _usage(payload):
    usage = payload.get('usage') or {}
    inp = usage.get('prompt_tokens', usage.get('input_tokens'))
    out = usage.get('completion_tokens', usage.get('output_tokens'))
    if not isinstance(inp, int) or not isinstance(out, int) or inp < 0 or out < 0:
        return {'known': False}
    result = {'known': True, 'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out}
    hit, miss = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
    # The cache is the provider's input-token breakdown, not a local response
    # cache or a discount to confirmed usage. Never infer zero for old records.
    if type(hit) is int and type(miss) is int and hit >= 0 and miss >= 0 and hit + miss == inp:
        result.update(cache_hit_tokens=hit, cache_miss_tokens=miss)
    return result


def _transport_evidence(exc):
    """Inspect typed causes only; exception messages can contain credentials/URLs."""
    pending, chain, seen = [exc], [], set()
    while pending and len(chain) < 16:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(value for value in (current.__cause__, current.__context__,
            getattr(current, 'reason', None), *current.args) if isinstance(value, BaseException))
    for cls, code in ((requests.ReadTimeout, 'read_timeout'), (ReadTimeoutError, 'read_timeout'),
                      (ProtocolError, 'connection_lost')):
        if any(isinstance(error, cls) for error in chain):
            return code, cls.__name__, False
    for cls, code in ((NameResolutionError, 'dns_resolution_failed'),
                      (NewConnectionError, 'connect_failed'),
                      (requests.ConnectTimeout, 'connect_timeout'),
                      (ConnectTimeoutError, 'connect_timeout')):
        if any(isinstance(error, cls) for error in chain):
            return code, cls.__name__, True
    for cls in (requests.exceptions.SSLError, SSLError, requests.ConnectionError,
                requests.Timeout, requests.RequestException):
        if isinstance(exc, cls) or any(isinstance(error, cls) for error in chain):
            return 'connection_lost', cls.__name__, False
    return 'connection_lost', 'RequestException', False


def complete(profile, api_key, messages, *, max_tokens=None, streaming=False, on_content=None, on_progress=None):
    """A fresh session avoids shared credentials/proxies and SDK retry behavior."""
    url = request_url_for(profile)
    budget = min(int(max_tokens or profile.get('max_output_tokens', 4096)), 16384)
    streaming = bool(streaming and profile['provider'] == 'deepseek')
    from backend.services.discovery_policy import get_policy
    temperature = get_policy()['ai_temperature']
    common = {'model': profile['model'], 'stream': streaming}
    if profile['provider'] == 'dashscope':
        body = {**common, 'input': {'messages': messages}, 'parameters': {
            'result_format': 'message', 'max_tokens': budget, 'temperature': temperature,
            'response_format': {'type': 'json_object'}, 'enable_thinking': False}}
    elif profile['provider'] == 'custom':
        # Third-party OpenAI-compatible gateways vary widely; response_format
        # is the most commonly rejected field, so structured output relies on
        # the Skill prompt plus the program-side JSON contract validation.
        body = {**common, 'messages': messages, 'max_tokens': budget, 'temperature': temperature}
    else:
        body = {**common, 'messages': messages, 'max_tokens': budget, 'temperature': temperature,
                'response_format': {'type': 'json_object'}}
        if profile['provider'] == 'deepseek':
            # These bounded classification/summary calls need the JSON answer;
            # default thinking can consume the output budget before it arrives.
            body['thinking'] = {'type': 'disabled'}
    if streaming:
        body['stream_options'] = {'include_usage': True}
    session = requests.Session()
    session.trust_env = False
    started = time.monotonic()
    request_id = None
    request_phase, response_received, exception_type = 'dispatch_started', False, None
    content, usage, received = '', {'known': False}, 0
    stream_read_deadline_limited = False
    def diagnostics():
        return {'elapsed_seconds': round(time.monotonic() - started, 3), 'received_bytes': received,
                'streaming': streaming, 'request_phase': request_phase,
                **({'exception_type': exception_type} if exception_type else {})}
    def failure(code):
        return ProviderError(code, uncertain=True, retryable=False, request_id=request_id,
                             content=content, usage=usage, diagnostics=diagnostics())
    try:
        with session.post(url, headers={'Authorization': 'Bearer ' + api_key,
                          'Content-Type': 'application/json'}, json=body,
                          timeout=(8, 120 if streaming else 60), allow_redirects=False, stream=True) as response:
            response_received, request_phase = True, 'response_headers'
            request_id = response.headers.get('x-request-id') or response.headers.get('x-dashscope-request-id')
            if 300 <= response.status_code < 400:
                raise ProviderError('redirect_rejected', request_id=request_id)
            if response.status_code in (401, 403):
                raise ProviderError('authentication_failed', request_id=request_id)
            if response.status_code == 402:
                raise ProviderError('budget_exhausted', request_id=request_id)
            if response.status_code == 429:
                raise ProviderError('rate_limited', retryable=True, request_id=request_id)
            if response.status_code >= 500:
                raise ProviderError('provider_unavailable', uncertain=True, request_id=request_id)
            if response.status_code != 200:
                raise ProviderError('request_rejected', request_id=request_id)
            request_phase = 'response_body'
            if streaming:
                # Bound each socket read by the remaining total allowance. This
                # also bounds a read that begins just before the total deadline.
                raw = getattr(response, 'raw', None)
                stream_socket = getattr(getattr(raw, '_connection', None), 'sock', None)
                if stream_socket is None:
                    try:
                        stream_socket = raw._fp.fp.raw._sock
                    except AttributeError:
                        pass
                def bounded_chunks():
                    nonlocal stream_read_deadline_limited
                    chunks = iter(response.iter_content(chunk_size=1))
                    while True:
                        remaining = STREAM_TOTAL_SECONDS - (time.monotonic() - started)
                        if remaining <= 0:
                            raise failure('response_deadline')
                        if stream_socket is not None:
                            stream_socket.settimeout(min(120, remaining))
                            stream_read_deadline_limited = remaining <= 120
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            return
                        yield chunk
                decoder = codecs.getincrementaldecoder('utf-8')()
                buffer, lines, finish, ended = '', [], '', False
                for chunk in bounded_chunks():
                    received += len(chunk)
                    if received > 2_000_000:
                        raise failure('response_too_large')
                    if time.monotonic() - started > STREAM_TOTAL_SECONDS:
                        raise failure('response_deadline')
                    if on_progress:
                        on_progress(diagnostics())
                    buffer += decoder.decode(chunk)
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.rstrip('\r')
                        if line:
                            if line.startswith('data:'):
                                lines.append(line[5:].lstrip())
                            continue
                        if not lines:
                            continue
                        raw, lines = '\n'.join(lines), []
                        if raw == '[DONE]':
                            ended = True
                            break
                        try:
                            payload = json.loads(raw)
                            request_id = payload.get('id') or request_id
                            if payload.get('usage'):
                                usage = _usage(payload)
                            for choice in payload.get('choices', []):
                                if choice.get('index', 0) != 0:
                                    continue
                                delta = choice.get('delta') or {}
                                value = delta.get('content') or ''
                                if not isinstance(value, str):
                                    raise ValueError('non-text content')
                                content += value
                                finish = choice.get('finish_reason') or finish
                                if value and on_content:
                                    on_content(content)
                        except (ValueError, KeyError, TypeError):
                            raise failure('invalid_stream_event') from None
                    if ended:
                        break
                if time.monotonic() - started >= STREAM_TOTAL_SECONDS:
                    raise failure('response_deadline')
                if not ended or not finish:
                    raise failure('stream_incomplete')
                return ProviderResult(content, finish, usage, request_id, diagnostics())
            data = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                data.extend(chunk)
                received += len(chunk)
                if len(data) > 2_000_000 or time.monotonic() - started > 90:
                    raise ProviderError('response_limit', uncertain=True, request_id=request_id)
            try:
                payload = json.loads(data)
                if profile['provider'] == 'dashscope':
                    if payload.get('code'):
                        raise ProviderError('request_rejected', request_id=payload.get('request_id'))
                    choice = payload['output']['choices'][0]
                else:
                    choice = payload['choices'][0]
                content = choice['message']['content']
                if not isinstance(content, str):
                    raise ValueError('non-text content')
                return ProviderResult(content=content, finish_reason=choice.get('finish_reason') or '',
                                      usage=_usage(payload), request_id=payload.get('request_id') or payload.get('id') or request_id,
                                      diagnostics=diagnostics())
            except (KeyError, IndexError, TypeError, ValueError):
                raise ProviderError('invalid_response', uncertain=True, request_id=request_id) from None
    except requests.RequestException as exc:
        code, exception_type, proven_before_request = _transport_evidence(exc)
        if proven_before_request and not response_received:
            request_phase = 'before_http_request'
            raise ProviderError(code, retryable=True,
                usage={'known': True, 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
                diagnostics=diagnostics()) from None
        # Even no received bytes and a very short elapsed time cannot prove
        # that the server did not receive/charge the request.
        if proven_before_request:
            code = 'connection_lost'
        # Windows socket timers may fire just before the monotonic deadline.
        # Attribute a timed-out read to the limit actually applied to that
        # socket, rather than mistaking this boundary for the 120s idle limit.
        if streaming and (time.monotonic() - started >= STREAM_TOTAL_SECONDS
                          or code == 'read_timeout' and stream_read_deadline_limited):
            code = 'response_deadline'
        raise failure(code if streaming else 'network_result_unknown') from None
    except UnicodeDecodeError:
        raise failure('invalid_stream_encoding') from None
    finally:
        session.close()
