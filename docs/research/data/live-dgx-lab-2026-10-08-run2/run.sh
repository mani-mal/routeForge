#!/usr/bin/env bash
cd "$(dirname "$0")"
date -u +%Y-%m-%dT%H:%M:%SZ > since2.txt
rm -f client-3.ndjson outage.log
ctl() { kubectl -n runai-routeforge exec deploy/routeforge-api -- python -c "import urllib.request;print(urllib.request.urlopen('http://alias-proxy.runai-routeforge:8000/_ctl?alias=glm-flash-east&down=$1',timeout=10).read().decode())" 2>/dev/null; }
( sleep 300; echo "$(date -u +%s.%N) down $(ctl 1)" >> outage.log; sleep 180; echo "$(date -u +%s.%N) up $(ctl 0)" >> outage.log ) &
python3 gen.py 900 3 1.0
wait
cat outage.log; wc -l client-3.ndjson
