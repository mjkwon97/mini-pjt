"""사용자 초기 설정(월·연 예산, 보유 ETF·목표 비중·초기 보유 수량)을 검사하고 저장한다. (LLM 호출 없음)

- 예산과 목표 비중은 config.yaml 에 쓴다. 주석을 지키려고 해당 값 줄만 바꾼다. 저장 전에 config.yaml.bak 으로 백업한다.
- 초기 보유 수량은 장부의 initial_holdings 표에 쓴다. 매수 기록(buys)은 바꾸지 않는다.
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from typing import Callable

from src import ledger, market

TICKER = re.compile(r"^[A-Z]{1,5}$")  # 미국 상장 ETF 코드만 (KRW·국내 상장 코드는 받지 않는다)
MAX_ETFS = 10


def _num(v) -> float | None:
    """숫자로 바꿀 수 있으면 float, 아니면 None."""
    try:
        f = float(v)
        return f if f == f else None  # NaN 제외
    except (TypeError, ValueError):
        return None


def validate(payload: dict, cfg: dict, price_ok: Callable[[str], bool] | None = None) -> dict:
    """입력값을 규칙으로 검사한다. 결과: {"errors": [...], "clean": {monthly_usd, yearly_usd, etfs: [...]}}

    payload: {"monthly_usd", "yearly_usd", "etfs": [{"ticker", "target_pct", "shares"}]}
    price_ok: 새 ETF 의 가격을 받을 수 있는지 확인하는 함수 (테스트에서는 바꿔 넣는다)
    """
    errors = []
    monthly, yearly = _num(payload.get("monthly_usd")), _num(payload.get("yearly_usd"))
    if monthly is None or monthly <= 0:
        errors.append("월 예산(USD)은 0보다 큰 숫자여야 합니다.")
    if yearly is None or yearly <= 0:
        errors.append("연 예산(USD)은 0보다 큰 숫자여야 합니다.")
    elif monthly and yearly < monthly:
        errors.append("연 예산은 월 예산보다 작을 수 없습니다.")

    rows = payload.get("etfs") or []
    if not rows:
        errors.append("ETF 를 하나 이상 넣어 주세요.")
    if len(rows) > MAX_ETFS:
        errors.append(f"ETF 는 최대 {MAX_ETFS}개까지 넣을 수 있습니다.")
    leveraged = {t.upper() for t in cfg["guardrails"]["leveraged_tickers"]}
    etfs, seen, total = [], set(), 0.0
    for i, r in enumerate(rows, 1):
        t = str(r.get("ticker", "")).strip().upper()
        w, sh = _num(r.get("target_pct")), _num(r.get("shares", 0) or 0)
        if not TICKER.match(t):
            errors.append(f"{i}번째 ETF 코드 '{t}' 가 올바르지 않습니다 (미국 상장 코드, 영문 대문자 1~5자).")
            continue
        if t in seen:
            errors.append(f"{t} 가 두 번 들어 있습니다.")
        if t in leveraged:
            errors.append(f"{t} 는 레버리지·인버스 상품이라 등록할 수 없습니다.")
        if w is None or not 0 < w <= 100:
            errors.append(f"{t} 목표 비중은 0보다 크고 100 이하인 % 값이어야 합니다.")
        if sh is None or sh < 0:
            errors.append(f"{t} 보유 수량은 0 이상이어야 합니다.")
        seen.add(t)
        total += w or 0
        etfs.append({"ticker": t, "target_pct": w, "shares": sh or 0.0})
    if etfs and abs(total - 100) > 0.01:
        errors.append(f"목표 비중 합계가 {total:g}% 입니다. 100% 가 되도록 맞춰 주세요.")
    if not errors and price_ok:
        for e in etfs:
            if e["ticker"] not in cfg["etfs"] and not price_ok(e["ticker"]):
                errors.append(f"{e['ticker']} 의 가격을 가져올 수 없습니다. 미국 상장 ETF 코드인지 확인해 주세요.")
    clean = {"monthly_usd": monthly, "yearly_usd": yearly, "etfs": etfs} if not errors else None
    return {"errors": errors, "clean": clean}


def _fmt(v: float) -> str:
    """정수면 소수점 없이, 아니면 소수 둘째 자리까지."""
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


def render_config(text: str, clean: dict) -> str:
    """config.yaml 원문에서 예산 두 줄과 etfs 블록만 바꾼 새 원문을 만든다. (주석·다른 설정은 그대로)"""
    text = re.sub(r"(?m)^(  monthly_usd:\s*)[^\s#]+", lambda m: m.group(1) + _fmt(clean["monthly_usd"]), text, count=1)
    text = re.sub(r"(?m)^(  yearly_usd:\s*)[^\s#]+", lambda m: m.group(1) + _fmt(clean["yearly_usd"]), text, count=1)
    block = "etfs:\n" + "".join(f"  {e['ticker']}: {e['target_pct'] / 100:.4g}\n" for e in clean["etfs"])
    new, n = re.subn(r"(?m)^etfs:\n(?:  .*\n)+", lambda m: block, text, count=1)
    if n != 1:
        raise ValueError("config.yaml 에서 etfs 블록을 찾지 못했습니다.")
    return new


def price_available(ticker: str) -> bool:
    """yfinance 에서 최근 종가를 받을 수 있는지."""
    try:
        return not market.fetch_daily(ticker, date.today(), days=10).empty
    except Exception:
        return False


def current(cfg: dict) -> dict:
    """설정 화면에 보여 줄 현재 값 (예산, ETF 별 목표 비중 %, 초기 보유 수량)."""
    conn = ledger.connect(cfg)
    try:
        init = ledger.initial_holdings(conn)
    finally:
        conn.close()
    return {"monthly_usd": cfg["budget"]["monthly_usd"], "yearly_usd": cfg["budget"]["yearly_usd"],
            "etfs": [{"ticker": t, "target_pct": round(w * 100, 2), "shares": init.get(t, 0.0)}
                     for t, w in cfg["etfs"].items()]}


def save(payload: dict) -> dict:
    """검사 후 저장한다. 결과: {"ok", "errors", "settings"}"""
    cfg = market.load_config()
    checked = validate(payload, cfg, price_ok=price_available)
    if checked["errors"]:
        return {"ok": False, "errors": checked["errors"], "settings": current(cfg)}
    clean = checked["clean"]
    path = market.CONFIG_PATH
    shutil.copyfile(path, path.with_suffix(".yaml.bak"))
    path.write_text(render_config(path.read_text(encoding="utf-8"), clean), encoding="utf-8")
    new_cfg = market.load_config()
    conn = ledger.connect(new_cfg)
    try:
        ledger.set_initial_holdings(conn, {e["ticker"]: e["shares"] for e in clean["etfs"] if e["shares"] > 0})
    finally:
        conn.close()
    from src import tools  # 등록 ETF 가 바뀌면 가격 캐시를 비운다
    tools._etf_prices_cached.cache_clear()
    tools._close_series_cached.cache_clear()
    return {"ok": True, "errors": [], "settings": current(new_cfg)}
