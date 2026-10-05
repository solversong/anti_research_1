#!/usr/bin/env python3
"""Interactive local synthetic pilot launcher. No data download or API credentials."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

class LaunchError(RuntimeError):
    """Fixed local user-facing instructions only, never model/library messages."""

HOST = 'Ericui-MacStudio.local'
BASE = Path(__file__).resolve().parents[2]
SOURCE = 'outputs/clinical_rules_pilot_2026-10-05_v0.1/frozen_inputs_v0.2'

@contextmanager
def run_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise LaunchError('잠금 파일 형식이 올바르지 않습니다.')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise LaunchError('이미 실행 중인 실험이 있습니다. 진행 상태 창에서 확인하세요.') from None
        yield
    finally:
        os.close(fd)

def listening():
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(('127.0.0.1', 11434)) == 0

def stop_owned_server(proc):
    if proc is None or proc.poll() is not None: return
    proc.terminate()
    try: proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.wait(timeout=5)

def execute(base, limit, dry_run=False):
    if socket.gethostname() != HOST: raise LaunchError('지정한 Mac Studio가 아니므로 실행하지 않습니다.')
    if limit not in (2, 24): raise LaunchError('합성 2건 또는 24건만 선택할 수 있습니다.')
    import run_draft_pilot as runner
    from local_runtime import strict_json
    import pilot_support as support
    frozen = base / SOURCE
    paths = {k: frozen/n for k,n in [('cases','synthetic24.json'), ('rules','rules_frozen.json'), ('config','config.json')]}
    contract = strict_json((frozen/'freeze_manifest.json').read_text())
    for p in paths.values():
        if support.file_sha(p) != contract['files_sha256'][p.name]:
            raise LaunchError('동결 입력 해시가 달라졌습니다. 새 입력 버전을 먼저 준비하세요.')
    values = {k:strict_json(p.read_text()) for k,p in paths.items()}
    # Copying a different rule file without a new freeze must never silently alter a run.
    if support.digest(values['rules']) != support.digest(strict_json(Path(__file__).with_name('clinical_rules_draft_v0.1.json').read_text())):
        raise LaunchError('코드의 규칙과 동결 규칙이 다릅니다. 검수 후 새 입력을 동결하세요.')
    work = base/'work/clinical_rules_monitor_v0.1'
    runs = base/'outputs/clinical_rules_runs'
    work.mkdir(parents=True,exist_ok=True,mode=0o700)
    runs.mkdir(parents=True,exist_ok=True,mode=0o700)
    run_name = datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    output = runs/run_name
    server = None
    log = None
    old_term = signal.getsignal(signal.SIGTERM)
    def interrupted(*_): raise KeyboardInterrupt
    with run_lock(work/'launcher.lock'):
        try:
            signal.signal(signal.SIGTERM, interrupted)
            if not dry_run:
                if listening():
                    raise LaunchError('11434 포트에 기존 서버가 있습니다. 실행 환경을 확인할 수 없어 새 실험을 시작하지 않았습니다. 기존 서버는 종료하지 않았습니다.')
                binary=base/'runtime/ollama-v0.35.1/ollama'
                if not binary.is_file(): raise LaunchError('연구 전용 Ollama를 찾을 수 없습니다.')
                env=dict(os.environ)
                env.update(OLLAMA_HOST='127.0.0.1:11434', OLLAMA_MODELS=str(base/'runtime/models'),
                           OLLAMA_NO_CLOUD='1', OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1', OLLAMA_DEBUG='0')
                log=os.fdopen(os.open(work/(run_name+'-server.log'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb')
                print('로컬 서버 준비 중 · 127.0.0.1 · 클라우드 사용 안 함',flush=True)
                server=subprocess.Popen([str(binary),'serve'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                deadline=time.monotonic()+30
                while not listening():
                    if server.poll() is not None or time.monotonic()>deadline:
                        raise LaunchError('연구 전용 로컬 서버가 준비되지 않았습니다. 서버 기록을 확인하세요.')
                    time.sleep(.2)
                if server.poll() is not None: raise LaunchError('이번 서버 프로세스가 종료되어 실험을 중단합니다.')
            print('입력: 완전 가상 합성 규칙 명세 · 임상 평가 아님',flush=True)
            print('종료 요청: Ctrl+C · 재시도 없음 · 호출 중 종료하면 추론 완료 여부는 미확정',flush=True)
            manifest=runner.run(values['cases'],values['rules'],values['config'],'synthetic_spec',output,
                                execute=not dry_run,limit=limit,console=True)
            print('결과 폴더: '+str(output),flush=True)
            return 0 if manifest['status'] in ('DRY_RUN_ONLY','COMPLETE_ALL_ATTEMPTED','PLANNED_PREFIX_COMPLETE_CONTINUATION_PENDING') and manifest['recorded_failures']==0 else 1
        finally:
            stop_owned_server(server)
            if log: log.close()
            signal.signal(signal.SIGTERM,old_term)
            if server: print('이번 실행이 시작한 로컬 서버를 종료했습니다.',flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--menu',action='store_true')
    parser.add_argument('--limit',type=int,choices=[2,24],default=2)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--research-root',type=Path,default=BASE)
    args=parser.parse_args()
    if args.menu:
        print('\n항생제 임상규칙 실험 · 검수 전 합성 파일럿\n1. 실행 상태 보기\n2. 합성 2건 실제 모델 실행\n3. 합성 24건 실제 모델 실행\n4. 입력·모델 설정 사전검사 (모델 호출 없음)\nq. 종료\n',flush=True)
        choice=input('선택: ').strip()
        if choice=='q': return 0
        if choice=='1':
            return subprocess.call([sys.executable,str(Path(__file__).with_name('monitor.py')),'--root',str(args.research_root/'outputs/clinical_rules_runs'),'--follow'])
        if choice not in ('2','3','4'):
            print('선택을 확인하세요. 실험은 시작하지 않았습니다.');return 2
        args.limit=24 if choice=='3' else 2
        args.dry_run=choice=='4'
    return execute(args.research_root,args.limit,args.dry_run)

if __name__=='__main__':
    try: raise SystemExit(main())
    except KeyboardInterrupt: print('\n중단 요청을 기록했습니다. 호출 중이었다면 모델의 완료 여부는 미확정입니다.',flush=True);raise SystemExit(130)
    except Exception as exc:
        # Never echo arbitrary exceptions, paths or model bodies into shared console logs.
        from pilot_support import ContractError
        if isinstance(exc,LaunchError): print(str(exc),file=sys.stderr)
        elif isinstance(exc,ContractError): print('실험 입력·모델·설정의 고정 계약 확인에 실패했습니다.',file=sys.stderr)
        else: print('실행 준비 또는 저장에 실패했습니다. 원자료를 출력하지 않고 중단합니다.',file=sys.stderr)
        raise SystemExit(1)
