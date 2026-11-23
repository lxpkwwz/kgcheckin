#!/usr/bin/env python3
"""Poll 100milionseek sign-up page and push an alert when slots are available.

Usage:
    python3 slot_monitor.py --webhook https://your-webhook-url
    WEBHOOK_URL=https://... python3 slot_monitor.py --interval 10

Env / flags:
    WEBHOOK_URL      alert endpoint (any URL that accepts a POST)
    WEBHOOK_FORMAT   json (default) | text | feishu
    --once           check once, print state, exit
    --interval N     seconds between checks (default 10)
    --cooldown N     minimum seconds between repeated "slots open" alerts (default 300)
    --log PATH       also append logs to PATH
    --verbose        log every check, not just changes/alerts
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

SIGNUP_URL = "https://100milionseek.top/signup"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

COUNT_RE = re.compile(r"data-slot-count[^>]*>\s*([\d,]+)\s+of\s+([\d,]+)\s+slots?", re.I)
COUNT_FALLBACK_RE = re.compile(r"([\d,]+)\s+of\s+([\d,]+)\s+slots available", re.I)
METER_RE = re.compile(r'<meter[^>]*value="(\d+)"', re.I)
RESET_RE = re.compile(r'data-slot-reset[^>]*>.*?<time[^>]*datetime="([^"]+)"', re.I | re.S)

OPEN_TEXT = "sign-ups are paused"
OPEN_TEXT_ZH = "注册"


def log(msg, path=None):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}"
    print(line, flush=True)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def fetch():
    req = urllib.request.Request(
        SIGNUP_URL,
        headers={"User-Agent": UA, "Accept": "text/html", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def parse(html):
    available = total = None
    m = COUNT_RE.search(html) or COUNT_FALLBACK_RE.search(html)
    if m:
        available, total = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    else:
        m = METER_RE.search(html)
        if m:
            available = int(m.group(1))
            tm = re.search(r'max="(\d+)"', m.group(0))
            total = int(tm.group(1)) if tm else None
    reset = None
    rm = RESET_RE.search(html)
    if rm:
        reset = rm.group(1)
    paused = OPEN_TEXT in html.lower()
    return {
        "available": available,
        "total": total,
        "reset_at": reset,
        "paused": paused,
    }


def notify(webhook, fmt, title, text, data):
    if not webhook:
        return False
    if fmt == "text":
        body = text.encode("utf-8")
        headers = {"Content-Type": "text/plain; charset=utf-8", "Title": title}
    elif fmt == "feishu":
        body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        payload = {"title": title, "text": text, **data}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

    for attempt in (1, 2):
        try:
            req = urllib.request.Request(webhook, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    return True
        except Exception as e:  # noqa: BLE001 - keep monitoring alive on any transport error
            if attempt == 2:
                log(f"webhook failed: {e}")
            else:
                time.sleep(3)
    return False


def describe(state):
    if state["available"] is None:
        return "signup page has no slot counter (sign-ups may be fully open)"
    s = f"{state['available']} of {state['total']} slots available"
    if state["reset_at"]:
        s += f" (next reset {state['reset_at']})"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("WEBHOOK_URL", ""))
    ap.add_argument("--format", default=os.environ.get("WEBHOOK_FORMAT", "json"),
                    choices=["json", "text", "feishu"])
    ap.add_argument("--interval", type=float, default=10)
    ap.add_argument("--cooldown", type=float, default=300)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-alert", action="store_true", help="send one test alert and exit")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--log", default="")
    a = ap.parse_args()

    last_state = None
    last_alert = 0.0
    errors = 0

    if a.test_alert:
        ok = notify(
            a.webhook, a.format, "100milionseek: test alert",
            f"测试推送成功。监控目标：{SIGNUP_URL}", {},
        )
        log(f"test alert sent={ok} webhook={'set' if a.webhook else 'missing'}", a.log)
        return 0 if ok else 1

    while True:
        try:
            state = parse(fetch())
            errors = 0
        except Exception as e:  # noqa: BLE001 - network hiccups should not kill the loop
            errors += 1
            log(f"fetch failed ({errors}): {e}", a.log)
            if a.once:
                return 1
            time.sleep(min(60, a.interval * errors))
            continue

        available = state["available"]
        open_now = state["paused"] is False or (available or 0) > 0
        if a.verbose or state != last_state or open_now:
            log(("OPEN " if open_now else "closed ") + describe(state), a.log)

        now = time.time()
        if open_now and now - last_alert >= a.cooldown:
            if available:
                text = (
                    f"100milionseek 有槽位了：{available}/{state['total']} 可注册。"
                    f" 马上去抢：{SIGNUP_URL}"
                )
            else:
                text = f"100milionseek 注册已开放（无槽位计数）。马上去：{SIGNUP_URL}"
            title = "100milionseek: sign-up slots open"
            ok = notify(a.webhook, a.format, title, text, state)
            log(f"ALERT sent={ok} webhook={'set' if a.webhook else 'missing'}", a.log)
            last_alert = now  # retry at most once per cooldown, never every loop

        last_state = state
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
