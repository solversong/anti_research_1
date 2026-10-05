"""Local-only runtime primitives copied from audited preparation code.
No formal-reference gate is imported; no network on import.
"""
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import pilot_support as cp

def require(condition, code):
    if not condition:
        raise cp.ContractError(code)

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def no_duplicate_keys(pairs):
    value = {}
    for k, v in pairs:
        if k in value:
            raise cp.ContractError('DUPLICATE_JSON_KEY')
        value[k] = v
    return value

def strict_json(text):
    try:
        return json.loads(text, object_pairs_hook=no_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(cp.ContractError('NONFINITE_JSON')))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise cp.ContractError('INVALID_JSON') from None

def canonical_digest(value):
    require(isinstance(value, str), 'FULL_MODEL_DIGEST_REQUIRED')
    value = value.removeprefix('sha256:')
    require(re.fullmatch(r'[0-9a-f]{64}', value) is not None, 'FULL_MODEL_DIGEST_REQUIRED')
    return 'sha256:' + value

def endpoint_port(url):
    require(isinstance(url, str), 'LOCAL_ENDPOINT_REQUIRED')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise cp.ContractError('INVALID_LOCAL_ENDPOINT') from None
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.username is None
            and parsed.password is None and port is not None and 1 <= port <= 65535
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
            'ONLY_EXPLICIT_127_0_0_1_HTTP_PORT_ALLOWED')
    # Reject userinfo tricks, noncanonical numeric IP spellings and encoded authority.
    require(parsed.netloc == f'127.0.0.1:{port}', 'NONCANONICAL_LOCAL_ENDPOINT')
    return port

class PinnedTokenizer:
    """Only existing local files, no downloads/custom remote code. No model loading."""
    FILES = {'config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json',
             'added_tokens.json', 'vocab.json', 'merges.txt', 'tokenizer.model', 'chat_template.jinja'}
    def __init__(self, config):
        self.path = Path(config['tokenizer_path'])
        self.hashes = config['tokenizer_files_sha256']
        require(self.path.is_dir(), 'LOCAL_TOKENIZER_DIRECTORY_MISSING')
        require(set(self.hashes) <= self.FILES, 'UNSUPPORTED_TOKENIZER_FILE_CONTRACT')
        present = {p.name for p in self.path.iterdir() if p.is_file() and p.name in self.FILES}
        require(present == set(self.hashes), 'ALL_RELEVANT_TOKENIZER_FILES_MUST_BE_PINNED')
        # Separate template directories would need their own explicit support/hash contract.
        require(not (self.path / 'chat_templates').exists(), 'MULTI_TEMPLATE_DIRECTORY_NOT_SUPPORTED')
        self.verify()
        try:
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(str(self.path), local_files_only=True, trust_remote_code=False)
        except (ImportError, OSError, ValueError, TypeError):
            raise cp.ContractError('PINNED_LOCAL_TOKENIZER_LOAD_FAILED_NO_DOWNLOAD_ATTEMPT') from None
        self.verify()

    def verify(self):
        for name, expected in self.hashes.items():
            require(cp.file_sha(self.path / name) == canonical_digest(expected).removeprefix('sha256:'), 'TOKENIZER_FILE_HASH_MISMATCH')

    def render(self, messages):
        self.verify()
        try:
            text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            tokens = self.tokenizer.encode(text, add_special_tokens=False)
        except (ValueError, TypeError, AttributeError):
            raise cp.ContractError('TOKENIZER_CHAT_TEMPLATE_FAILED') from None
        require(isinstance(text, str) and bool(text) and len(tokens) > 0, 'EMPTY_RENDERED_PROMPT')
        return {'prompt': text, 'prompt_tokens': len(tokens)}

class LocalTransport:
    """Direct fixed loopback HTTP: no environment proxy, DNS, redirect or credentials."""
    def __init__(self, config):
        self.port = endpoint_port(config['base_url'])
        self.timeout = config['timeout_seconds']
        self.network_requests = 0
        self.generation_requests = 0

    def request(self, route, payload=None):
        require(route in ('/api/tags', '/api/generate'), 'UNSUPPORTED_LOCAL_ROUTE')
        self.network_requests += 1
        self.generation_requests += int(route == '/api/generate')
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        started = time.perf_counter()
        try:
            conn.request('GET' if payload is None else 'POST', route,
                         body=cp.canonical(payload).encode() if payload is not None else None,
                         headers={'Content-Type': 'application/json', 'Connection': 'close'})
            response = conn.getresponse()
            # No redirect following. Bound response memory; do not print error bodies.
            raw = response.read(2 * 1024 * 1024 + 1)
            require(len(raw) <= 2 * 1024 * 1024, 'LOCAL_RESPONSE_TOO_LARGE')
            return {'http_status': response.status, 'raw_body': raw.decode('utf-8', errors='replace'),
                    'elapsed_seconds': time.perf_counter() - started}
        except cp.ContractError:
            raise
        except (OSError, http.client.HTTPException):
            raise cp.ContractError('LOCAL_TRANSPORT_FAILURE_INFERENCE_COMPLETION_UNKNOWN') from None
        finally:
            conn.close()

def validate_local_model(transport, config):
    raw = transport.request('/api/tags')
    require(raw['http_status'] == 200, 'LOCAL_TAGS_HTTP_FAILURE_OR_REDIRECT')
    value = strict_json(raw['raw_body'])
    require(isinstance(value, dict) and isinstance(value.get('models'), list), 'INVALID_LOCAL_TAGS_SCHEMA')
    matches = [m for m in value['models'] if isinstance(m, dict) and config['model_name'] in (m.get('name'), m.get('model'))]
    require(len(matches) == 1, 'EXACT_LOCAL_MODEL_NOT_FOUND_OR_AMBIGUOUS')
    model = matches[0]
    def no_remote(item):
        if isinstance(item, dict):
            for k, v in item.items():
                require(not ((str(k).lower().startswith('remote') or str(k).lower().startswith('cloud')) and v), 'REMOTE_OR_CLOUD_MODEL_REJECTED')
                no_remote(v)
        elif isinstance(item, list):
            for v in item: no_remote(v)
        elif isinstance(item, str):
            require('cloud' not in item.lower() and 'remote' not in item.lower(), 'REMOTE_OR_CLOUD_MODEL_DESCRIPTION_REJECTED')
    no_remote(model)
    require(model.get('details', {}).get('format') == 'gguf' and type(model.get('size')) is int and model['size'] > 0, 'LOCAL_GGUF_METADATA_REQUIRED')
    actual = canonical_digest(model.get('digest'))
    require(actual == canonical_digest(config['model_digest']), 'PINNED_MODEL_DIGEST_MISMATCH')
    return {'model_name': config['model_name'], 'digest': actual,
            'size': model['size'], 'details': model['details'], 'verified_at': utcnow(),
            'verification': 'local tags metadata; not independent tensor attestation'}
