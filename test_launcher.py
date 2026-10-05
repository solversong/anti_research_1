"""Independent launcher tests: invented files, mocked process/server, no inference."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import launch
import run_draft_pilot as runner

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def frozen_fixture(root):
    p = root / launch.SOURCE
    p.mkdir(parents=True)
    values={'synthetic24.json': {'cases':[], 'audit_fixture':True},
            'rules_frozen.json': json.loads((REPO/'clinical_rules_draft_v0.1.json').read_text()),
            'config.json': {'audit_fixture':True}}
    for name,value in values.items(): (p/name).write_text(json.dumps(value))
    (p/'freeze_manifest.json').write_text(json.dumps({'files_sha256':{n:sha(p/n) for n in values}}))
    binary=root/'runtime/ollama-v0.35.1/ollama'
    binary.parent.mkdir(parents=True);binary.write_text('INVENTED_NONEXECUTABLE_FIXTURE')
    return p

class LauncherIndependentTests(unittest.TestCase):
    def context(self):
        stack=contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(launch.socket,'gethostname',return_value=launch.HOST))
        stack.enter_context(patch.object(launch.signal,'signal'))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack

    def test_wrong_host_before_files_or_process(self):
        with patch.object(launch.socket,'gethostname',return_value='WRONG'), patch.object(launch.subprocess,'Popen') as pop:
            with self.assertRaises(RuntimeError):launch.execute(Path('/nonexistent_audit_sentinel'),2,False)
            pop.assert_not_called()

    def test_invalid_limit_before_files_or_process(self):
        self.context()
        with patch.object(launch.subprocess,'Popen') as pop:
            with self.assertRaises(RuntimeError):launch.execute(Path('/nonexistent_audit_sentinel'),3,False)
            pop.assert_not_called()

    def test_input_mutation_blocks_before_server(self):
        self.context()
        with tempfile.TemporaryDirectory() as td, patch.object(launch.subprocess,'Popen') as pop:
            root=Path(td);p=frozen_fixture(root);(p/'synthetic24.json').write_text('{"mutated":true}')
            with self.assertRaises(RuntimeError):launch.execute(root,2)
            pop.assert_not_called()

    def test_changed_rule_with_rehashed_fixture_still_blocked(self):
        self.context()
        with tempfile.TemporaryDirectory() as td, patch.object(launch.subprocess,'Popen') as pop:
            root=Path(td);p=frozen_fixture(root);(p/'rules_frozen.json').write_text('{"different_rule":true}')
            contract=json.loads((p/'freeze_manifest.json').read_text());contract['files_sha256']['rules_frozen.json']=sha(p/'rules_frozen.json')
            (p/'freeze_manifest.json').write_text(json.dumps(contract))
            with self.assertRaises(RuntimeError):launch.execute(root,2)
            pop.assert_not_called()

    def test_dry_run_never_starts_or_checks_server(self):
        self.context()
        with tempfile.TemporaryDirectory() as td, patch.object(launch.subprocess,'Popen') as pop, patch.object(launch,'listening') as listen, patch.object(runner,'run',return_value={'status':'DRY_RUN_ONLY','recorded_failures':0}) as run:
            root=Path(td);frozen_fixture(root)
            self.assertEqual(launch.execute(root,2,True),0)
            pop.assert_not_called();listen.assert_not_called()
            self.assertFalse(run.call_args.kwargs['execute'])
            self.assertEqual(run.call_args.args[3],'synthetic_spec')
            self.assertTrue(run.call_args.args[4].is_relative_to(root/'outputs/clinical_rules_runs'))

    def test_existing_server_not_stopped_or_reused(self):
        self.context()
        with tempfile.TemporaryDirectory() as td, patch.object(launch.subprocess,'Popen') as pop, patch.object(launch,'listening',return_value=True), patch.object(runner,'run') as run:
            root=Path(td);frozen_fixture(root)
            with self.assertRaises(RuntimeError):launch.execute(root,2)
            pop.assert_not_called();run.assert_not_called()

    def test_mocked_owned_server_local_env_and_cleanup(self):
        self.context();proc=Mock();proc.poll.return_value=None;proc.wait.return_value=0
        with tempfile.TemporaryDirectory() as td, patch.object(launch.subprocess,'Popen',return_value=proc) as pop, patch.object(launch,'listening',side_effect=[False,True]), patch.object(runner,'run',return_value={'status':'PLANNED_PREFIX_COMPLETE_CONTINUATION_PENDING','recorded_failures':0}):
            root=Path(td);frozen_fixture(root)
            self.assertEqual(launch.execute(root,2),0)
            kw=pop.call_args.kwargs
            self.assertEqual(kw['env']['OLLAMA_HOST'],'127.0.0.1:11434');self.assertEqual(kw['env']['OLLAMA_NO_CLOUD'],'1')
            self.assertEqual(kw['env']['OLLAMA_NUM_PARALLEL'],'1');self.assertTrue(kw['start_new_session'])
            proc.terminate.assert_called_once();proc.kill.assert_not_called()

    def test_interrupt_cleans_only_owned_process(self):
        self.context();proc=Mock();proc.poll.return_value=None;proc.wait.return_value=0
        with tempfile.TemporaryDirectory() as td, patch.object(launch.subprocess,'Popen',return_value=proc), patch.object(launch,'listening',side_effect=[False,True]), patch.object(runner,'run',side_effect=KeyboardInterrupt):
            root=Path(td);frozen_fixture(root)
            with self.assertRaises(KeyboardInterrupt):launch.execute(root,2)
            proc.terminate.assert_called_once()

    def test_failed_generation_exit_is_not_success(self):
        self.context()
        with tempfile.TemporaryDirectory() as td, patch.object(runner,'run',return_value={'status':'COMPLETE_ALL_ATTEMPTED','recorded_failures':1}):
            root=Path(td);frozen_fixture(root)
            self.assertEqual(launch.execute(root,2,True),1)

    def test_owned_termination_timeout_has_bounded_escalation(self):
        proc=Mock();proc.poll.return_value=None;proc.wait.side_effect=[subprocess.TimeoutExpired('fixture',10),0]
        launch.stop_owned_server(proc)
        proc.terminate.assert_called_once();proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count,2)

    def test_finished_or_absent_process_not_signalled(self):
        launch.stop_owned_server(None)
        proc=Mock();proc.poll.return_value=0;launch.stop_owned_server(proc)
        proc.terminate.assert_not_called();proc.kill.assert_not_called()

    def test_lock_blocks_second_cooperating_writer(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'lock'
            with launch.run_lock(p):
                with self.assertRaises((RuntimeError,BlockingIOError)):
                    with launch.run_lock(p):pass
            with launch.run_lock(p):pass

    def test_lock_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'target';target.write_text('INVENTED_SENTINEL')
            link=Path(td)/'lock';link.symlink_to(target)
            with self.assertRaises((OSError,RuntimeError,ValueError)):
                with launch.run_lock(link):pass
            self.assertEqual(target.read_text(),'INVENTED_SENTINEL')

if __name__=='__main__':unittest.main()
