"""Outage proxy for a LIVE FeasibleRouteBench failure run (B45).

Stands in front of one real OpenAI-compatible endpoint and forwards every
request to it, EXCEPT chat calls whose last user message contains one of the
outage questions listed in DOWN_JSON. For those it closes the connection
without sending any response: a transport-level failure with no HTTP status,
the shape of a dead server. The real server is never stopped, so anything
else using it (e.g. an interactive coding client) is unaffected.

Matching on the question text, not on call order, makes the outage window
deterministic regardless of retries or sweep order. Every pass/drop is logged
to LOG as one JSON line. Test-and-eval tooling only; never part of serving.

    python3 scripts/outage_proxy.py PORT UPSTREAM DOWN_JSON LOG
    # e.g. 11500 http://127.0.0.1:11434 down.json proxy.log

DOWN_JSON is a JSON list of question strings -- for the B45 scenario, the last
user message of cases 10-19 of eval/suites/outage.jsonl:

    python3 -c "import json; rows=[json.loads(l) for l in open('eval/suites/outage.jsonl')]; \
      json.dump([r['messages'][-1]['content'] for r in rows[10:20]], open('down.json','w'))"

See docs/research/feasible-route-bench.md §11.
"""
import http.server, json, sys, time, urllib.request, urllib.error

UPSTREAM = sys.argv[2]
DOWN = set(json.load(open(sys.argv[3])))
LOG = open(sys.argv[4], "a", buffering=1)

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _relay(self, body=None):
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=self.command,
            headers={k: v for k, v in self.headers.items()
                     if k.lower() not in ("host", "connection", "content-length")})
        try:
            resp = urllib.request.urlopen(req, timeout=600)
            status, headers, data = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, headers, data = e.code, e.headers, e.read()
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self._relay()
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            msgs = json.loads(body).get("messages", [])
            last = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), None)
        except Exception:
            last = None
        if isinstance(last, str) and any(q in last for q in DOWN):
            LOG.write(json.dumps({"t": time.time(), "drop": last[:60]}) + "\n")
            self.close_connection = True
            self.connection.shutdown(2)
            return
        LOG.write(json.dumps({"t": time.time(), "pass": (last or "")[:60]}) + "\n")
        self._relay(body)
    def log_message(self, *a):
        pass

http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
