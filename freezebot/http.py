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
        except urllib.error.HTTPError as e:
            # Many public RPCs answer a too-large eth_getLogs range with HTTP 400/403/413 and a JSON-RPC error
            # body. Hand that body back (so the caller can fail over or shrink its range) instead of retrying
            # the same request three times. Rate limits and server errors are still retried.
            last = e
            if e.code not in (408, 425, 429) and e.code < 500:
                try:
                    body = json.loads(e.read().decode())
                except Exception:
                    body = None
                if isinstance(body, dict) and "error" in body:
                    return body
                raise HTTPError(f"{url}: HTTP {e.code}") from None
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise HTTPError(f"{url}: {last}")
