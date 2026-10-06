#!/usr/bin/env python3
"""일일 파이프라인 실패 등 운영 알림을 Telegram으로 보낸다(브리핑 발송과 무관한 별도 채널 가능).

  python3 scripts/tg_notify.py "메시지 본문"

환경변수(.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID(또는 OPS_TELEGRAM_CHAT_ID로 별도 지정 시 그쪽 우선).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: tg_notify.py <message>", file=sys.stderr)
        return 2
    message = sys.argv[1][:4000]  # Telegram 메시지 길이 상한 여유

    _load_dotenv(Path(__file__).parent.parent / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("OPS_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHANNEL_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID 미설정 — 알림 건너뜀", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"Telegram 전송 실패: HTTP {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Telegram 전송 실패: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
