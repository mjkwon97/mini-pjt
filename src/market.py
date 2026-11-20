"""시장 데이터 조회, 신호 점수 계산, 데이터 신선도 판정.

규칙 숫자(임계값·티커·기간)는 모두 config.yaml 에서 읽는다.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import yaml
import yfinance as yf
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """config.yaml 을 읽어 dict 로 돌려준다."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def last_completed_session(calendar: str, now: datetime | None = None) -> date:
    """지금 시각 기준으로 장이 끝난 가장 최근 거래일(예: XNYS)을 돌려준다.

    휴장일과 주말은 거래소 달력으로 건너뛴다. 오늘 장이 아직 안 끝났으면 그 전 거래일이다.
    """
    now = now or datetime.now(timezone.utc)
    cal = mcal.get_calendar(calendar)
    sched = cal.schedule(start_date=(now - timedelta(days=14)).date(), end_date=now.date())
    closed = sched[sched["market_close"] <= pd.Timestamp(now)]
    return closed.index[-1].date()


def latest_started_session(calendar: str, now: datetime | None = None) -> date:
    """지금 시각 기준으로 장이 이미 열린 가장 최근 거래일. (체결 기록의 기본 체결일)

    한국 밤에 장중 매수하고 바로 기록해도, 한국 아침에 어젯밤 체결을 기록해도 그 미국 거래일이 된다.
    """
    now = now or datetime.now(timezone.utc)
    sched = mcal.get_calendar(calendar).schedule(start_date=(now - timedelta(days=14)).date(), end_date=now.date())
    started = sched[sched["market_open"] <= pd.Timestamp(now)]
    return started.index[-1].date()


def first_session(calendar: str, ym: str) -> date:
    """YYYY-MM 달의 첫 거래일 (1일 일괄 매수 비교 기준일)."""
    y, m = map(int, ym.split("-"))
    start = date(y, m, 1)
    return mcal.get_calendar(calendar).schedule(start_date=start, end_date=start + timedelta(days=10)).index[0].date()


def close_on(series: pd.Series, day: date) -> tuple[float, str] | None:
    """종가 시계열에서 day 당일(없으면 그 전 가장 가까운 날) 종가와 그 날짜."""
    s = series[series.index <= pd.Timestamp(day)]
    if s.empty:
        return None
    return float(s.iloc[-1]), s.index[-1].date().isoformat()


def is_session(calendar: str, day: date) -> bool:
    """그날이 거래소 거래일인지 (주말·휴장일이면 False)."""
    return not mcal.get_calendar(calendar).schedule(start_date=day, end_date=day).empty


def trade_week_info(calendar: str, now: datetime | None = None) -> dict:
    """오늘 매수할 거래일과, 그달 남은 거래 주 수·주 마지막 거래일 여부를 돌려준다.

    매수일 = 직전 완료 거래일 다음 거래일 (한국 아침 기준 오늘 밤 미국 장).
    거래 주 = ISO 주(월~일) 중 그달에 속한 부분. 매수일이 속한 주를 포함해 센다.
    """
    now = now or datetime.now(timezone.utc)
    done = last_completed_session(calendar, now)
    cal = mcal.get_calendar(calendar)
    days = cal.schedule(start_date=done + timedelta(days=1), end_date=done + timedelta(days=45)).index.date
    trade_day = days[0]
    month_days = [d for d in days if (d.year, d.month) == (trade_day.year, trade_day.month)]
    weeks = sorted({d.isocalendar()[:2] for d in month_days})
    same_week = [d for d in month_days if d.isocalendar()[:2] == trade_day.isocalendar()[:2]]
    return {
        "trade_date": trade_day.isoformat(),
        "weeks_left": len(weeks),
        "is_week_last_day": trade_day == same_week[-1],
        "is_month_last_day": trade_day == month_days[-1],
    }


def _yf_session():
    """yfinance 용 HTTP 세션을 만든다.

    프로젝트 경로에 한글이 있으면 curl 이 기본 인증서 파일을 못 읽으므로,
    .env 의 YF_CA_BUNDLE(영문 경로의 cacert.pem)이 있으면 그 파일을 쓴다.
    """
    ca = os.getenv("YF_CA_BUNDLE")
    return curl_requests.Session(impersonate="chrome", verify=ca) if ca else None


def fetch_daily(ticker: str, until: date, days: int = 60) -> pd.DataFrame:
    """yfinance 로 일봉을 받아 until 날짜까지만 남겨 돌려준다. (장중 미완성 봉 제외)"""
    df = yf.Ticker(ticker, session=_yf_session()).history(period=f"{days}d", interval="1d", auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"{ticker} 데이터를 받지 못했다")
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df.dropna(subset=["Close"])  # 값이 비어 있는 봉 제외
    return df[df.index <= pd.Timestamp(until)]


def compute_signals(index_df: pd.DataFrame, vix_df: pd.DataFrame, cfg: dict) -> dict:
    """지수·VIX 일봉으로 신호 점수와 항목별 근거를 계산한다. (네트워크 호출 없음)

    항목: 하루 등락률, 20일 고점 대비, 하락일 거래량 배수, VIX.
    """
    s = cfg["signals"]
    n = cfg["market"]["lookback_days"]
    close, high, vol = index_df["Close"], index_df["High"], index_df["Volume"]

    ret = float(close.iloc[-1] / close.iloc[-2] - 1)
    high_n = float(high.iloc[-n:].max())
    close_ratio = float(close.iloc[-1] / high_n)
    avg_vol = float(vol.iloc[-n - 1:-1].mean())
    vol_ratio = float(vol.iloc[-1] / avg_vol) if avg_vol else 0.0
    vix = float(vix_df["Close"].iloc[-1])

    items = []

    pts = 2 if ret <= s["drop_2"] else 1 if ret <= s["drop_1"] else 0
    items.append({"name": "daily_return", "label": "S&P 500 하루 등락률", "value": round(ret * 100, 2), "unit": "%",
                  "rule": f"<= {s['drop_1']*100:.0f}% +1, <= {s['drop_2']*100:.0f}% +2", "points": pts})

    pts = 1 if close_ratio <= s["below_high_ratio"] else 0
    items.append({"name": "close_vs_20d_high", "label": "20일 고점 대비 종가", "value": round(close_ratio * 100, 2), "unit": "%",
                  "rule": f"<= {s['below_high_ratio']*100:.0f}% +1", "points": pts})

    pts = 1 if (ret < 0 and vol_ratio >= s["volume_ratio"]) else 0
    items.append({"name": "down_day_volume_ratio", "label": "하락일 거래량 배수(20일 평균 대비)", "value": round(vol_ratio, 2), "unit": "x",
                  "rule": f"하락일이고 >= {s['volume_ratio']}x +1", "points": pts})

    pts = 2 if vix >= s["vix_2"] else 1 if vix >= s["vix_1"] else 0
    items.append({"name": "vix", "label": "VIX", "value": round(vix, 2), "unit": "",
                  "rule": f">= {s['vix_1']} +1, >= {s['vix_2']} +2", "points": pts})

    return {
        "score": sum(i["points"] for i in items),
        "items": items,
        "index_close": round(float(close.iloc[-1]), 2),
    }


def get_market_signals(cfg: dict | None = None, now: datetime | None = None) -> dict:
    """어젯밤 미국 시장 신호를 계산하고 데이터 신선도를 판정한다.

    데이터 기준일이 직전 NYSE 거래일과 같으면 freshness=OK, 아니면 STALE.
    STALE 이면 신호 기반 추천에 쓰지 않는다. 데이터를 못 받으면 freshness=ERROR.
    """
    cfg = cfg or load_config()
    m = cfg["market"]
    expected = last_completed_session(m["calendar"], now)
    try:
        idx = fetch_daily(m["index_ticker"], expected)
        vix = fetch_daily(m["vix_ticker"], expected)
    except Exception as e:  # 네트워크·티커 오류
        return {"freshness": "ERROR", "error": str(e), "expected_session": expected.isoformat()}

    data_date = idx.index[-1].date()
    result = compute_signals(idx, vix, cfg)
    result.update({
        "data_date": data_date.isoformat(),
        "expected_session": expected.isoformat(),
        "freshness": "OK" if data_date == expected and vix.index[-1].date() == expected else "STALE",
    })
    return result


if __name__ == "__main__":
    print(json.dumps(get_market_signals(), ensure_ascii=False, indent=2))
