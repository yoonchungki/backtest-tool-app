# -*- coding: utf-8 -*-
"""
Vercel 서버리스 함수: 백테스트 도구용 과거 시세 조회 (/api/history).
종목코드 + 기간(하루~95일 이내)을 받아서 KIS "국내주식기간별시세"(inquire-daily-itemchartprice)를
호출해 일별 OHLCV+거래대금을 JSON으로 돌려줌. 계좌 조회가 아니라서 KIS_CANO/KIS_ACNT_PRDT_CD는 필요 없음.

긴 기간(예: 신규 종목 전체 히스토리)은 이 함수를 여러 번 나눠서 호출하는 방식으로 처리함
(프론트에서 ~90일 단위로 쪼개서 반복 호출 + 진행상황 표시) — 서버 쪽에서 한 번에 몇 년치를
다 처리하려고 하면 Vercel 함수 실행시간 제한에 걸릴 수 있어서 일부러 이렇게 나눔.
"""
import json
import os
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests


def load_config():
    is_paper = os.environ.get("KIS_MODE", "paper") == "paper"
    return {
        "app_key": os.environ["KIS_APP_KEY"],
        "app_secret": os.environ["KIS_APP_SECRET"],
        "base_url": "https://openapivts.koreainvestment.com:29443" if is_paper else "https://openapi.koreainvestment.com:9443",
    }


_token_cache = {}


def get_access_token(cfg):
    cached = _token_cache.get(cfg["app_key"])
    if cached and cached["expire_at"] > time.time() + 60:
        return cached["access_token"]

    url = cfg["base_url"] + "/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": cfg["app_key"], "appsecret": cfg["app_secret"]}
    resp = requests.post(url, json=body, timeout=15)
    if resp.status_code == 403:
        raise RuntimeError("KIS 토큰 재발급 제한에 걸렸습니다 (같은 앱키로 1분 안에 다시 요청함). 1분 정도 기다렸다가 다시 시도해주세요.")
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))
    _token_cache[cfg["app_key"]] = {"access_token": token, "expire_at": time.time() + expires_in}
    return token


def get_daily_price_chunk(cfg, token, stock_code, start_ymd, end_ymd):
    """한 번에 최대 ~95일치까지만 안전함(KIS 제한) — 그 이상은 호출한 쪽(프론트)에서 나눠서 반복 호출."""
    url = cfg["base_url"] + "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "authorization": f"Bearer {token}", "appkey": cfg["app_key"], "appsecret": cfg["app_secret"],
        "tr_id": "FHKST03010100", "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": start_ymd, "FID_INPUT_DATE_2": end_ymd,
        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "시세 조회에 실패했습니다")
    rows = {}
    for r in data.get("output2", []) or []:
        try:
            rows[r["stck_bsop_date"]] = {
                "date": r["stck_bsop_date"],
                "open": float(r["stck_oprc"]), "high": float(r["stck_hgpr"]),
                "low": float(r["stck_lwpr"]), "close": float(r["stck_clpr"]),
                "volume": float(r.get("acml_vol", 0) or 0),
                "value": float(r.get("acml_tr_pbmn", 0) or 0),
            }
        except (KeyError, ValueError):
            continue
    return sorted(rows.values(), key=lambda r: r["date"])


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
            stock = (qs.get("stock", [None])[0] or "").strip()
            start = (qs.get("start", [None])[0] or "").strip()
            end = (qs.get("end", [None])[0] or "").strip()
            if not stock or not start or not end:
                self._send_json(400, {"error": "stock/start/end 파라미터가 필요합니다 (YYYYMMDD)"})
                return

            start_dt = datetime.strptime(start, "%Y%m%d")
            end_dt = datetime.strptime(end, "%Y%m%d")
            if end_dt < start_dt:
                self._send_json(400, {"error": "종료일이 시작일보다 빠릅니다"})
                return
            # 안전장치: 한 번 요청에 95일을 넘기면 서버에서도 잘라서 처리(프론트가 이미 나눠 보내는 게 기본)
            if (end_dt - start_dt).days > 95:
                end_dt = start_dt + timedelta(days=95)

            cfg = load_config()
            token = get_access_token(cfg)
            rows = get_daily_price_chunk(cfg, token, stock, start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"))
            self._send_json(200, {"stock": stock, "rows": rows})
        except Exception as e:
            self._send_json(500, {"error": str(e)})
