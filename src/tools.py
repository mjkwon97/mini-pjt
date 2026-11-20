"""에이전트가 쓰는 도메인 도구. 계산은 market.py·engine.py·ledger.py 가 하고, 여기서는 연결만 한다.

추천·조회 도구는 숫자 인자를 받지 않는다. LLM 이 금액·점수를 넣어 바꿀 수 없게 하기 위해서다.
record_buy 만 사용자가 알려준 체결 값을 받고, 그 값이 사용자 메시지에 실제로 있는지 파이썬으로 대조한다.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt

from src import engine, guardrails, ledger, market, retriever


@lru_cache(maxsize=4)
def _signals_cached(session: str) -> dict:
    """같은 거래일 데이터는 한 번만 받는다. (session 은 캐시 키)"""
    return market.get_market_signals(market.load_config())


def _signals() -> dict:
    """직전 거래일 기준 신호를 캐시에서 꺼낸다."""
    cfg = market.load_config()
    session = market.last_completed_session(cfg["market"]["calendar"]).isoformat()
    return _signals_cached(session)


@lru_cache(maxsize=4)
def _etf_prices_cached(session: str) -> tuple:
    """등록 ETF 의 마지막 정상 종가와 그 날짜를 받는다. 못 받은 ETF 는 빠진다."""
    cfg = market.load_config()
    prices, dates = {}, {}
    for t in cfg["etfs"]:
        try:
            df = market.fetch_daily(t, date.fromisoformat(session), days=10)
            prices[t], dates[t] = float(df["Close"].iloc[-1]), df.index[-1].date().isoformat()
        except Exception:
            pass
    return prices, dates


def _etf_prices(cfg: dict) -> tuple[dict[str, float], dict[str, str]]:
    """직전 거래일 기준 ETF 가격과 가격 기준일을 캐시에서 꺼낸다."""
    session = market.last_completed_session(cfg["market"]["calendar"]).isoformat()
    prices, dates = _etf_prices_cached(session)
    if set(dates) != set(cfg["etfs"]) or any(d != session for d in dates.values()):
        # 야후가 직전 거래일 종가를 아직 비워 둔 경우(NaN) 옛 가격이 캐시에 굳지 않도록, 다음 호출에서 다시 받는다
        _etf_prices_cached.cache_clear()
    return dict(prices), dict(dates)


@lru_cache(maxsize=2)
def _close_series_cached(session: str) -> dict:
    """등록 ETF 의 종가 시계열 (지난해 12월부터 session 까지). 1일 매수 비교에 쓴다."""
    cfg = market.load_config()
    until = date.fromisoformat(session)
    days = (until - date(until.year - 1, 12, 1)).days + 10
    out = {}
    for t in cfg["etfs"]:
        try:
            out[t] = market.fetch_daily(t, until, days=days)["Close"]
        except Exception:
            pass
    return out


def _month_shift(ym: str, delta: int) -> str:
    """YYYY-MM 문자열을 delta 개월 옮긴다."""
    y, m = map(int, ym.split("-"))
    m += delta
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def budget_context(cfg: dict) -> dict:
    """장부와 NYSE 달력으로 오늘의 예산 상황을 만든다. (세 도구가 같이 쓴다)

    budget_month 는 오늘 매수일이 속한 달이다. 연 예산이 부족하면 월 예산을 줄인 값(effective)을 쓴다.
    """
    week = market.trade_week_info(cfg["market"]["calendar"])
    trade_day = date.fromisoformat(week["trade_date"])
    bm = week["trade_date"][:7]
    conn = ledger.connect(cfg)
    try:
        spent_month = ledger.spent_in_month(conn, bm)
        spent_year = ledger.spent_in_year(conn, trade_day.year)
        months_left = 12 - trade_day.month + 1
        monthly = cfg["budget"]["monthly_usd"]
        effective = engine.effective_month_budget(
            monthly, cfg["budget"]["yearly_usd"], spent_year - spent_month, months_left)
        iso_week = trade_day.isocalendar()[:2]
        this_week_buys = [d for d in ledger.buy_dates(conn, bm)
                          if date.fromisoformat(d).isocalendar()[:2] == iso_week]
        prev = _month_shift(bm, -1)
        unspent_prev = None
        if prev.startswith(str(trade_day.year)):  # 같은 해의 지난달만 (연 예산 단위)
            unspent_prev = engine.remaining_month_budget(monthly, ledger.spent_in_month(conn, prev))
        holdings = ledger.holdings(conn)
    finally:
        conn.close()
    return {
        **week,
        "budget_month": bm,
        "monthly_budget_usd": monthly,
        "effective_month_budget_usd": effective,
        "spent_month_usd": spent_month,
        "remaining_month_usd": engine.remaining_month_budget(effective, spent_month),
        "yearly_budget_usd": cfg["budget"]["yearly_usd"],
        "spent_year_usd": spent_year,
        "remaining_year_usd": round(cfg["budget"]["yearly_usd"] - spent_year, 2),
        "months_left_incl_this": months_left,
        "bought_this_week": bool(this_week_buys),
        "this_week_buy_dates": this_week_buys,
        "prev_month": prev,
        "unspent_prev_month_usd": unspent_prev,
        "holdings": holdings,
        "currency": "USD",
    }


@tool
def get_market_signals() -> str:
    """어젯밤 미국 시장 신호를 조회한다.

    S&P 500 하루 등락률, 20일 고점 대비 위치, 하락일 거래량 배수, VIX 를 항목별 점수와 함께 돌려준다.
    데이터 기준일(data_date)과 신선도(freshness: OK/STALE/ERROR)도 포함한다.
    """
    sig = dict(_signals())
    if sig.get("freshness") == "OK" and sig.get("score") is not None:
        # 점수가 규칙상 어떤 날인지 (config 의 multipliers). 실제 매수 여부·금액은 propose_order 가 정한다.
        tier = engine.pick_multiplier(sig["score"], market.load_config())
        sig["rule_tier"] = {"min_score": tier["min_score"], "multiplier": tier["multiplier"],
                            "buy_today_if_not_bought_this_week": tier["buy_today"],
                            "note": "buy_today 가 false 면 주 마지막 거래일에만 매수"}
    return json.dumps(sig, ensure_ascii=False)


@tool
def get_budget_status() -> str:
    """이번 달·올해 예산 현황을 장부(budget_month 기준)에서 조회한다.

    이번 달 남은 예산(USD), 남은 거래 주 수, 이번 주 매수 여부, 연 예산 사용액·잔여,
    연 예산 부족 시 줄어든 이번 달 예산, 지난달 미집행 금액을 돌려준다.
    """
    ctx = budget_context(market.load_config())
    ctx.pop("holdings")
    return json.dumps(ctx, ensure_ascii=False)


@tool
def get_allocation_gap() -> str:
    """보유 수량 × 직전 거래일 종가로 ETF 별 현재 비중을 구해 목표 비중과 비교한다.

    ETF 별 보유 수량·가격·평가액·현재 비중·목표 비중·차이(%p)와 가장 모자란 ETF 를 돌려준다.
    """
    return json.dumps(allocation_view(market.load_config()), ensure_ascii=False)


def allocation_view(cfg: dict, ctx: dict | None = None) -> dict:
    """비중 비교 결과 (도구와 대시보드가 같이 쓴다). 계산은 engine.allocation_gap 이 한다."""
    ctx = ctx or budget_context(cfg)
    prices, price_dates = _etf_prices(cfg)
    gap = engine.allocation_gap(ctx["holdings"], prices, cfg)
    for row in gap["etfs"]:
        # 부호를 헷갈리지 않게 '현재 − 목표'와 초과/부족 상태를 같이 준다 (gap_pct 는 목표 − 현재)
        diff = round(row["current_pct"] - row["target_pct"], 2)
        row["current_minus_target_pct"] = diff
        row["status"] = "목표보다 많음(초과)" if diff > 0 else "목표보다 적음(부족)" if diff < 0 else "목표와 같음"
    gap.update({"price_dates": price_dates, "currency": "USD",
                "fields": {"current_minus_target_pct": "현재 비중 − 목표 비중 (%p). 양수면 초과, 음수면 부족",
                           "gap_pct": "목표 비중 − 현재 비중 (%p). 양수면 부족"}})
    return gap


def wait_notes(order: dict, ctx: dict) -> list[str]:
    """WAIT 일 때 관련 규칙의 예외·다음 단계를 코드로 적는다. (LLM 이 설명을 빠뜨리거나 지어내지 않게)"""
    nxt = _month_shift(ctx["budget_month"], 1)
    notes = []
    if order.get("remaining_usd", 0) <= 0:
        notes.append(f"이번 달({ctx['budget_month']}) 예산 소진: 추가 매수 금액은 0. 다음 달({nxt}) 예산으로 매수한다.")
        notes.append(f"연 예산을 넘는 추천은 하지 않는다 (올해 남은 연 예산 ${ctx['remaining_year_usd']:,.2f}). "
                     "다음 달 예산을 당겨 쓰지 않는다.")
    elif ctx.get("bought_this_week"):
        notes.append("주 1회 규칙: 이번 거래 주에는 이미 샀으므로 다음 거래 주에 다시 매수할 수 있다.")
        notes.append("예외: 월 마지막 거래일에는 주 1회 제한과 관계없이 남은 예산을 전액 매수한다.")
    elif order.get("freshness") != "OK":
        notes.append("시장 데이터가 직전 거래일 기준이 아니어서 신호 기반 매수 판단을 보류한다.")
        notes.append("예외: 월 마지막 거래일이면 데이터와 관계없이 남은 예산 전액을 금액(USD)으로 안내한다.")
    else:
        notes.append("신호 점수가 '오늘 매수' 구간이 아니면 이번 거래 주의 마지막 거래일에 기본 금액으로 산다.")
        notes.append("월 마지막 거래일에는 남은 예산을 전액 매수한다.")
    return notes


@tool
def propose_order() -> str:
    """오늘의 매수 결정(BUY/WAIT, 금액 USD, ETF, 수량)을 규칙 엔진으로 계산한다.

    장부의 이번 달 남은 예산, 이번 주 매수 여부, 월 마지막 거래일, 연 예산 한도, 보유 비중을 모두 반영한다.
    결과가 BUY 이면 사용자 승인을 받은 뒤 approval(approved/rejected)을 붙여 돌려준다.
    금액·ETF·수량은 이 도구 결과만 사용하고 바꾸지 않는다.
    """
    order = compute_order(market.load_config())
    if order["action"] == "BUY":
        # 사람 승인(HITL). 그래프가 여기서 멈추고, 재개 값({"approved": bool})을 받아 이어간다.
        decision = interrupt({"type": "buy_approval", "order": order})
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else False
        order["approval"] = "approved" if approved else "rejected"
        if not approved:
            order["reason"] += " → 사용자가 거절해 이번 추천은 취소"
    return json.dumps(order, ensure_ascii=False)


def compute_order(cfg: dict, ctx: dict | None = None, sig: dict | None = None) -> dict:
    """오늘의 매수 결정을 규칙 엔진으로 계산한다 (승인 단계 없음). propose_order 도구와 대시보드가 같이 쓴다."""
    sig = sig or _signals()
    ctx = ctx or budget_context(cfg)
    prices, price_dates = _etf_prices(cfg)
    current_values = {t: ctx["holdings"].get(t, 0.0) * p for t, p in prices.items()}
    order = engine.propose_order(
        sig, ctx["remaining_month_usd"], ctx["weeks_left"], ctx["is_week_last_day"], cfg,
        current_values=current_values,
        prices=prices if sig.get("freshness") == "OK" else None,
        bought_this_week=ctx["bought_this_week"],
        is_month_last_day=ctx["is_month_last_day"],
    )
    order.update({k: ctx[k] for k in ("trade_date", "budget_month", "weeks_left", "is_week_last_day",
                                      "is_month_last_day", "bought_this_week", "currency")})
    if order.get("etf") in price_dates:
        order["price_date"] = price_dates[order["etf"]]
    order["remaining_year_usd"] = ctx["remaining_year_usd"]
    if order["action"] == "WAIT":
        order["rule_notes"] = wait_notes(order, ctx)
    # 답과 승인 요청에 근거를 보이기 위한 신호 요약 (계산은 market.py 가 이미 했다)
    order.update({"data_date": sig.get("data_date"), "freshness": sig.get("freshness"),
                  "signal_score": sig.get("score"), "signal_items": sig.get("items", [])})
    return order


@tool
def retrieve_docs(query: str) -> str:
    """투자 원칙(investment_rules)·ETF 팩트시트(etf_factsheets)·적립식 연구 요약(dca_research) 문서를 검색한다.

    규칙의 이유, ETF 설명, 저점 매수·적립식 연구 근거를 묻는 질문에 쓴다. query 는 검색할 한국어 문장이다.
    관련 청크(doc_id, section, text)를 점수 순으로 돌려준다. 답은 이 text 에 있는 내용만 근거로 쓴다.
    """
    hits = retriever.search(query)
    return json.dumps([{k: h[k] for k in ("doc_id", "section", "text", "score")} for h in hits],
                      ensure_ascii=False)


def compare_month(cfg: dict, conn, month: str, series: dict, eval_date: date) -> dict:
    """한 달(budget_month)의 실제 매수를 그달 첫 거래일 종가 일괄 매수와 비교한다. 계산은 engine 이 한다."""
    first = market.first_session(cfg["market"]["calendar"], month)
    first_px, last_px, price_dates = {}, {}, {}
    for t, s in series.items():
        f, l = market.close_on(s, first), market.close_on(s, eval_date)
        if f and l:
            first_px[t], last_px[t] = f[0], l[0]
            price_dates[t] = {"first_day": f[1], "evaluation": l[1]}
    buys = ledger.buys_in_month(conn, month)
    result = engine.compare_first_day(buys, first_px, last_px)
    return {"month": month, "first_session": first.isoformat(), "buy_dates": sorted({b["trade_date"] for b in buys}),
            "evaluation_date": eval_date.isoformat(), "price_dates": price_dates, **result}


@tool
def compare_with_first_day(month: str | None = None) -> str:
    """내 매수 성적을 "그달 첫 거래일 종가에 같은 금액을 한 번에 산 경우"(1일 일괄 매수)와 비교한다.

    ETF 별 실제 평균 단가와 1일 매수 평균 단가, 차이(%), 같은 날 기준 평가액과 차이(USD)를 돌려준다.
    month: 비우면 지난달, 'year' 면 올해의 끝난 달 전부(이긴 달·진 달 수 포함).
    연도 없이 월만 말했으면 '8' 처럼 월 숫자만 넣는다 (가장 최근의 그 달로 정한다). 연도를 말했으면 'YYYY-MM' 또는 'YYYY'.
    """
    return json.dumps(compare_data(market.load_config(), month), ensure_ascii=False)


def compare_data(cfg: dict, month: str | None = None, current: str | None = None) -> dict:
    """1일 일괄 매수 비교 결과 (도구와 대시보드가 같이 쓴다). 계산은 engine.compare_first_day 가 한다."""
    current = current or budget_context(cfg)["budget_month"]
    month = (month or "").strip() or _month_shift(current, -1)
    if month.lower() in ("year", "this_year", "올해"):
        month = current[:4]  # 올해는 파이썬이 정한다 (LLM 이 연도를 추측하지 않게)
    elif month.isdigit() and 1 <= int(month) <= 12:
        y, m = int(current[:4]), int(month)  # 월만 말하면 이번 달 이전의 가장 최근 그 달
        month = f"{y if m < int(current[5:]) else y - 1:04d}-{m:02d}"
    if len(month) == 4 and month.isdigit():
        conn = ledger.connect(cfg)
        try:
            months = [m for m in ledger.monthly_totals(conn) if m.startswith(month) and m < current]
        finally:
            conn.close()
    elif len(month) == 7 and month[4] == "-":
        months = [month]
    else:
        return {"status": "invalid", "errors": [f"month 는 YYYY-MM 또는 YYYY 형식이어야 합니다: {month}"]}
    session = market.last_completed_session(cfg["market"]["calendar"])
    series = _close_series_cached(session.isoformat())
    conn = ledger.connect(cfg)
    try:
        results = [compare_month(cfg, conn, m, series, session) for m in months]
    finally:
        conn.close()
    results = [r for r in results if r["etfs"]]
    out = {"basis": "그달 첫 거래일 종가에 실제와 같은 금액을 일괄 매수했다고 가정, 평가액은 evaluation_date 종가 기준",
           "current_budget_month": current, "months": results, "currency": "USD",
           "fields": {"avg_cost_diff_pct": "실제 평균 단가가 1일 매수 단가보다 비싼 정도(%). 음수면 실제로 더 싸게 샀다는 뜻",
                      "value_diff_usd": "실제 평가액 − 1일 매수 평가액. 양수면 실제가 유리(WIN), 음수면 1일 매수가 유리(LOSE)"}}
    if any(m >= current for m in months):
        out["note"] = "이번 달은 아직 진행 중이라 결과가 바뀔 수 있다."
    if len(results) > 1:
        out["summary"] = {"months": len(results), "win": sum(r["result"] == "WIN" for r in results),
                          "lose": sum(r["result"] == "LOSE" for r in results),
                          "tie": sum(r["result"] == "TIE" for r in results),
                          "value_diff_usd": round(sum(r["value_diff_usd"] for r in results), 2)}
    if not results:
        out["status"] = "no_data"
    return out


def _last_user_text(runtime: ToolRuntime) -> str:
    """대화 상태에서 마지막 사용자 메시지 원문을 꺼낸다."""
    for m in reversed(runtime.state.get("messages", [])):
        if getattr(m, "type", "") == "human":
            c = m.content
            return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def _in_text(value: float, numbers: set[float]) -> bool:
    """value 가 사용자 메시지 속 숫자 중 하나와 같은지 (반올림 차이 0.01 허용)."""
    return any(abs(value - n) < 0.011 for n in numbers)


def check_fill(cfg: dict, user_text: str, etf: str, shares: float, total_cost_usd: float | None,
               price_usd: float | None, trade_date: str | None, budget_month: str | None,
               now_session: date) -> dict:
    """체결 기록 값을 규칙으로 검사하고 장부에 쓸 행(entry)을 만든다. (LLM 호출 없음)

    - 등록 ETF 인지, 수량·금액이 양수인지, 소수점 주문 설정에 맞는지
    - ETF·수량·금액이 사용자 메시지에 실제로 있는지 (LLM 이 숫자를 만들지 못하게)
    - 체결일이 거래일이고 미래가 아닌지. 비어 있으면 가장 최근 장이 열린 거래일
    결과: {"errors": [...], "entry": {...}}
    """
    errors = []
    etf = (etf or "").upper().strip()
    if etf not in cfg["etfs"]:
        errors.append(f"등록 ETF({', '.join(cfg['etfs'])})가 아닙니다: {etf}")
    if etf and etf not in user_text.upper():
        errors.append(f"사용자 메시지에 ETF {etf} 가 없습니다.")
    nums = guardrails.collect_numbers(user_text)
    if not shares or shares <= 0:
        errors.append("수량은 0보다 커야 합니다.")
    elif not _in_text(shares, nums):
        errors.append(f"사용자 메시지에 수량 {shares} 가 없습니다.")
    elif not cfg["order"]["fractional_us"] and float(shares) != int(shares):
        errors.append("소수점 주문 설정(fractional_us: false)이라 수량은 정수여야 합니다.")
    if total_cost_usd is None and price_usd is None:
        errors.append("총 체결 금액(USD) 또는 1주 체결가(USD)가 필요합니다.")
    for name, v in (("총 체결 금액", total_cost_usd), ("1주 체결가", price_usd)):
        if v is not None and (v <= 0 or not _in_text(v, nums)):
            errors.append(f"사용자 메시지에 {name} {v} 가 없거나 0 이하입니다.")
    day = None
    try:
        day = date.fromisoformat(trade_date) if trade_date else now_session
    except ValueError:
        errors.append(f"체결일 형식이 YYYY-MM-DD 가 아닙니다: {trade_date}")
    if day:
        if day > now_session:
            errors.append(f"체결일 {day} 이 아직 오지 않은 날입니다.")
        elif not market.is_session(cfg["market"]["calendar"], day):
            errors.append(f"체결일 {day} 은 미국 거래일이 아닙니다.")
    bm = budget_month or (day.isoformat()[:7] if day else None)
    if budget_month and not (len(budget_month) == 7 and budget_month[4] == "-"):
        errors.append(f"예산 귀속 월 형식이 YYYY-MM 이 아닙니다: {budget_month}")
    if errors or not shares:
        return {"errors": errors, "entry": None}
    cost = round(total_cost_usd if total_cost_usd is not None else shares * price_usd, 2)
    price = round(price_usd if price_usd is not None else cost / shares, 4)
    return {"errors": [], "entry": {"trade_date": day.isoformat(), "budget_month": bm, "etf": etf,
                                    "shares": float(shares), "price_usd": price, "cost_usd": cost}}


def fill_warnings(cfg: dict, conn, entry: dict, last_price: float | None) -> list[str]:
    """기록은 막지 않지만 승인 전에 보여 줄 주의 사항 (예산 초과, 같은 주 매수, 시세와 큰 차이)."""
    warn = []
    monthly, yearly = cfg["budget"]["monthly_usd"], cfg["budget"]["yearly_usd"]
    spent_m = ledger.spent_in_month(conn, entry["budget_month"])
    if spent_m + entry["cost_usd"] > monthly + 0.005:
        warn.append(f"{entry['budget_month']} 월 예산 ${monthly:,.2f} 를 ${spent_m + entry['cost_usd'] - monthly:,.2f} 넘습니다.")
    spent_y = ledger.spent_in_year(conn, int(entry["budget_month"][:4]))
    if spent_y + entry["cost_usd"] > yearly + 0.005:
        warn.append(f"연 예산 ${yearly:,.2f} 를 ${spent_y + entry['cost_usd'] - yearly:,.2f} 넘습니다.")
    week = date.fromisoformat(entry["trade_date"]).isocalendar()[:2]
    if any(date.fromisoformat(d).isocalendar()[:2] == week for d in ledger.buy_dates(conn, entry["budget_month"])):
        warn.append("같은 거래 주에 이미 매수 기록이 있습니다 (주 1회 규칙).")
    if last_price and abs(entry["price_usd"] / last_price - 1) > 0.1:
        warn.append(f"1주 체결가 ${entry['price_usd']:,.2f} 가 최근 종가 ${last_price:,.2f} 와 10% 넘게 다릅니다.")
    return warn


@tool
def record_buy(etf: str, shares: float, runtime: ToolRuntime, total_cost_usd: float | None = None,
               price_usd: float | None = None, trade_date: str | None = None,
               budget_month: str | None = None) -> str:
    """실제 체결(사용자가 증권사 앱에서 산 내역)을 매수 장부에 기록한다. 사용자 승인 뒤에만 기록된다.

    사용자가 체결 내역을 알려주며 기록을 요청할 때만 쓴다. 숫자는 사용자 메시지에 있는 값만 그대로 넣고 계산하지 않는다.
    etf: 등록 ETF 코드. shares: 체결 수량.
    total_cost_usd(총 체결 금액, USD) 또는 price_usd(1주 체결가, USD) 중 사용자가 말한 것 하나만 넣는다.
    trade_date(체결일 YYYY-MM-DD)·budget_month(예산 귀속 월 YYYY-MM)는 사용자가 말했을 때만 넣고, 아니면 비운다.
    status 가 invalid 면 errors 를 사용자에게 알리고 빠진 값을 물어본다.
    """
    cfg = market.load_config()
    now_session = market.latest_started_session(cfg["market"]["calendar"])
    checked = check_fill(cfg, _last_user_text(runtime), etf, shares, total_cost_usd, price_usd,
                         trade_date, budget_month, now_session)
    if checked["errors"]:
        return json.dumps({"status": "invalid", "errors": checked["errors"]}, ensure_ascii=False)
    entry = checked["entry"]
    prices, _ = _etf_prices(cfg)
    conn = ledger.connect(cfg)
    try:
        warnings = fill_warnings(cfg, conn, entry, prices.get(entry["etf"]))
        rows_before = ledger.count_buys(conn)
    finally:
        conn.close()

    # 사람 승인(HITL). 승인 전에는 장부를 건드리지 않는다. 재개되면 이 함수가 처음부터 다시 실행된다.
    decision = interrupt({"type": "record_approval", "entry": entry, "warnings": warnings})
    approved = bool(decision.get("approved")) if isinstance(decision, dict) else False
    result = {"entry": entry, "warnings": warnings, "rows_before": rows_before, "currency": "USD"}
    if not approved:
        return json.dumps({**result, "status": "rejected", "approval": "rejected"}, ensure_ascii=False)
    conn = ledger.connect(cfg)
    try:
        row_id = ledger.record_buy(conn, note="record_buy (사용자 승인)", **entry)
        spent = ledger.spent_in_month(conn, entry["budget_month"])
        result.update({"status": "recorded", "approval": "approved", "row_id": row_id,
                       "rows_after": ledger.count_buys(conn), "month_spent_usd": spent,
                       "month_remaining_usd": engine.remaining_month_budget(cfg["budget"]["monthly_usd"], spent)})
    finally:
        conn.close()
    return json.dumps(result, ensure_ascii=False)


TOOLS = [get_market_signals, get_budget_status, get_allocation_gap, propose_order, retrieve_docs, record_buy,
         compare_with_first_day]
