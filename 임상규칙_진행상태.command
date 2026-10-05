#!/bin/zsh
set -u
ANTIBIOTIC_ROOT="${0:A:h}"
if [[ -f "$ANTIBIOTIC_ROOT/launch.py" ]]; then
  ANTIBIOTIC_ROOT="${ANTIBIOTIC_ROOT:h:h}"
fi
if [[ "$(/bin/hostname)" != "Ericui-MacStudio.local" ]]; then
  print "지정된 Mac Studio가 아니므로 실행하지 않습니다."
  read -k 1 "?아무 키나 눌러 닫기 "
  exit 1
fi
ANTIBIOTIC_PYTHON="$ANTIBIOTIC_ROOT/exploratory/2026-10-05_kapp_sft_v0.1/.venv/bin/python"
if [[ ! -x "$ANTIBIOTIC_PYTHON" ]]; then
  print "연구 전용 Python 환경을 찾을 수 없습니다."
  read -k 1 "?아무 키나 눌러 닫기 "
  exit 1
fi
"$ANTIBIOTIC_PYTHON" "$ANTIBIOTIC_ROOT/code/clinical_rules_experiment/monitor.py" --root "$ANTIBIOTIC_ROOT/outputs/clinical_rules_runs" --follow
ANTIBIOTIC_EXIT=$?
print ""
read -k 1 "?아무 키나 눌러 닫기 "
exit "$ANTIBIOTIC_EXIT"
