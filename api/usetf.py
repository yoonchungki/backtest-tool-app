# -*- coding: utf-8 -*-
"""
Vercel 서버리스 함수: 탭3(개인연금 보유전략) "미국ETF비교"용 미국 ETF/환율 시세 조회 (/api/usetf).
야후 파이낸스 차트 API(query1.finance.yahoo.com)를 프록시함 - 브라우저에서 이 API를 직접 fetch하면
CORS로 막혀서(Access-Control-Allow-Origin 헤더가 없음), KIS 시세 조회(api/history.py)와 동일하게
서버 함수를 한 번 거쳐서 받아옴.

`?symbols=QQQ,SOXX,KRW=X&start=YYYYMMDD&end=YYYYMMDD`로 여러 심볼을 한 번에 조회함(개별 요청을
심볼 수만큼 반복하면 왕복이 늘어나서 배치로 묶음 - marketcap_batch와 같은 이유).
"KRW=X"가 야후 파이낸스의 USD/KRW 환율 심볼.
"""
import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_symbol(symbol, start_dt, end_dt, time_budget_left):
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp()) + 86400  # end 날짜 당일도 포함되도록 하루 더함
    resp = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"period1": period1, "period2": period2, "interval": "1d"},
        headers=HEADERS, timeout=min(15, max(3, time_budget_left)),
    )
    resp.raise_for_status()
    data = resp.json()
    result = (data.get("chart") or {}).get("result")
    if not result:
        err = (data.get("chart") or {}).get("error") or {}
        raise RuntimeError(err.get("description") or "야후 파이낸스 조회 결과가 없습니다")

    r = result[0]
    ts = r.get("timestamp") or []
    indicators = r.get("indicators", {}) or {}
    adjclose = ((indicators.get("adjclose") or [{}])[0] or {}).get("adjclose") or []
    close = ((indicators.get("quote") or [{}])[0] or {}).get("close") or []

    rows = {}
    for i, t in enumerate(ts):
        val = adjclose[i] if i < len(adjclose) and adjclose[i] is not None else (close[i] if i < len(close) else None)
        if val is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        rows[d] = round(float(val), 4)
    return sorted(rows.items())


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            symbols_raw = (qs.get("symbols", [None])[0] or "").strip()
            start = (qs.get("start", [None])[0] or "").strip()
            end = (qs.get("end", [None])[0] or "").strip()
            if not symbols_raw or not start or not end:
                self._send_json(400, {"error": "symbols/start/end 파라미터가 필요합니다 (YYYYMMDD)"})
                return

            symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
            start_dt = datetime.strptime(start, "%Y%m%d").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
            if end_dt < start_dt:
                self._send_json(400, {"error": "종료일이 시작일보다 빠릅니다"})
                return

            results, errors = {}, {}
            deadline = time.time() + 8.5  # Vercel 함수 기본 실행시간 제한(10초) 안에서 여유를 둠
            for symbol in symbols:
                remaining = deadline - time.time()
                if remaining <= 0:
                    errors[symbol] = "시간 초과로 처리하지 못했습니다(다시 시도하면 나머지가 처리됩니다)"
                    continue
                try:
                    results[symbol] = fetch_symbol(symbol, start_dt, end_dt, remaining)
                except Exception as e:
                    errors[symbol] = str(e)

            self._send_json(200, {"results": results, "errors": errors})
        except Exception as e:
            self._send_json(500, {"error": str(e)})
