#!/usr/bin/env python3
"""Read only progress.json. Never inspect cases, prompts, outputs or manifests."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import unicodedata

from progress import SCHEMA, SAFE_ID, SAFE_MODEL, STAGES, STATUSES, TERMINAL

MAX_BYTES = 16384


class InvalidProgress(ValueError):
    pass


def _require(condition):
    if not condition:
        raise InvalidProgress('INVALID_OPERATIONAL_PROGRESS')


def _pairs(pairs):
    value = {}
    for k, v in pairs:
        _require(k not in value)
        value[k] = v
    return value


def validate_snapshot(value):
    """Strict allowlist prevents arbitrary data or terminal escape disclosure."""
    keys = {'schema','run_id','model','version','pid','status','stage','sequence',
            'started_at_unix','heartbeat_at_unix','heartbeat_interval_seconds',
            'elapsed_seconds','total','dataset_total','reused_records','new_attempts',
            'completed','failed','succeeded','outcome_unrecorded','current_ordinal',
            'current_dataset_ordinal','current_request_age_seconds',
            'mean_recorded_request_seconds','eta_seconds_estimate','eta_sample_count',
            'heartbeat_write_error'}
    _require(isinstance(value, dict) and set(value) == keys)
    _require(value['schema'] == SCHEMA)
    for key, pattern in [('run_id', SAFE_ID), ('version', SAFE_ID), ('model', SAFE_MODEL)]:
        _require(isinstance(value[key], str) and pattern.fullmatch(value[key]) is not None)
    _require(isinstance(value['status'], str) and value['status'] in STATUSES
             and isinstance(value['stage'], str) and value['stage'] in STAGES)
    for key in ('pid','sequence','total','dataset_total','reused_records','new_attempts','completed','failed','succeeded','outcome_unrecorded','eta_sample_count'):
        _require(type(value[key]) is int and 0 <= value[key] <= 10**12)
    _require(value['pid'] > 0)
    for key in ('started_at_unix','heartbeat_at_unix','heartbeat_interval_seconds','elapsed_seconds'):
        _require(type(value[key]) in (int,float) and math.isfinite(value[key]) and value[key] >= 0)
    _require(0.02 <= value['heartbeat_interval_seconds'] <= 60)
    for key in ('current_request_age_seconds','mean_recorded_request_seconds','eta_seconds_estimate'):
        _require(value[key] is None or type(value[key]) in (int,float) and math.isfinite(value[key]) and value[key] >= 0)
    for key in ('current_ordinal','current_dataset_ordinal'):
        _require(value[key] is None or type(value[key]) is int and 1 <= value[key] <= 10**12)
    _require(type(value['heartbeat_write_error']) is bool)
    _require(value['failed'] <= value['completed'] <= value['new_attempts'] <= value['total'])
    _require(value['new_attempts'] - value['completed'] == value['outcome_unrecorded'] <= 1)
    _require(value['succeeded'] == value['completed'] - value['failed'])
    _require(value['total'] + value['reused_records'] <= value['dataset_total'])
    _require(value['eta_sample_count'] == value['completed'])
    if value['current_ordinal'] is None:
        _require(value['current_dataset_ordinal'] is None and value['current_request_age_seconds'] is None and value['outcome_unrecorded'] == 0)
    else:
        _require(value['current_ordinal'] == value['new_attempts'] == value['completed'] + 1)
        _require(value['current_dataset_ordinal'] == value['reused_records'] + value['current_ordinal'])
        _require(value['current_request_age_seconds'] is not None)
    _require((value['status'] in TERMINAL) == (value['stage'] == 'finished'))
    if value['status'] in ('completed', 'completed_with_errors', 'planned_prefix_complete'):
        _require(value['completed'] == value['total'] and value['current_ordinal'] is None)
    if value['status'] == 'completed':
        _require(value['failed'] == 0)
    if value['status'] == 'completed_with_errors':
        _require(value['failed'] > 0)
    if value['status'] == 'dry_run_complete':
        _require(value['new_attempts'] == 0)
    return dict(value)


def read_snapshot(run_dir):
    fd = os.open(Path(run_dir) / 'progress.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode))
        raw = stream.read(MAX_BYTES + 1)
    _require(len(raw) <= MAX_BYTES)
    try:
        return validate_snapshot(json.loads(raw, object_pairs_hook=_pairs,
             parse_constant=lambda _: (_ for _ in ()).throw(InvalidProgress('INVALID_OPERATIONAL_PROGRESS'))))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError):
        raise InvalidProgress('INVALID_OPERATIONAL_PROGRESS') from None


def latest_snapshot(root):
    """Immediate run directories only. Latest start wins, not newest heartbeat."""
    candidates = []
    for directory in Path(root).iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            snapshot = read_snapshot(directory)
        except (OSError, ValueError):
            continue
        candidates.append((snapshot['started_at_unix'], snapshot['run_id'], snapshot))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[:2])[2]


def process_state(pid):
    """PID existence only; PID reuse means this is not identity/health proof."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'absent'
    except (PermissionError, OSError, OverflowError):
        return 'unknown'
    return 'exists'


def assess(snapshot, *, now=None, stale_seconds=None, process_probe=process_state):
    value = validate_snapshot(snapshot)
    now = time.time() if now is None else now
    age = max(0.0, now - value['heartbeat_at_unix'])
    limit = max(15.0, value['heartbeat_interval_seconds'] * 3) if stale_seconds is None else stale_seconds
    _require(type(limit) in (int,float) and math.isfinite(limit) and limit > 0)
    terminal = value['status'] in TERMINAL
    state = 'not_checked_terminal' if terminal else process_probe(value['pid'])
    _require(state in ('not_checked_terminal', 'exists', 'absent', 'unknown'))
    stale = not terminal and age > limit
    if terminal:
        observation = 'terminal_recorded'
    elif state == 'absent':
        observation = 'interrupted_suspected_process_absent'
    elif stale:
        observation = 'heartbeat_stale_process_' + state
    else:
        observation = 'heartbeat_recent_process_' + state
    return {'heartbeat_age_seconds': age, 'heartbeat_stale': stale,
            'writer_process': state, 'observation': observation,
            'model_health': 'not_inferred'}


def _duration(seconds):
    if seconds is None:
        return 'n/a'
    seconds = int(max(0, seconds))
    return f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'


def render_snapshot(snapshot, *, now=None, stale_seconds=None, process_probe=process_state):
    """Two fixed Korean lines; model/version/run ID are validated ASCII only."""
    v = validate_snapshot(snapshot)
    state = assess(v, now=now, stale_seconds=stale_seconds, process_probe=process_probe)
    ordinal = '-' if v['current_ordinal'] is None else str(v['current_ordinal'])
    statuses = {'running':'실행 중','completed':'완료','completed_with_errors':'완료 · 오류 포함',
                'planned_prefix_complete':'이번 구간 완료','dry_run_complete':'실행 전 검사 완료',
                'failed':'실행 실패','interrupted':'중단 기록됨'}
    stages = {'preflight':'준비 검사','tokenizer':'입력 길이 검사','validating_model':'모델 확인',
              'generating':'모델 응답 대기','applying_gate':'규칙 검사','saving':'결과 기록',
              'verifying_model':'종료 후 모델 확인','finalizing':'마무리','finished':'종료'}
    if state['observation'] == 'terminal_recorded':
        signal = '종료 상태 기록됨'
    elif state['writer_process'] == 'absent':
        signal = '중단 의심: 기록 프로세스 없음'
    elif state['heartbeat_stale']:
        signal = '상태 신호 지연: 프로세스 존재' if state['writer_process'] == 'exists' else '상태 신호 지연: 프로세스 확인 불가'
    else:
        signal = '최근 상태 신호' if state['writer_process'] == 'exists' else '상태 신호 있음: 프로세스 확인 불가'
    # A short second line avoids identifiers overwhelming the changing counters.
    return (f"[{statuses[v['status']]} · {stages[v['stage']]}] 완료 {v['completed']}/{v['total']} | "
            f"정상 {v['succeeded']} | 실패 {v['failed']} | 새 요청 {v['new_attempts']} | 미기록 {v['outcome_unrecorded']} | "
            f"현재 {ordinal} | 경과 {_duration(v['elapsed_seconds'])} | 현재 요청 {_duration(v['current_request_age_seconds'])} | "
            f"예상 잔여 {_duration(v['eta_seconds_estimate'])} ({v['eta_sample_count']}건 기준)\n"
            f"모델 {v['model']} / 버전 {v['version']} / 실행 {v['run_id']} | "
            f"이전 재사용 {v['reused_records']} · 전체 {v['dataset_total']} | "
            f"{signal} {_duration(state['heartbeat_age_seconds'])} 전 · 모델 건강 미판정")


def _screen_rows(text, columns):
    def width(line):
        return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in line)
    return sum(max(1, (width(line) + columns - 1) // columns) for line in text.splitlines())


def print_view(text, *, replace=False, stream=None, previous_rows=None):
    """Only our two lines are refreshed in a TTY; redirected output is a log."""
    stream = sys.stdout if stream is None else stream
    rows = _screen_rows(text, max(1, shutil.get_terminal_size(fallback=(120,24)).columns))
    if replace and stream.isatty():
        # Fixed control bytes only. User-controlled values cannot contain ESC.
        count = previous_rows if type(previous_rows) is int and previous_rows > 0 else rows
        stream.write(f'\x1b[{count}F\x1b[J')
    stream.write(text + '\n')
    stream.flush()
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description='Safe operational counts only. No model-health inference.')
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument('--run-dir', type=Path)
    location.add_argument('--root', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--follow', action='store_true')
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--stale-seconds', type=float)
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not 0.2 <= args.interval <= 60 or args.stale_seconds is not None and (not math.isfinite(args.stale_seconds) or args.stale_seconds <= 0):
        print('monitor_error=invalid_interval', file=sys.stderr)
        return 2
    following = args.follow
    rendered_rows = 0
    try:
        while True:
            snapshot = None
            try:
                snapshot = latest_snapshot(args.root) if args.root else read_snapshot(args.run_dir)
                line = '[대기] 새 실행의 상태 기록을 기다립니다.\n사례 내용은 표시하지 않습니다.' if snapshot is None else render_snapshot(snapshot, stale_seconds=args.stale_seconds)
            except FileNotFoundError:
                line = '[대기] 새 실행의 상태 기록을 기다립니다.\n사례 내용은 표시하지 않습니다.'
            except (OSError, ValueError):
                print('monitor_error=unreadable_or_invalid_progress', file=sys.stderr)
                return 2
            rendered_rows = print_view(line, replace=bool(rendered_rows), previous_rows=rendered_rows)
            if not following or args.run_dir and snapshot is not None and snapshot['status'] in TERMINAL:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('monitor_status=stopped_runner_unchanged')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
