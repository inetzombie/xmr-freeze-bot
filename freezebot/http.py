"""Tiny stdlib-only JSON HTTP helpers with retries."""
import json
import time
import urllib.error
import urllib.request

USER_AGENT = "xmr-freeze-bot/0.3 (+https://getmonero.org)"


class HTTPError(Exception):
    pass


def request_json(url, payload=None, headers=None, retries=3, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise HTTPError(f"{url}: {last}")
