"""대시보드 화면에 쓸 숫자를 모은다. LLM 을 부르지 않고, 도구와 같은 함수(engine·market·ledger)만 쓴다.

GET /api/dashboard 가 이 결과를 그대로 돌려준다. 규칙 판정은 하지 않고 기존 함수 결과를 묶는다.
화면용으로 더하는 것은 목표 금액과의 차이(with_gap_usd)와 ETF 가격 기준일 확인(price_check) 두 가지뿐이다.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from src import ledger, market, tools
from src import settings as user_settings

ROOT = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")


def month_sessions_left(calendar: str, trade_date: str) -> dict:
    """매수일부터 그달 말까지 남은 거래일 수와 월 마지막 거래일."""
    day = date.fromisoformat(trade_date)
    end = (date(day.year + (day.month == 12), day.month % 12 + 1, 1) - timedelta(days=1))
    days = mcal.get_calendar(calendar).schedule(start_date=day, end_date=end).index.date
    return {"sessions_left": len(days), "month_last_session": days[-1].isoformat() if len(days) else None}


def explanation(order: dict, alloc: dict) -> list[str]:
    """추천 근거 문장. 도구 결과 값으로만 만든다 (숫자를 새로 계산하지 않는다)."""
    lines = []
    if order.get("freshness") == "OK":
        items = ", ".join(f"{i.get('label', i['name'])} {i['value']}{i.get('unit', '')} (+{i['points']})"
                          for i in order.get("signal_items", []))
        lines.append(f"어젯밤({order.get('data_date')}) 신호 점수 {order.get('signal_score')}점: {items}.")
    else:
        lines.append(f"시장 데이터가 최신이 아닙니다 (신선도 {order.get('freshness')}). 신호 기반 판단은 보류합니다.")
    lines.append(order.get("reason", ""))
    under = alloc.get("most_underweight")
    row = next((r for r in alloc.get("etfs", []) if r["etf"] == under), None)
    if row:
        lines.append(f"{under} 비중이 {row['current_pct']}%로 목표 {row['target_pct']}%보다 "
                     f"{abs(row['current_minus_target_pct'])}%p 낮아 가장 모자랍니다.")
    lines += order.get("rule_notes", [])
    return [l for l in lines if l]


def with_gap_usd(alloc: dict) -> dict:
    """비중 결과에 ETF별 목표 금액과의 차이(USD)를 붙인다. 목표 평가액 − 현재 평가액, 양수면 부족·음수면 초과."""
    total = alloc.get("total_value_usd") or 0
    rows = [{**r, "gap_usd": round(r["target_pct"] / 100 * total - r["value_usd"], 2)} for r in alloc.get("etfs", [])]
    fields = {**alloc.get("fields", {}), "gap_usd": "목표 평가액 − 현재 평가액 (USD). 양수면 부족, 음수면 초과"}
    return {**alloc, "etfs": rows, "fields": fields}


def price_check(alloc: dict, expected_session: str | None) -> dict:
    """ETF 가격 기준일이 직전 거래일(신호 기준일)과 같은지 본다. 다르거나 가격이 없으면 stale 에 담는다."""
    dates = alloc.get("price_dates", {})
    names = [r["etf"] for r in alloc.get("etfs", [])] or list(dates)
    stale = {t: dates.get(t) for t in names if dates.get(t) != expected_session}
    return {"expected_session": expected_session, "ok": not stale, "stale": stale}


def backtest_summary() -> dict | None:
    """evaluation/backtest_months.csv 가 있으면 이긴 달·진 달 수와 평가액 차이를 요약한다."""
    path = ROOT / "evaluation" / "backtest_months.csv"
    if not path.exists():
        return None
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if not rows:
        return None
    va = sum(float(r["actual_value_usd"]) for r in rows)
    vb = sum(float(r["first_day_value_usd"]) for r in rows)
    return {"period": f"{rows[0]['month']} ~ {rows[-1]['month']}", "months": len(rows),
            "win": sum(r["result"] == "WIN" for r in rows), "lose": sum(r["result"] == "LOSE" for r in rows),
            "value_diff_usd": round(va - vb, 2), "value_diff_pct": round((va / vb - 1) * 100, 2)}


def build_dashboard() -> dict:
    """대시보드 한 화면 분량의 데이터를 만든다."""
    cfg = market.load_config()
    sig = tools._signals()
    ctx = tools.budget_context(cfg)
    order = tools.compute_order(cfg, ctx=ctx, sig=sig)
    alloc = tools.allocation_view(cfg, ctx=ctx)
    conn = ledger.connect(cfg)
    try:
        month_buys = ledger.buys_in_month(conn, ctx["budget_month"])
    finally:
        conn.close()
    compare = tools.compare_data(cfg, "year", current=ctx["budget_month"])
    pending = None
    from src import agent as agent_module  # 승인 대기 상태 (채팅에서 생긴 것)
    if agent_module._pending:
        value = agent_module._pending["payload"]
        pending = {"type": agent_module._pending["type"], "order": value.get("order"), "entry": value.get("entry"),
                   "warnings": value.get("warnings", [])}
    return {
        "now_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "signals": {k: sig.get(k) for k in ("score", "items", "data_date", "expected_session", "freshness")},
        "order": order,
        "explanation": explanation(order, alloc),
        "tools_used": ["get_market_signals", "get_budget_status", "get_allocation_gap", "propose_order"],
        "budget": {**{k: ctx[k] for k in ("budget_month", "monthly_budget_usd", "effective_month_budget_usd",
                                          "spent_month_usd", "remaining_month_usd", "yearly_budget_usd",
                                          "spent_year_usd", "remaining_year_usd", "weeks_left",
                                          "bought_this_week", "trade_date", "is_month_last_day")},
                   **month_sessions_left(cfg["market"]["calendar"], ctx["trade_date"])},
        "allocation": {**with_gap_usd(alloc), "price_check": price_check(alloc, sig.get("expected_session"))},
        "month_buys": month_buys,
        "compare": {"months": [{k: m[k] for k in ("month", "cost_usd", "actual_value_usd", "first_day_value_usd",
                                                  "value_diff_usd", "value_diff_pct", "result", "evaluation_date")}
                               for m in compare.get("months", [])],
                    "summary": compare.get("summary"), "basis": compare.get("basis")},
        "backtest": backtest_summary(),
        "pending": pending,
        "settings": {"budget": cfg["budget"], "etfs": cfg["etfs"], "signals": cfg["signals"],
                     "multipliers": cfg["multipliers"], "fractional_us": cfg["order"]["fractional_us"],
                     "editable": user_settings.current(cfg)},
        "currency": "USD",
    }
