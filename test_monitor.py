import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from progress import Progress
import monitor


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def new(self, name='run', **kwargs):
        args = dict(model='qwen3.5:27b', version='v0.2', total=2, heartbeat_seconds=0.02)
        args.update(kwargs)
        return Progress(self.root / name, **args)

    def test_counts_prefix_and_reuse_are_distinct(self):
        with self.new(total=2, dataset_total=24, reused_records=2) as p:
            p.start_item(1)
            p.finish_item(success=True)
            p.start_item(2)
            p.finish_item(success=False)
            p.finish('planned_prefix_complete')
        value = monitor.read_snapshot(self.root / 'run')
        self.assertEqual((value['new_attempts'], value['completed'], value['failed'], value['reused_records'], value['dataset_total']), (2, 2, 1, 2, 24))
        self.assertEqual(value['eta_sample_count'], 2)

    def test_plan_resolution_before_dispatch_only(self):
        with self.new(total=24) as p:
            p.set_plan(total=2, dataset_total=24, reused_records=2)
            p.set_stage('tokenizer')
            p.start_item(1)
            with self.assertRaises(RuntimeError):
                p.set_plan(total=24, dataset_total=24)
            p.finish_item(success=True)
            p.finish('interrupted')
        self.assertEqual(monitor.read_snapshot(self.root / 'run')['total'], 2)

    def test_atomic_reader_under_heartbeat_and_terminal_race(self):
        failures = []
        stop = threading.Event()
        with self.new(total=1) as p:
            def read():
                while not stop.is_set():
                    try:
                        monitor.read_snapshot(self.root / 'run')
                    except Exception as exc:
                        failures.append(type(exc).__name__)
            reader = threading.Thread(target=read)
            reader.start()
            p.start_item(1)
            time.sleep(0.06)
            p.finish_item(success=True)
            final = p.finish('completed')
            time.sleep(0.06)
            stop.set()
            reader.join(timeout=2)
        self.assertEqual(failures, [])
        value = monitor.read_snapshot(self.root / 'run')
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(value['sequence'], final['sequence'])
        events = [json.loads(line) for line in (self.root / 'run' / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'finished')
        self.assertEqual([x['sequence'] for x in events], sorted({x['sequence'] for x in events}))

    def test_process_lock_rejects_second_writer(self):
        with self.new() as p:
            code = "from progress import Progress\ntry:\n Progress(__import__('sys').argv[1],model='qwen3.5:27b',version='v1',total=1)\nexcept RuntimeError as e:\n print(str(e))\nelse:\n raise SystemExit(5)"
            result = subprocess.run([sys.executable, '-c', code, str(self.root/'run')], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'PROGRESS_WRITER_ALREADY_ACTIVE')
            p.finish('interrupted')

    def test_no_existing_progress_overwrite(self):
        with self.new() as p:
            p.finish('dry_run_complete')
        original = (self.root/'run'/'progress.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.new()
        self.assertEqual((self.root/'run'/'progress.json').read_bytes(), original)

    def test_permissions_and_no_arbitrary_content(self):
        with self.new() as p:
            p.finish('dry_run_complete')
        for name in ('progress.json', 'events.jsonl', '.progress.lock'):
            self.assertEqual(stat.S_IMODE((self.root/'run'/name).stat().st_mode), 0o600)
        snapshot = monitor.read_snapshot(self.root/'run')
        line = monitor.render_snapshot(snapshot)
        self.assertNotIn(str(self.root), line)
        self.assertNotIn('\x1b', line)
        self.assertEqual(len(line.splitlines()), 2)
        self.assertTrue(snapshot['run_id'].isascii())
        snapshot['patient'] = 'forbidden'
        with self.assertRaises(monitor.InvalidProgress):
            monitor.render_snapshot(snapshot)

    def test_reject_terminal_injection_and_model_paths(self):
        for key, value in [('run_id', 'a\x1b[31m'), ('run_id', '../patient'), ('model', '/Users/private/model'), ('version', 'v1\npatient')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.new(**{key:value})

    def test_incomplete_request_preserved_on_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.new() as p:
                p.start_item(1)
                raise KeyboardInterrupt
        value = monitor.read_snapshot(self.root/'run')
        self.assertEqual((value['status'],value['new_attempts'],value['completed'],value['outcome_unrecorded']), ('interrupted',1,0,1))
        self.assertIsNotNone(value['current_request_age_seconds'])

    def test_completed_with_errors_not_false_success(self):
        with self.new(total=1) as p:
            p.start_item(1)
            p.finish_item(success=False)
            with self.assertRaises(ValueError):
                p.finish('completed')
        self.assertEqual(monitor.read_snapshot(self.root/'run')['status'], 'completed_with_errors')

    def test_eta_requires_observed_new_requests(self):
        with self.new(total=2, reused_records=20, dataset_total=24) as p:
            self.assertIsNone(p.snapshot()['eta_seconds_estimate'])
            p.start_item(1)
            time.sleep(0.01)
            p.finish_item(success=True)
            self.assertGreater(p.snapshot()['eta_seconds_estimate'], 0)
            p.finish('interrupted')

    def test_terminal_does_not_become_stale_or_probe_pid(self):
        with self.new() as p:
            p.finish('dry_run_complete')
        value = p.snapshot()
        state = monitor.assess(value, now=value['heartbeat_at_unix']+86400,
                               process_probe=lambda _: self.fail('terminal PID must not be probed'))
        self.assertFalse(state['heartbeat_stale'])
        self.assertEqual(state['observation'], 'terminal_recorded')

    def test_stale_process_states_not_model_health(self):
        with self.new() as p:
            value = p.snapshot()
            later = value['heartbeat_at_unix']+60
            absent = monitor.assess(value, now=later, process_probe=lambda _: 'absent')
            alive = monitor.assess(value, now=later, process_probe=lambda _: 'exists')
            self.assertEqual(absent['observation'], 'interrupted_suspected_process_absent')
            self.assertEqual(alive['observation'], 'heartbeat_stale_process_exists')
            self.assertEqual(alive['model_health'], 'not_inferred')
            p.finish('interrupted')

    def test_latest_uses_start_and_ignores_other_files(self):
        with self.new('old') as old:
            time.sleep(0.02)
            with self.new('new', run_id='new_safe') as new:
                old.set_stage('tokenizer')
                self.assertEqual(monitor.latest_snapshot(self.root)['run_id'], 'new_safe')
                new.finish('dry_run_complete')
            old.finish('dry_run_complete')
        (self.root/'do_not_read_patient.txt').write_text('must not be printed')
        self.assertEqual(monitor.latest_snapshot(self.root)['run_id'], 'new_safe')

    def test_stdout_and_missing_path_never_echo_path(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.new(console=True) as p:
                p.finish('dry_run_complete')
            result = monitor.main(['--run-dir', str(self.root/'sensitive_nonexistent'), '--once'])
        self.assertEqual(result, 0)
        self.assertNotIn(str(self.root), out.getvalue())
        self.assertIn('[대기]', out.getvalue())

    def test_reject_nonfinite_duplicate_and_oversize_json(self):
        with self.new() as p:
            p.finish('dry_run_complete')
        path = self.root/'run'/'progress.json'
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b' '*(monitor.MAX_BYTES+1)):
            path.write_bytes(raw)
            with self.assertRaises(monitor.InvalidProgress):
                monitor.read_snapshot(path.parent)

    def test_symlink_progress_rejected(self):
        with self.new() as p:
            p.finish('dry_run_complete')
        linked = self.root/'linked'
        linked.mkdir()
        (linked/'progress.json').symlink_to(self.root/'run'/'progress.json')
        with self.assertRaises(OSError):
            monitor.read_snapshot(linked)

    def test_fifo_rejected_without_waiting(self):
        run = self.root/'fifo'
        run.mkdir()
        os.mkfifo(run/'progress.json', 0o600)
        with self.assertRaises(monitor.InvalidProgress):
            monitor.read_snapshot(run)

    def test_no_calls_dry_run_is_explicit(self):
        with self.new(total=24) as p:
            p.set_stage('tokenizer')
            p.finish('dry_run_complete')
        value = monitor.read_snapshot(self.root/'run')
        self.assertEqual(value['new_attempts'], 0)
        self.assertEqual(value['completed'], 0)
        self.assertEqual(value['total'], 24)

    def test_sequential_and_terminal_guards(self):
        with self.new(total=1) as p:
            with self.assertRaises(ValueError):
                p.start_item(2)
            p.start_item(1)
            with self.assertRaises(ValueError):
                p.start_item(1)
            with self.assertRaises(ValueError):
                p.finish('dry_run_complete')
            p.finish_item(success=True)
            p.finish('completed')
            with self.assertRaises(RuntimeError):
                p.set_stage('tokenizer')

    def test_reader_rejects_forged_terminal_counts(self):
        with self.new(total=2) as p:
            p.finish('interrupted')
        value = p.snapshot()
        for status in ('completed', 'completed_with_errors', 'planned_prefix_complete'):
            with self.subTest(status=status):
                value['status'] = status
                with self.assertRaises(monitor.InvalidProgress):
                    monitor.validate_snapshot(value)

    def test_zero_byte_event_write_fails_without_loop(self):
        p = self.new()
        with patch('progress.os.write', return_value=0):
            with self.assertRaises(OSError):
                p.finish('failed')
        self.assertTrue(p._closed)

    def test_tty_refresh_is_fixed_control_not_input(self):
        class FakeTTY(io.StringIO):
            def isatty(self): return True
        tty = FakeTTY()
        monitor.print_view('첫째\n둘째', stream=tty, replace=True)
        self.assertEqual(tty.getvalue(), '\x1b[2F\x1b[J첫째\n둘째\n')
        log = io.StringIO()
        monitor.print_view('첫째\n둘째', stream=log, replace=True)
        self.assertNotIn('\x1b', log.getvalue())


if __name__ == '__main__':
    unittest.main()
