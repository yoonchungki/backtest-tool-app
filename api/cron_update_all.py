# -*- coding: utf-8 -*-
"""
Vercel Cron: 매일 장 마감 후(15:40 KST, 평일만) index.html의 "전체 종목 최신화" 버튼과 같은 일을
서버에서 자동으로 해줌 (/api/cron_update_all). PRESETS(가격 데이터)·시가총액에 이어 탭3 "미국ETF
비교"용 미국 ETF/환율 데이터도 Vercel Blob(api/usetf_store.py)에 갱신함(2026-09-11 추가 - 처음엔
그 데이터가 기기별 localStorage 전용이라 자동화 대상에서 뺐었는데, 이후 usetf_store.py로 공용
저장소를 만들면서 이 cron도 같이 갱신하도록 확장함). 실제 야후 파이낸스 조회는 이 파일에서
새로 하지 않고, 이미 배포된 /api/usetf(이 프로젝트 자기 자신의 API)를 HTTP로 호출해서 재사용함
- 야후 응답 파싱 로직을 세 번째로 복붙하지 않기 위해서(프론트 JS, api/usetf.py에 이미 있음).

cron 트리거는 Vercel Hobby 플랜에서 하루 1번만 허용되기 때문에(여러 번 나눠 호출하는 체이닝 불가),
한 번의 실행 안에서 최대한 처리하고, 그래도 시간 예산(TIME_BUDGET_SEC)을 넘기면 처리하다 만
상태를 그대로 Blob에 저장하고 조용히 끝냄 - 못 끝낸 나머지는 다음날 실행에서 "아직 오늘자가
아닌 종목"으로 다시 잡혀서 자연스럽게 이어짐(하루 정도 갱신이 늦어지는 것뿐이라 개인용 앱에서
문제없음 - 무리하게 한 번에 다 끝내려고 재귀/체이닝하다 오히려 복잡해지고 깨지기 쉬워지는 것보다 나음).
vercel.json에서 이 함수만 config.maxDuration을 늘려서(기본 10초 → 60초) 예산을 넉넉히 줌.

새 종목(한 번도 받아온 적 없는 코드, PRESETS에 데이터가 아예 없음)은 이 cron에서 다루지 않고
건너뜀 - 전체 기간(수년치) 첫 백필은 데이터량이 커서 하루 치 증분 갱신용 시간 예산을 초과하기
쉽고, 애초에 새 종목 추가는 사용자가 "새 종목 추가" 버튼으로 그때그때 직접 하는 흐름이라 자동화
대상이 아님.

인증: Vercel이 CRON_SECRET 환경변수를 보고 cron 트리거 요청에 자동으로
`Authorization: Bearer <CRON_SECRET>` 헤더를 붙여줌 - 이 값이 없거나 안 맞으면 거부해서, 외부에서
이 URL을 알아내 함부로 호출해(KIS API 쿼터 소모) KIS_APP_KEY/SECRET을 낭비하는 걸 막음.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import requests
import vercel_blob

BLOB_PATHNAME = "backtest-tool-presets-overrides.json"
USETF_BLOB_PATHNAME = "backtest-tool-usetf-overrides.json"  # api/usetf_store.py와 동일 경로
TIME_BUDGET_SEC = 50  # vercel.json에서 이 함수의 maxDuration=60으로 늘려둠 - 10초 여유를 두고 끊음

# index.html의 PEN_US_ETF_MAP 값들과 동일한 티커 목록(수동 동기화 필요 - index.html에서 매핑을
# 추가/삭제하면 여기도 같이 고쳐야 함). 환율("KRW=X")은 목록에 안 넣고 아래 코드에서 항상 별도로 더함.
US_ETF_TICKERS = ["QQQ", "SOXX", "SCHD", "GLD", "IEF", "SPY", "ASHR", "EWJ", "INDA"]

# index.html의 PRESET_INFO_LIST/BASE_PRESET_CODES와 동일한 목록(수동 동기화 필요 -
# index.html에서 기본 내장 종목을 추가/삭제하면 여기도 같이 고쳐야 함).
BASE_PRESET_INFO = [
    {"code": "069500", "name": "KODEX 200"},
    {"code": "114800", "name": "KODEX 인버스"},
    {"code": "122630", "name": "KODEX 레버리지"},
    {"code": "133690", "name": "TIGER 미국나스닥100"},
    {"code": "148020", "name": "KBSTAR 200"},
    {"code": "229200", "name": "KODEX 코스닥 150"},
    {"code": "233740", "name": "KODEX 코스닥150 레버리지"},
    {"code": "360750", "name": "TIGER 미국S&P500"},
    {"code": "381180", "name": "TIGER 미국필라델피아반도체나스닥"},
    {"code": "396500", "name": "TIGER Fn반도체TOP10"},
    {"code": "411060", "name": "KINDEX KRX금현물"},
    {"code": "458730", "name": "TIGER 미국배당다우존스"},
    {"code": "102960", "name": "KODEX 기계장비"},
    {"code": "305720", "name": "KODEX 2차전지산업"},
    {"code": "244580", "name": "KODEX 바이오"},
    {"code": "117460", "name": "KODEX 에너지화학"},
    {"code": "091180", "name": "KODEX 자동차"},
    {"code": "091160", "name": "KODEX 반도체"},
    {"code": "140700", "name": "KODEX 보험"},
    {"code": "117700", "name": "KODEX 건설"},
    {"code": "117680", "name": "KODEX 철강"},
    {"code": "102970", "name": "KODEX 증권"},
    {"code": "091170", "name": "KODEX 은행"},
    {"code": "266420", "name": "KODEX 헬스케어"},
    {"code": "140710", "name": "KODEX 운송"},
    {"code": "329650", "name": "KODEX TRF3070"},
    {"code": "329660", "name": "KODEX TRF5050"},
    {"code": "329670", "name": "KODEX TRF7030"},
    {"code": "114260", "name": "KODEX 국고채3년"},
    {"code": "284430", "name": "KODEX 200미국채혼합"},
    {"code": "273130", "name": "KODEX 종합채권(AA-이상)액티브"},
]


def _get_with_retry(url, headers=None, params=None, timeout=15, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            return requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


def load_config():
    is_paper = os.environ.get("KIS_MODE", "paper") == "paper"
    return {
        "app_key": os.environ["KIS_APP_KEY"],
        "app_secret": os.environ["KIS_APP_SECRET"],
        "base_url": "https://openapivts.koreainvestment.com:29443" if is_paper else "https://openapi.koreainvestment.com:9443",
    }


def get_access_token(cfg):
    url = cfg["base_url"] + "/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": cfg["app_key"], "appsecret": cfg["app_secret"]}
    resp = requests.post(url, json=body, timeout=15)
    if resp.status_code == 403:
        raise RuntimeError("KIS 토큰 재발급 제한에 걸렸습니다 (1분 안에 재요청함). 다음날 실행에서 이어집니다.")
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_daily_price_chunk(cfg, token, stock_code, start_ymd, end_ymd):
    url = cfg["base_url"] + "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "authorization": f"Bearer {token}", "appkey": cfg["app_key"], "appsecret": cfg["app_secret"],
        "tr_id": "FHKST03010100", "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": start_ymd, "FID_INPUT_DATE_2": end_ymd,
        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1",
    }
    resp = _get_with_retry(url, headers=headers, params=params, timeout=15)
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


def get_market_cap(cfg, token, stock_code):
    url = cfg["base_url"] + "/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "authorization": f"Bearer {token}", "appkey": cfg["app_key"], "appsecret": cfg["app_secret"],
        "tr_id": "FHKST01010100", "custtype": "P",
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
    resp = _get_with_retry(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "시가총액 조회에 실패했습니다")
    raw = (data.get("output") or {}).get("hts_avls")
    if raw is None:
        raise RuntimeError("시가총액 정보가 없습니다")
    return float(raw)


def ymd_add_days(ymd, days):
    d = datetime.strptime(ymd, "%Y%m%d") + timedelta(days=days)
    return d.strftime("%Y%m%d")


def today_ymd_kst():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")


def fetch_history_range(cfg, token, code, start_ymd, end_ymd, deadline):
    """KIS는 한 번에 최대 ~95일까지만 줘서 90일 단위로 나눠 호출 (index.html fetchHistoryRange와 동일 패턴).
       deadline을 넘기면 그때까지 모은 것만 반환(부분 완료 허용)."""
    cursor, all_rows = start_ymd, []
    while cursor <= end_ymd:
        if time.time() > deadline:
            break
        chunk_end = ymd_add_days(cursor, 90)
        if chunk_end > end_ymd:
            chunk_end = end_ymd
        all_rows.extend(get_daily_price_chunk(cfg, token, code, cursor, chunk_end))
        cursor = ymd_add_days(chunk_end, 1)
    return all_rows


def merge_price_data(ds, rows, stock_name, stock_code):
    """index.html의 rowsToColumnar + mergeStockData와 동일한 동작(날짜 기준 병합, 오름차순 정렬)."""
    by_date = {}
    if ds:
        for i, d in enumerate(ds.get("dates") or []):
            ymd = d.replace("-", "")
            by_date[ymd] = {
                "date": ymd, "open": ds["open"][i], "high": ds["high"][i], "low": ds["low"][i],
                "close": ds["close"][i], "volume": ds["volume"][i], "value": ds["tradingValue"][i],
            }
    for r in rows:
        by_date[r["date"]] = r
    ordered = sorted(by_date.values(), key=lambda r: r["date"])
    return {
        "dates": [f"{r['date'][0:4]}-{r['date'][4:6]}-{r['date'][6:8]}" for r in ordered],
        "open": [r["open"] for r in ordered], "high": [r["high"] for r in ordered],
        "low": [r["low"] for r in ordered], "close": [r["close"] for r in ordered],
        "volume": [r["volume"] for r in ordered], "tradingValue": [r["value"] for r in ordered],
        "stockName": stock_name, "stockCode": stock_code,
    }


def _find_blob_url():
    result = vercel_blob.list({"prefix": BLOB_PATHNAME, "limit": "1"})
    for b in result.get("blobs", []) or []:
        if b.get("pathname") == BLOB_PATHNAME:
            return b.get("url")
    return None


def load_saved():
    url = _find_blob_url()
    if not url:
        return {"presets": {}, "infoList": [], "marketCap": {}, "deletedCodes": []}
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    data.setdefault("presets", {})
    data.setdefault("infoList", [])
    data.setdefault("marketCap", {})
    data.setdefault("deletedCodes", [])
    return data


def save_saved(saved):
    body = json.dumps(saved, ensure_ascii=False).encode("utf-8")
    vercel_blob.put(BLOB_PATHNAME, body, {
        "addRandomSuffix": "false", "allowOverwrite": "true", "contentType": "application/json",
    })


def _find_usetf_blob_url():
    result = vercel_blob.list({"prefix": USETF_BLOB_PATHNAME, "limit": "1"})
    for b in result.get("blobs", []) or []:
        if b.get("pathname") == USETF_BLOB_PATHNAME:
            return b.get("url")
    return None


def load_usetf_saved():
    url = _find_usetf_blob_url()
    if not url:
        return {"usEtf": {}, "usdKrw": {"dates": [], "close": []}}
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    data.setdefault("usEtf", {})
    data.setdefault("usdKrw", {"dates": [], "close": []})
    return data


def save_usetf_saved(saved):
    body = json.dumps(saved, ensure_ascii=False).encode("utf-8")
    vercel_blob.put(USETF_BLOB_PATHNAME, body, {
        "addRandomSuffix": "false", "allowOverwrite": "true", "contentType": "application/json",
    })


def merge_series(base, pairs):
    """index.html의 mergeSeriesData()와 동일한 동작(날짜 기준 병합, 오름차순 정렬) - 가격 캔들이 아니라
       날짜당 값 하나짜리(ETF 종가, 환율) 시계열용이라 merge_price_data()보다 훨씬 단순함."""
    by_date = {}
    dates = base.get("dates") or []
    closes = base.get("close") or []
    for d, v in zip(dates, closes):
        by_date[d] = v
    for d, v in pairs:
        if v is not None:
            by_date[d] = v
    ordered_dates = sorted(by_date.keys())
    return {"dates": ordered_dates, "close": [by_date[d] for d in ordered_dates]}


def run_usetf_update(deadline, host):
    """탭3 "미국ETF비교"용 데이터 갱신 - 실제 야후 파이낸스 조회는 이미 배포된 /api/usetf를 그대로
       재사용(이 프로젝트 자기 자신을 HTTP로 호출). index.html의 updateUsEtfAndFxData()와 동일한
       "가장 오래 뒤처진 심볼 기준으로 한 번에 조회" 방식."""
    if time.time() > deadline:
        return {"skipped": "timeout_before_start"}
    saved = load_usetf_saved()
    series_by_symbol = {t: (saved["usEtf"].get(t) or {"dates": [], "close": []}) for t in US_ETF_TICKERS}
    series_by_symbol["KRW=X"] = saved["usdKrw"]

    today = today_ymd_kst()
    today_dash = f"{today[0:4]}-{today[4:6]}-{today[6:8]}"
    earliest_start = None
    for ds in series_by_symbol.values():
        last_date = ds["dates"][-1] if ds["dates"] else "2000-01-01"
        if last_date >= today_dash:
            continue
        next_ymd = ymd_add_days(last_date.replace("-", ""), 1)
        if earliest_start is None or next_ymd < earliest_start:
            earliest_start = next_ymd
    if earliest_start is None:
        return {"addedDays": 0, "errors": {}}

    symbols = list(series_by_symbol.keys())
    try:
        resp = requests.get(
            f"https://{host}/api/usetf",
            params={"symbols": ",".join(symbols), "start": earliest_start, "end": today},
            timeout=min(30, max(5, deadline - time.time())),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}

    added_days = 0
    for sym, pairs in (data.get("results") or {}).items():
        ds = series_by_symbol.get(sym)
        if not ds or not pairs:
            continue
        before_len = len(ds["dates"])
        merged = merge_series(ds, pairs)
        if sym == "KRW=X":
            saved["usdKrw"] = merged
        else:
            saved["usEtf"][sym] = merged
        added_days += max(0, len(merged["dates"]) - before_len)
    save_usetf_saved(saved)
    return {"addedDays": added_days, "errors": data.get("errors") or {}}


def run_update(deadline):
    saved = load_saved()
    deleted = set(saved.get("deletedCodes") or [])
    name_by_code = {p["code"]: p["name"] for p in BASE_PRESET_INFO}
    for p in (saved.get("infoList") or []):
        if p.get("code"):
            name_by_code[p["code"]] = p.get("name") or p["code"]
    codes = sorted(c for c in name_by_code if c not in deleted)

    today = today_ymd_kst()
    cfg = load_config()
    token = get_access_token(cfg)

    updated, skipped, no_data, failed, timed_out = [], [], [], {}, []
    for code in codes:
        if time.time() > deadline:
            timed_out.append(code)
            continue
        ds = saved["presets"].get(code)
        if not ds or not ds.get("dates"):
            no_data.append(code)  # 새 종목 첫 백필은 여기서 안 함 - "새 종목 추가" 버튼으로 직접
            continue
        last_ymd = ds["dates"][-1].replace("-", "")
        if last_ymd >= today:
            skipped.append(code)
            continue
        try:
            rows = fetch_history_range(cfg, token, code, ymd_add_days(last_ymd, 1), today, deadline)
            if rows:
                saved["presets"][code] = merge_price_data(ds, rows, name_by_code.get(code, ds.get("stockName")), code)
                updated.append(code)
            else:
                skipped.append(code)
        except Exception as e:
            failed[code] = str(e)

    # 진행 상황(가격 데이터)을 먼저 저장 - 아래 시가총액 단계에서 시간이 모자라도 여기까지는 반드시 남게 함.
    save_saved(saved)

    cap_updated, cap_failed, cap_timed_out = [], {}, []
    for code in codes:
        if time.time() > deadline:
            cap_timed_out.append(code)
            continue
        try:
            saved["marketCap"][code] = get_market_cap(cfg, token, code)
            cap_updated.append(code)
        except Exception as e:
            cap_failed[code] = str(e)
    save_saved(saved)

    host = os.environ.get("VERCEL_URL") or "backtest-tool-app.vercel.app"
    usetf_result = run_usetf_update(deadline, host)

    return {
        "date": today,
        "codesTotal": len(codes),
        "priceUpdated": len(updated), "priceSkipped": len(skipped),
        "priceNoData": no_data, "priceFailed": failed, "priceTimedOut": timed_out,
        "capUpdated": len(cap_updated), "capFailed": cap_failed, "capTimedOut": cap_timed_out,
        "usEtf": usetf_result,
    }


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self):
        secret = os.environ.get("CRON_SECRET")
        if not secret:
            return False  # 설정 전엔 항상 거부 - KIS_APP_KEY/SECRET을 아무나 호출해 소모하지 못하게 함
        return self.headers.get("Authorization") == f"Bearer {secret}"

    def do_GET(self):
        if not self._is_authorized():
            self._send_json(401, {"error": "unauthorized (CRON_SECRET 환경변수를 설정했는지 확인하세요)"})
            return
        try:
            deadline = time.time() + TIME_BUDGET_SEC
            result = run_update(deadline)
            self._send_json(200, result)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
