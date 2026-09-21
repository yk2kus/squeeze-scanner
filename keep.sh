#!/bin/bash
cd "$(dirname "$0")"
LOCK=reports/.keep.lock; exec 9>"$LOCK"; flock -n 9 || exit 0
ABSPY="/home/ubuntu/Desktop/Projects/SqueezeScanner/.venv/bin/python"
alive(){ pgrep -f "SqueezeScanner/.venv/bin/python scanner_v2.py" >/dev/null 2>&1; }
mkdir -p reports
while true; do
  if ! alive; then
    setsid nohup "$ABSPY" scanner_v2.py >> reports/scanner.log 2>&1 < /dev/null 9>&- &
    echo "$(date -u '+%F %T') (re)started scanner" >> reports/keep.log
  fi
  "$ABSPY" squeeze_report.py >> reports/report.log 2>&1
  sleep 60
done
