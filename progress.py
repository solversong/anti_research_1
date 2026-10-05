"""Private, content-free operational progress. Standard library, no network.

Only this process writes progress.json; an advisory process lock also rejects
another cooperating writer. Heartbeats and transitions share one mutex.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid

SCHEMA = 'clinical-run-progress.v1'
STAGES = frozenset(('preflight', 'tokenizer', 'validating_model', 'generating',
                    'applying_gate', 'saving', 'verifying_model', 'finalizing', 'finished'))
TERMINAL = frozenset(('completed', 'completed_with_errors', 'planned_prefix_complete',
                      'dry_run_complete', 'failed', 'interrupted'))
STATUSES = TERMINAL | {'running'}
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')
SAFE_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z')


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('INVALID_OPERATIONAL_COUNT')
    return value


def _safe(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError('INVALID_SAFE_OPERATIONAL_IDENTIFIER')
    return value


def _private_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('OPERATIONAL_FILE_MUST_BE_REGULAR')
    os.fchmod(fd, 0o600)
    return fd


class Progress:
    """One invocation, including prefixes. Prior records are never new calls.

    start_item must be called immediately before dispatching /api/generate;
    finish_item marks a durably recorded response/error, not clinical validity.
    It is intentionally impossible to attach arbitrary data or error text.
    """
    def __init__(self, run_dir, *, model, version, total, dataset_total=None,
                 reused_records=0, run_id=None, heartbeat_seconds=5, console=False):
        self.total = _integer(total)
        self.reused_records = _integer(reused_records)
        self.dataset_total = _integer(dataset_total if dataset_total is not None
                                      else total + reused_records)
        if total + reused_records > self.dataset_total:
            raise ValueError('PLAN_EXCEEDS_DATASET')
        self.run_id = _safe(run_id or uuid.uuid4().hex, SAFE_ID)
        self.model = _safe(model, SAFE_MODEL)
        self.version = _safe(version, SAFE_ID)
        if type(console) is not bool:
            raise ValueError('CONSOLE_MUST_BE_BOOLEAN')
        self.console = console
        self._console_rows = 0
        if type(heartbeat_seconds) not in (int, float) or not math.isfinite(heartbeat_seconds) or not 0.02 <= heartbeat_seconds <= 60:
            raise ValueError('INVALID_HEARTBEAT_INTERVAL')
        self.interval = heartbeat_seconds
        self.directory = Path(run_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError('RUN_DIRECTORY_MUST_NOT_BE_SYMLINK')
        self._lock_fd = _private_open(self.directory / '.progress.lock', os.O_RDWR | os.O_CREAT)
        self._closed = False
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(self._lock_fd)
            self._closed = True
            raise RuntimeError('PROGRESS_WRITER_ALREADY_ACTIVE') from None
        try:
            if (self.directory / 'progress.json').exists() or (self.directory / 'events.jsonl').exists():
                raise FileExistsError('EXISTING_PROGRESS_REQUIRES_NEW_RUN_DIRECTORY')
            self._events_fd = _private_open(self.directory / 'events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL)
            self._mutex = threading.RLock()
            self._stop = threading.Event()
            self._started_wall = time.time()
            self._started_mono = time.monotonic()
            self._request_start = None
            self._current = None
            self._attempts = self._completed = self._failed = self._sequence = 0
            self._durations = []
            self._status = 'running'
            self._stage = 'preflight'
            self._last = None
            self._write_error = False
            self._publish('started')
            self._thread = threading.Thread(target=self._heartbeat, name='safe-progress-heartbeat', daemon=True)
            self._thread.start()
        except BaseException:
            if hasattr(self, '_events_fd'):
                os.close(self._events_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._closed = True
            raise

    def _snapshot(self):
        now = time.monotonic()
        average = sum(self._durations) / len(self._durations) if self._durations else None
        request_age = now - self._request_start if self._request_start is not None else None
        remaining = self.total - self._completed
        eta = None
        if average is not None and self._status == 'running':
            eta = average * remaining
            if request_age is not None:
                eta = max(0.0, average - request_age) + average * max(0, remaining - 1)
        if self._status in TERMINAL:
            eta = 0.0 if self._completed == self.total else None
        return {'schema': SCHEMA, 'run_id': self.run_id, 'model': self.model,
                'version': self.version, 'pid': os.getpid(), 'status': self._status,
                'stage': self._stage, 'sequence': self._sequence,
                'started_at_unix': self._started_wall, 'heartbeat_at_unix': time.time(),
                'heartbeat_interval_seconds': self.interval,
                'elapsed_seconds': max(0.0, now - self._started_mono),
                'total': self.total, 'dataset_total': self.dataset_total,
                'reused_records': self.reused_records, 'new_attempts': self._attempts,
                'completed': self._completed, 'failed': self._failed,
                'succeeded': self._completed - self._failed,
                'outcome_unrecorded': self._attempts - self._completed,
                'current_ordinal': self._current,
                'current_dataset_ordinal': self.reused_records + self._current if self._current is not None else None,
                'current_request_age_seconds': request_age,
                'mean_recorded_request_seconds': average, 'eta_seconds_estimate': eta,
                'eta_sample_count': len(self._durations),
                'heartbeat_write_error': self._write_error}

    def _publish(self, event=None):
        # Caller holds mutex, including heartbeat. Sequence is monotonic and the
        # terminal transition can never be overwritten by an earlier snapshot.
        self._sequence += 1
        snapshot = self._snapshot()
        raw = (json.dumps(snapshot, ensure_ascii=True, sort_keys=True, allow_nan=False) + '\n').encode('ascii')
        fd, name = tempfile.mkstemp(prefix='.progress-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / 'progress.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._last = snapshot
        if event is not None:
            entry = dict(snapshot, event=event)
            payload = (json.dumps(entry, ensure_ascii=True, sort_keys=True, allow_nan=False) + '\n').encode('ascii')
            # One process owns this append fd; all thread appends share mutex.
            offset = 0
            while offset < len(payload):
                written = os.write(self._events_fd, payload[offset:])
                if written <= 0:
                    raise OSError('PROGRESS_EVENT_WRITE_FAILED')
                offset += written
            os.fsync(self._events_fd)
        if self.console:
            # Lazy import avoids a module import cycle. Only validated metadata
            # reaches this formatter; no raw exception or source path is printed.
            from monitor import render_snapshot, print_view
            try:
                self._console_rows = print_view(render_snapshot(snapshot),
                    replace=bool(self._console_rows), previous_rows=self._console_rows)
            except OSError:
                pass  # A closed viewing pipe does not change experiment results.

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            with self._mutex:
                if self._status != 'running':
                    return
                try:
                    self._publish()
                except OSError:
                    # No raw exception/path in logs; the last successful beat
                    # naturally becomes stale. Next transition will raise on IO.
                    self._write_error = True

    def _active(self):
        if self._status != 'running' or self._closed:
            raise RuntimeError('PROGRESS_ALREADY_TERMINAL')

    def set_stage(self, stage):
        if not isinstance(stage, str) or stage not in STAGES or stage == 'finished':
            raise ValueError('INVALID_STAGE')
        with self._mutex:
            self._active()
            self._stage = stage
            self._publish('stage_changed')

    def set_plan(self, *, total, dataset_total, reused_records=0):
        """Resolve a prefix/resume plan after validation, before any new call."""
        total, dataset_total, reused_records = map(_integer, (total, dataset_total, reused_records))
        if total + reused_records > dataset_total:
            raise ValueError('PLAN_EXCEEDS_DATASET')
        with self._mutex:
            self._active()
            if self._attempts:
                raise RuntimeError('CANNOT_CHANGE_PLAN_AFTER_REQUEST_DISPATCH')
            self.total, self.dataset_total, self.reused_records = total, dataset_total, reused_records
            self._publish('plan_resolved')

    def start_item(self, ordinal):
        _integer(ordinal, 1)
        with self._mutex:
            self._active()
            if self._current is not None or ordinal != self._completed + 1 or ordinal > self.total:
                raise ValueError('INVALID_SEQUENTIAL_REQUEST_ORDINAL')
            self._current = ordinal
            self._request_start = time.monotonic()
            self._attempts += 1
            self._stage = 'generating'
            self._publish('request_started')

    def finish_item(self, *, success):
        if type(success) is not bool:
            raise ValueError('SUCCESS_MUST_BE_BOOLEAN')
        with self._mutex:
            self._active()
            if self._current is None:
                raise ValueError('NO_REQUEST_IN_PROGRESS')
            self._durations.append(max(0.0, time.monotonic() - self._request_start))
            self._completed += 1
            self._failed += int(not success)
            self._current = self._request_start = None
            self._stage = 'saving'
            self._publish('request_recorded')

    def snapshot(self):
        """Return a detached safe snapshot, including final state after close."""
        with self._mutex:
            return dict(self._last)

    def finish(self, status='completed'):
        if not isinstance(status, str) or status not in TERMINAL:
            raise ValueError('INVALID_TERMINAL_STATUS')
        with self._mutex:
            if self._status != 'running':
                if self._status == status:
                    return self.snapshot()
                raise RuntimeError('TERMINAL_STATUS_CANNOT_BE_REPLACED')
            if status in ('completed', 'completed_with_errors', 'planned_prefix_complete') and self._completed != self.total:
                raise ValueError('SUCCESS_REQUIRES_ALL_PLANNED_REQUESTS_RECORDED')
            if status == 'completed' and self._failed:
                raise ValueError('FAILED_REQUESTS_REQUIRE_COMPLETED_WITH_ERRORS')
            if status == 'completed_with_errors' and not self._failed:
                raise ValueError('COMPLETED_WITH_ERRORS_REQUIRES_RECORDED_FAILURE')
            if status == 'dry_run_complete' and self._attempts:
                raise ValueError('DRY_RUN_CANNOT_HAVE_MODEL_ATTEMPTS')
            self._status = status
            self._stage = 'finished'
            self._stop.set()
            try:
                self._publish('finished')
            finally:
                self._close_files()
        self._thread.join(timeout=self.interval + 1)
        return self.snapshot()

    def _close_files(self):
        if not self._closed:
            os.close(self._events_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._status == 'running':
            if exc_type is not None:
                status = 'interrupted' if issubclass(exc_type, (KeyboardInterrupt, SystemExit)) else 'failed'
            elif self._completed == self.total:
                status = 'completed_with_errors' if self._failed else 'completed'
            else:
                status = 'interrupted'
            self.finish(status)
        return False
