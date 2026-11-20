"""예산·주문 규칙 엔진. 순수 함수만 둔다 (네트워크·LLM·파일 접근 없음).

매수 금액·ETF·수량은 이 모듈의 propose_order 만 정한다.
규칙 숫자는 호출하는 쪽이 넘겨주는 cfg(config.yaml 내용)에서 읽는다.
"""

from __future__ import annotations

import math


def effective_month_budget(monthly_usd: float, yearly_usd: float,
                           spent_year_before_month_usd: float, months_left: int) -> float:
    """연 예산을 넘지 않도록 줄인 이번 달 예산(USD).

    남은 연 예산 = 연 예산 − 이번 달 이전 budget_month 들로 쓴 금액.
    월 예산 × 남은 개월 수(이번 달 포함)가 남은 연 예산보다 크면, 이번 달 예산 = 남은 연 예산 ÷ 남은 개월 수.
    """
    if months_left <= 0:
        raise ValueError("months_left 는 1 이상이어야 한다")
    year_room = max(0.0, yearly_usd - spent_year_before_month_usd)
    if monthly_usd * months_left > year_room:
        return round(year_room / months_left, 2)
    return round(float(monthly_usd), 2)


def remaining_month_budget(monthly_usd: float, spent_month_usd: float) -> float:
    """이번 달 남은 예산(USD) = 월 예산 − 이번 budget_month 로 쓴 금액. 0 아래로 내려가지 않는다."""
    return round(max(0.0, monthly_usd - spent_month_usd), 2)


def pick_multiplier(score: int, cfg: dict) -> dict:
    """신호 점수에 맞는 배수 구간을 config 의 multipliers 에서 고른다.

    min_score 가 큰 구간부터 보고, 점수가 그 이상이면 그 구간을 돌려준다.
    """
    for tier in sorted(cfg["multipliers"], key=lambda t: t["min_score"], reverse=True):
        if score >= tier["min_score"]:
            return tier
    raise ValueError(f"점수 {score} 에 맞는 배수 구간이 없다")


def compare_first_day(buys: list[dict], first_prices: dict[str, float], last_prices: dict[str, float]) -> dict:
    """한 달의 실제 매수와 "그달 첫 거래일 종가에 같은 금액을 한 번에 매수"를 비교한다. (순수 함수)

    ETF 마다 실제로 쓴 금액(cost_usd)을 첫 거래일 종가로 샀다고 가정한다 (비교용이라 소수 주 허용).
    평균 단가가 낮을수록, 같은 날(last_prices) 평가액이 높을수록 유리하다.
    buys: [{"etf", "shares", "cost_usd"}], first_prices/last_prices: {etf: 가격}
    """
    per = {}
    for b in buys:
        p = per.setdefault(b["etf"], {"shares": 0.0, "cost_usd": 0.0})
        p["shares"] += b["shares"]
        p["cost_usd"] += b["cost_usd"]
    rows, tot_cost, tot_actual, tot_bench = [], 0.0, 0.0, 0.0
    for etf, p in sorted(per.items()):
        first, last = first_prices.get(etf), last_prices.get(etf)
        if not first or not last or p["shares"] <= 0:
            continue
        actual_avg = p["cost_usd"] / p["shares"]
        bench_shares = p["cost_usd"] / first
        v_actual, v_bench = p["shares"] * last, bench_shares * last
        rows.append({"etf": etf, "cost_usd": round(p["cost_usd"], 2),
                     "actual_shares": round(p["shares"], 4), "actual_avg_cost_usd": round(actual_avg, 2),
                     "first_day_shares": round(bench_shares, 4), "first_day_avg_cost_usd": round(first, 2),
                     "avg_cost_diff_pct": round((actual_avg / first - 1) * 100, 2),
                     "actual_value_usd": round(v_actual, 2), "first_day_value_usd": round(v_bench, 2),
                     "value_diff_usd": round(v_actual - v_bench, 2)})
        tot_cost += p["cost_usd"]
        tot_actual += v_actual
        tot_bench += v_bench
    diff = round(tot_actual - tot_bench, 2)
    return {"etfs": rows, "cost_usd": round(tot_cost, 2), "actual_value_usd": round(tot_actual, 2),
            "first_day_value_usd": round(tot_bench, 2), "value_diff_usd": diff,
            "value_diff_pct": round((tot_actual / tot_bench - 1) * 100, 2) if tot_bench else None,
            "result": "WIN" if diff > 0 else "LOSE" if diff < 0 else "TIE"}


def allocation_gap(holdings: dict[str, float], prices: dict[str, float], cfg: dict) -> dict:
    """보유 수량 × 가격으로 ETF 별 현재 비중을 구해 목표 비중과 비교한다.

    gap_pct = 목표 비중 − 현재 비중 (%p). 양수면 모자란 것. 가격이 없는 ETF 는 평가액 0 으로 본다.
    """
    targets = cfg["etfs"]
    values = {t: holdings.get(t, 0.0) * prices.get(t, 0.0) for t in targets}
    total = sum(values.values())
    rows = []
    for t, w in targets.items():
        cur = values[t] / total if total else 0.0
        rows.append({"etf": t, "shares": holdings.get(t, 0.0),
                     "price": round(prices[t], 2) if t in prices else None,
                     "value_usd": round(values[t], 2), "current_pct": round(cur * 100, 2),
                     "target_pct": round(w * 100, 2), "gap_pct": round((w - cur) * 100, 2)})
    most = max(rows, key=lambda r: r["gap_pct"])["etf"]
    return {"total_value_usd": round(total, 2), "etfs": rows, "most_underweight": most}


def pick_etf(amount_usd: float, current_values: dict[str, float], cfg: dict) -> str:
    """이번 매수 후 목표 비중 대비 가장 많이 모자란 ETF 를 고른다.

    current_values 는 ETF 별 현재 평가액(USD). 없는 ETF 는 0 으로 본다.
    """
    targets = cfg["etfs"]
    total_after = sum(current_values.get(t, 0.0) for t in targets) + amount_usd
    gaps = {t: w * total_after - current_values.get(t, 0.0) for t, w in targets.items()}
    return max(gaps, key=gaps.get)


def propose_order(
    signals: dict,
    remaining_usd: float,
    weeks_left: int,
    is_week_last_day: bool,
    cfg: dict,
    current_values: dict[str, float] | None = None,
    prices: dict[str, float] | None = None,
    *,
    bought_this_week: bool = False,
    is_month_last_day: bool = False,
) -> dict:
    """오늘 BUY/WAIT, 배수, 금액(USD), ETF, 수량을 정한다.

    판정 순서:
    1. 남은 예산이 0 이면 WAIT.
    2. 월 마지막 거래일이면 신호·주 1회 제한과 관계없이 남은 예산 전액 BUY.
       데이터가 최신이 아니면 금액(USD)만 안내하고 수량은 계산하지 않는다(가격 기준일이 최신이 아님).
    3. 시장 데이터가 OK 가 아니면(STALE·ERROR) 신호 기반 추천을 하지 않고 WAIT.
    4. 이번 거래 주에 이미 매수했으면 WAIT (주 1회).
    5. 기본 금액 = 이번 달 남은 예산 ÷ 남은 거래 주 수.
       점수가 오늘 매수 구간(buy_today)이면 기본 금액 × 배수, 아니면 주 마지막 거래일에만 기본 금액.
    금액은 남은 예산을 넘지 않는다. prices 가 있으면 수량을 계산한다(fractional_us 가 false 면 정수 주로 내림).
    """
    score = int(signals.get("score", 0))
    freshness = signals.get("freshness", "ERROR")
    base = {"score": score, "freshness": freshness, "remaining_usd": round(remaining_usd, 2)}

    if remaining_usd <= 0:
        return {**base, "action": "WAIT", "reason": "이번 달 예산 소진"}

    if is_month_last_day:
        etf = pick_etf(remaining_usd, current_values or {}, cfg)
        order = {**base, "action": "BUY", "multiplier": None, "amount_usd": round(remaining_usd, 2),
                 "etf": etf, "reason": "월 마지막 거래일 → 남은 예산 전액 매수"}
        if freshness != "OK":
            order.update({"price_stale": True,
                          "reason": order["reason"] + f" (시장 데이터 {freshness}: 금액만 안내, 수량은 증권사 앱 현재가로 계산)"})
            return order
        order = _with_shares(order, etf, remaining_usd, prices, cfg)
        if order["action"] == "WAIT":
            order.update({"carry_over_usd": round(remaining_usd, 2),
                          "reason": order["reason"] + " → 잔액은 다음 달 첫 매수에 이월(원래 달 예산으로 계산)"})
        return order

    if freshness != "OK":
        return {**base, "action": "WAIT", "reason": f"시장 데이터가 최신이 아님({freshness}). 신호 기반 추천 보류"}
    if bought_this_week:
        return {**base, "action": "WAIT",
                "reason": f"이번 거래 주에 이미 매수함(주 1회). 신호 점수 {score} 와 관계없이 다음 주 또는 월 마지막 거래일까지 대기"}
    if weeks_left <= 0:
        raise ValueError("weeks_left 는 1 이상이어야 한다")

    base_usd = remaining_usd / weeks_left
    tier = pick_multiplier(score, cfg)
    base["base_usd"] = round(base_usd, 2)

    if tier["buy_today"]:
        multiplier = tier["multiplier"]
        reason = f"신호 점수 {score} → 배수 {multiplier}, 오늘 매수"
    elif is_week_last_day:
        multiplier = 1.0
        reason = f"신호 점수 {score} 로 약하지만 이번 주 마지막 거래일 → 기본 금액 매수"
    else:
        return {**base, "action": "WAIT", "multiplier": tier["multiplier"],
                "reason": f"신호 점수 {score} 로 약함. 이번 주 마지막 거래일까지 대기"}

    amount = min(base_usd * multiplier, remaining_usd)
    if amount < base_usd * multiplier:
        reason += " (남은 예산까지만)"
    etf = pick_etf(amount, current_values or {}, cfg)
    order = {**base, "action": "BUY", "multiplier": multiplier, "amount_usd": round(amount, 2),
             "etf": etf, "reason": reason}
    return _with_shares(order, etf, amount, prices, cfg, budget_cap=remaining_usd)


def _with_shares(order: dict, etf: str, amount: float, prices: dict | None, cfg: dict,
                 budget_cap: float | None = None) -> dict:
    """가격이 유효하면 수량·예상 비용·남는 금액을 주문에 붙인다.

    정수 주 주문에서 금액이 1주 가격보다 적으면:
    - 1주 가격이 남은 월 예산(budget_cap) 이하이면 최소 1주를 산다 (min_one_share).
    - 남은 월 예산으로도 1주를 못 사면 WAIT.
    """
    price = (prices or {}).get(etf)
    if price is None or not math.isfinite(price) or price <= 0:
        return order
    if cfg["order"]["fractional_us"]:
        shares = round(amount / price, 6)
    else:
        shares = math.floor(amount / price)
        if shares == 0 and budget_cap is not None and price <= budget_cap:
            shares = 1
            order["min_one_share"] = True
            order["reason"] += f" (금액 ${amount:.2f} 가 1주 가격보다 적어 남은 월 예산 안에서 최소 1주 매수)"
    cost = round(shares * price, 2)
    order.update({"price": round(price, 2), "shares": shares, "est_cost_usd": cost})
    if cost > amount:  # 최소 1주로 목표 금액보다 더 쓴 경우
        order.update({"over_target_usd": round(cost - amount, 2),
                      "remaining_after_usd": round(budget_cap - cost, 2)})
    else:
        order["leftover_usd"] = round(amount - cost, 2)
    if shares == 0:
        order.update({"action": "WAIT", "reason": f"금액 ${amount:.2f} 가 {etf} 1주 가격 ${price:.2f} 보다 적음"})
    return order
