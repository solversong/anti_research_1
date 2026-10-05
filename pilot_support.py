"""Local serialization only. This package never treats drafts as clinical gold."""
import hashlib
import json
import os
from pathlib import Path

class ContractError(ValueError): pass

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()

def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024), b''): h.update(part)
    return h.hexdigest()

def private_write(path, value):
    fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd,'wb') as f: f.write((json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode())
