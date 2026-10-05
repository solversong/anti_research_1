"""Index-only guard verification in disposable local repositories; no remote."""
import contextlib, importlib.util, io, json, os, pathlib, subprocess, tempfile, unittest
ROOT=pathlib.Path(__file__).resolve().parent
if not (ROOT/'repo_guard.py').is_file():ROOT=ROOT/'repo'

def git(root,*args):
    env=dict(os.environ,GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1')
    return subprocess.run(['git','-C',str(root),*args],env=env,check=True,capture_output=True)

class IndexGuardAudit(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.p=pathlib.Path(self.t.name)
        git(self.p,'init','--quiet')
        (self.p/'repo_guard.py').write_bytes((ROOT/'repo_guard.py').read_bytes())
        self.allowed=['TRACKED_FILES.json','repo_guard.py','README.md']
        (self.p/'README.md').write_text('Invented safe fixture')
        self.write_allowlist(self.allowed)
        git(self.p,'add','--','TRACKED_FILES.json','repo_guard.py','README.md')
    def write_allowlist(self,names): (self.p/'TRACKED_FILES.json').write_text(json.dumps(names))
    def check(self):
        spec=importlib.util.spec_from_file_location('temporary_audit_guard',self.p/'repo_guard.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):return m.check()
    def test_safe_index_passes(self):self.assertEqual(self.check(),0)
    def test_unknown_file_rejected(self):
        (self.p/'forbidden.json').write_text('{"invented":true}');git(self.p,'add','forbidden.json')
        self.assertNotEqual(self.check(),0)
    def test_symlink_rejected_even_if_allowlisted(self):
        (self.p/'README.md').unlink();(self.p/'README.md').symlink_to('repo_guard.py');git(self.p,'add','README.md')
        self.assertNotEqual(self.check(),0)
    def test_possible_credential_rejected(self):
        (self.p/'README.md').write_text('sk-proj-'+'X'*40);git(self.p,'add','README.md')
        self.assertNotEqual(self.check(),0)
    def test_oversize_rejected(self):
        (self.p/'README.md').write_text('X'*250001);git(self.p,'add','README.md')
        self.assertNotEqual(self.check(),0)
    def test_unstaged_allowlist_cannot_authorize_staged_data(self):
        (self.p/'forbidden.json').write_text('{"invented":true}');git(self.p,'add','forbidden.json')
        self.write_allowlist(self.allowed+['forbidden.json'])
        self.assertNotEqual(self.check(),0)

if __name__=='__main__':unittest.main()
