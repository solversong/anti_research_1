"""Content-free process coordination and code provenance."""
from contextlib import contextmanager
import fcntl
from importlib import metadata
import os
from pathlib import Path
import subprocess
import stat

@contextmanager
def exclusive_run(parent):
    parent=Path(parent)
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(parent/'.experiment.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise RuntimeError('INVALID_RUN_LOCK_FILE')
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('RUN_ALREADY_ACTIVE_IN_OUTPUT_ROOT') from None
        yield
    finally: os.close(fd)

def code_provenance():
    root=Path(__file__).resolve().parent
    result={'git_commit':None,'git_tracked_changes':None,'dependency_versions':{}}
    try:
        def git(*args):
            return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,timeout=3,text=True).strip()
        result['git_commit']=git('rev-parse','HEAD')
        result['git_tracked_changes']=bool(git('status','--porcelain','--untracked-files=no'))
    except (OSError,subprocess.SubprocessError): pass
    for name in ('transformers','tokenizers'):
        try: result['dependency_versions'][name]=metadata.version(name)
        except metadata.PackageNotFoundError: result['dependency_versions'][name]=None
    return result
