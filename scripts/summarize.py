"""Reads `curl -w '\\nHTTP %{http_code}'` output on stdin and prints one summary line."""

import json
import sys

raw = sys.stdin.read()
body_text, _, code = raw.rpartition("\nHTTP ")
try:
    body = json.loads(body_text)
except ValueError:
    print(f"  HTTP {code.strip()}  (non-JSON response) {body_text[:200]}")
    sys.exit(0)

print(
    f"  HTTP {code.strip()}  status={body.get('status')}  attempts={body.get('attempts')}  "
    f"upstream_status={body.get('upstream_status')}  error={body.get('error')}"
)
