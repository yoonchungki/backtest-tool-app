# -*- coding: utf-8 -*-
"""
Vercel 서버리스 함수: 탭3 "미국ETF비교"용 미국 ETF/환율 시세를 Vercel Blob에 저장/조회 (/api/usetf_store).
지금까지 이 데이터는 기기별 localStorage에만 저장됐음(api/usetf.py 파일 헤더 주석 참고 - PRESETS
Blob에 900KB+를 매번 얹지 않으려고 일부러 분리해뒀던 것) - 그래서 "전체 종목 최신화"를 자동화(cron,
api/cron_update_all.py)해도 이 데이터만은 각 브라우저에 반영될 방법이 없었음. 여기에 별도 공용
저장소를 둬서, cron이 갱신한 값이나 어느 한 기기에서 받은 값을 모든 기기가 받아볼 수 있게 함.
GET/POST 패턴은 api/presets.py와 동일("마지막 저장이 이김", 병합 없음 - 개인용 앱이라 충돌
가능성 낮음). 이 파일은 저장/조회만 담당 - 실제 야후 파이낸스 조회는 api/usetf.py가 계속 맡음.
"""
import json
from http.server import BaseHTTPRequestHandler

import requests
import vercel_blob

BLOB_PATHNAME = "backtest-tool-usetf-overrides.json"


def _find_blob_url():
    result = vercel_blob.list({"prefix": BLOB_PATHNAME, "limit": "1"})
    for b in result.get("blobs", []) or []:
        if b.get("pathname") == BLOB_PATHNAME:
            return b.get("url")
    return None


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            url = _find_blob_url()
            if not url:
                self._send_json(200, {"usEtf": {}, "usdKrw": {"dates": [], "close": []}})
                return
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            self._send_json(200, resp.json())
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            vercel_blob.put(BLOB_PATHNAME, body, {
                "addRandomSuffix": "false", "allowOverwrite": "true", "contentType": "application/json",
            })
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._send_json(500, {"error": str(e)})
