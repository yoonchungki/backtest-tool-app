# -*- coding: utf-8 -*-
"""
Vercel 서버리스 함수: 종목 추가/최신화 데이터(PRESETS 오버라이드)를 Vercel Blob에 저장/조회 (/api/presets).
지금까지는 이 데이터가 localStorage(기기별)에만 저장돼서 PC에서 추가한 종목이 폰에는 안 보였는데,
여기에 공용 저장소를 두고 페이지 로드 시 여기서도 받아오게 해서 기기 간에 보이게 함.
동시 저장 충돌은 "마지막 저장이 이김"으로 단순하게 처리(별도 병합 로직 없음 - 개인용 앱이라 충돌 가능성 낮음).
"""
import json
from http.server import BaseHTTPRequestHandler

import requests
import vercel_blob

BLOB_PATHNAME = "backtest-tool-presets-overrides.json"


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
                self._send_json(200, {"presets": {}, "infoList": []})
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
