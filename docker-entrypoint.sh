#!/bin/sh
set -e
mkdir -p "$REPORTS_DIR"
python squeeze_report.py || true
python scanner_v2.py &
SCANNER=$!
( cd "$REPORTS_DIR" && python -m http.server 8000 ) &
echo "Squeeze scanner running. Report: http://localhost:8000/squeeze.html"
while kill -0 "$SCANNER" 2>/dev/null; do
  python squeeze_report.py || true
  sleep 60
done
