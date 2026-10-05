#!/usr/bin/env python3
"""Reject staged files outside an explicit code-only list; never print contents."""
import json
from pathlib import Path
import re
import subprocess
import sys

def check():
    root=Path(__file__).resolve().parent
    def git(*args): return subprocess.check_output(['git','-C',str(root),*args])
    indexed_allowlist=git('show',':TRACKED_FILES.json')
    if indexed_allowlist != (root/'TRACKED_FILES.json').read_bytes():
        print('Commit blocked: staged and working allowlists differ.',file=sys.stderr);return 1
    allowed=set(json.loads(indexed_allowlist))
    rejected=[]
    for line in git('ls-files','--stage','-z').decode().split('\0'):
        if not line: continue
        meta,name=line.split('\t',1);mode,oid,stage=meta.split()
        if name not in allowed or mode not in ('100644','100755') or stage!='0':
            rejected.append('unapproved file/type');continue
        blob=git('cat-file','blob',oid)
        if len(blob)>250000: rejected.append('oversized file');continue
        if re.search(rb'(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)',blob):
            rejected.append('possible credential')
    if rejected:
        print('Commit blocked: code-only allowlist or credential check failed ('+str(len(rejected))+' files).',file=sys.stderr)
        return 1
    print('Code-only Git check passed; no runtime data directories tracked.')
    return 0

if __name__=='__main__': raise SystemExit(check())
