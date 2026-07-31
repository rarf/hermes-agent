# -*- coding: utf-8 -*-
"""
Local Kimi quota probe - run this YOURSELF (or via the refresh cron).

Reads credentials from ~/kimi_session.json:
  {"api_key": "..."}      # preferred - from https://www.kimi.com/code/console
  {"token": "..."}        # kimi-auth cookie JWT (web fallback)

Then calls the Kimi usages API and prints the raw JSON + parsed windows.
Does NOT print your secret.
"""
import json
import os
import sys
import urllib.request
import urllib.error

API_URL = "https://api.kimi.com/coding/v1/usages"
WEB_URL = "https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages"
SESSION = os.path.join(os.path.expanduser("~"), "kimi_session.json")


def _creds():
    try:
        with open(SESSION, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("api_key"), d.get("token")
    except FileNotFoundError:
        return None, None
    except Exception as e:
        print("ERROR reading %s: %s" % (SESSION, e))
        return None, None


def main():
    api_key, token = _creds()
    if not api_key and not token:
        print("ERROR: %s not found or empty. Create it with {\"api_key\": \"...\"} "
              "(or {\"token\": \"...\"})." % SESSION)
        sys.exit(2)

    if api_key:
        url, method, body, hdr = API_URL, "GET", None, {"Authorization": "Bearer %s" % api_key}
        print("using API key auth")
    else:
        url, method, body, hdr = WEB_URL, "POST", b"{}", {"Authorization": "Bearer %s" % token}
        print("using web token auth")

    req = urllib.request.Request(url, headers=hdr, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        print("HTTP %s, %d bytes" % (resp.status, len(raw)))
    except urllib.error.HTTPError as e:
        print("HTTPError %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:500]))
        sys.exit(1)
    except Exception as e:
        print("ERROR: %s" % e)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except Exception as e:
        print("bad JSON: %s" % e)
        sys.exit(1)

    print("RAW JSON:")
    print(json.dumps(data, indent=2)[:2000])

    # Parse windows (mirror of agent/quota_providers/kimi.py)
    def blk(b):
        d = b.get("detail") or {}
        limit = b.get("limit", d.get("limit"))
        used = b.get("used", d.get("used"))
        rem = b.get("remaining", d.get("remaining"))
        reset = b.get("resetTime", d.get("resetTime"))
        pct = None
        if used is not None and limit not in (None, 0):
            try:
                pct = round(100.0 * float(used) / float(limit), 2)
            except (TypeError, ValueError):
                pass
        if rem is not None and limit not in (None, 0):
            try:
                pct = round(100.0 * (1 - float(rem) / float(limit)), 2)
            except (TypeError, ValueError):
                pass
        return b.get("scope") or b.get("window") or "?", pct, reset

    if isinstance(data.get("usage"), dict):
        l, p, r = blk(data["usage"])
        print("  window(%s): used=%s%% reset=%s" % (l, p, r))
    limits = data.get("limits")
    if isinstance(limits, dict):
        l, p, r = blk(limits)
        print("  limit(%s): used=%s%% reset=%s" % (l, p, r))
    elif isinstance(limits, list):
        for x in limits:
            if isinstance(x, dict):
                l, p, r = blk(x)
                print("  limit(%s): used=%s%% reset=%s" % (l, p, r))


if __name__ == "__main__":
    main()
