"""engine.propose_order 규칙 테스트. 숫자는 config.yaml 에서 읽는다."""

from pathlib import Path

import pytest
import yaml

from src import engine
from src.engine import effective_month_budget, propose_order

CFG = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config.yaml", encoding="utf-8"))
REMAINING = CFG["budget"]["monthly_usd"]
WEEKS = 4
BASE = REMAINING / WEEKS


def tier_mult(min_score):
    """config 에서 min_score 구간의 배수를 찾는다."""
    return next(t["multiplier"] for t in CFG["multipliers"] if t["min_score"] == min_score)


def sig(score, freshness="OK"):
    """테스트용 신호 dict 를 만든다."""
    return {"score": score, "freshness": freshness}


def test_score0_midweek_waits():
    """점수 0, 주 중간이면 WAIT."""
    o = propose_order(sig(0), REMAINING, WEEKS, is_week_last_day=False, cfg=CFG)
    assert o["action"] == "WAIT"


def test_score0_week_last_day_buys_base():
    """점수 0 이라도 주 마지막 거래일이면 기본 금액(배수 1.0)만큼 BUY."""
    o = propose_order(sig(0), REMAINING, WEEKS, is_week_last_day=True, cfg=CFG)
    assert o["action"] == "BUY"
    assert o["multiplier"] == 1.0
    assert o["amount_usd"] == pytest.approx(BASE)


def test_score2_buys_today_with_mid_multiplier():
    """점수 2 면 주 중간이라도 기본 금액 × 1.5 BUY."""
    o = propose_order(sig(2), REMAINING, WEEKS, is_week_last_day=False, cfg=CFG)
    assert o["action"] == "BUY"
    assert o["multiplier"] == tier_mult(2)
    assert o["amount_usd"] == pytest.approx(BASE * tier_mult(2))


def test_score4_buys_today_with_max_multiplier():
    """점수 4 면 기본 금액 × 2.0 BUY."""
    o = propose_order(sig(4), REMAINING, WEEKS, is_week_last_day=False, cfg=CFG)
    assert o["action"] == "BUY"
    assert o["multiplier"] == tier_mult(4)
    assert o["amount_usd"] == pytest.approx(BASE * tier_mult(4))


def test_amount_capped_at_remaining():
    """배수를 곱한 금액이 남은 예산보다 크면 남은 예산까지만 산다."""
    o = propose_order(sig(4), 500, weeks_left=1, is_week_last_day=False, cfg=CFG)
    assert o["amount_usd"] == pytest.approx(500)


@pytest.mark.parametrize("freshness", ["STALE", "ERROR"])
def test_stale_or_error_data_waits(freshness):
    """데이터가 최신이 아니면 점수가 높아도 신호 기반 추천을 하지 않는다."""
    o = propose_order(sig(5, freshness), REMAINING, WEEKS, is_week_last_day=True, cfg=CFG)
    assert o["action"] == "WAIT"
    assert "amount_usd" not in o


def test_budget_exhausted_waits():
    """남은 예산이 0 이면 WAIT."""
    o = propose_order(sig(4), 0, WEEKS, is_week_last_day=True, cfg=CFG)
    assert o["action"] == "WAIT"


def test_etf_is_most_underweight():
    """보유가 없으면 목표 비중이 가장 큰 ETF, VOO 가 넘치면 다른 ETF 를 고른다."""
    o = propose_order(sig(2), REMAINING, WEEKS, False, CFG)
    assert o["etf"] == max(CFG["etfs"], key=CFG["etfs"].get)
    o = propose_order(sig(2), REMAINING, WEEKS, False, CFG, current_values={"VOO": 10000})
    assert o["etf"] != "VOO"


@pytest.mark.parametrize("bad", [float("nan"), 0.0, -1.0])
def test_invalid_price_skips_shares(bad):
    """가격이 NaN·0·음수면 수량을 계산하지 않고 금액만 돌려준다."""
    o = propose_order(sig(2), REMAINING, WEEKS, False, CFG, prices={"VOO": bad})
    assert o["action"] == "BUY"
    assert "shares" not in o


def test_whole_shares_rounded_down():
    """소수점 주문이 꺼져 있으면 정수 주로 내리고, 예상 비용은 금액 이하."""
    o = propose_order(sig(2), REMAINING, WEEKS, False, CFG, prices={"VOO": 500.0, "QQQ": 500.0, "SCHD": 25.0})
    assert isinstance(o["shares"], int)
    assert o["est_cost_usd"] <= o["amount_usd"]


# ── T9: 주 1회 제한, 월말 전액 매수, 연 예산 축소 ─────────────────────────

def test_already_bought_this_week_and_vix35_waits():
    """이번 주에 이미 샀으면 VIX 35 처럼 신호가 강해도(점수 4 이상) WAIT."""
    o = propose_order(sig(5), REMAINING, WEEKS, is_week_last_day=False, cfg=CFG, bought_this_week=True)
    assert o["action"] == "WAIT"
    assert "amount_usd" not in o


def test_month_last_day_buys_all_remaining_even_if_bought_this_week():
    """월 마지막 거래일은 신호가 약하고 이번 주에 이미 샀어도 남은 예산 전액 BUY."""
    o = propose_order(sig(0), 831.10, 1, is_week_last_day=True, cfg=CFG,
                      bought_this_week=True, is_month_last_day=True)
    assert o["action"] == "BUY"
    assert o["amount_usd"] == pytest.approx(831.10)


@pytest.mark.parametrize("freshness", ["STALE", "ERROR"])
def test_month_last_day_with_bad_data_gives_usd_only(freshness):
    """월말인데 데이터가 최신이 아니면 금액(USD)만 안내하고 수량은 계산하지 않는다."""
    o = propose_order(sig(0, freshness), 831.10, 1, True, CFG, prices={"VOO": 600.0},
                      is_month_last_day=True)
    assert o["action"] == "BUY"
    assert o["amount_usd"] == pytest.approx(831.10)
    assert o["price_stale"] is True
    assert "shares" not in o


def test_month_last_day_whole_shares_and_leftover():
    """월말 전액 매수도 정수 주로 내리고, 남는 금액(leftover)을 알려준다."""
    o = propose_order(sig(0), 831.10, 1, True, CFG, prices={"VOO": 600.0}, is_month_last_day=True)
    assert o["shares"] == 1
    assert o["leftover_usd"] == pytest.approx(231.10)


def test_month_last_day_less_than_one_share_carries_over():
    """월말 잔액이 1주 가격보다 작으면 사지 않고 다음 달로 이월 표시."""
    o = propose_order(sig(0), 50.0, 1, True, CFG, prices={"VOO": 600.0}, is_month_last_day=True)
    assert o["action"] == "WAIT"
    assert o["carry_over_usd"] == pytest.approx(50.0)


def test_budget_exhausted_on_month_last_day_waits():
    """예산을 다 썼으면 월말이어도 추가 매수 없음 (다음 달 예산 당겨쓰기 금지)."""
    o = propose_order(sig(3), 0, 1, True, CFG, is_month_last_day=True)
    assert o["action"] == "WAIT"


def test_yearly_budget_enough_keeps_monthly():
    """남은 연 예산이 충분하면 월 예산 그대로."""
    m, y = CFG["budget"]["monthly_usd"], CFG["budget"]["yearly_usd"]
    assert effective_month_budget(m, y, spent_year_before_month_usd=m * 10, months_left=2) == pytest.approx(m)


def test_yearly_budget_short_scales_month_down():
    """남은 연 예산이 월 예산 × 남은 개월보다 적으면 월 예산을 나눠 줄인다."""
    m, y = CFG["budget"]["monthly_usd"], CFG["budget"]["yearly_usd"]
    spent = y - 2000  # 남은 연 예산 $2,000, 남은 2개월
    assert effective_month_budget(m, y, spent, months_left=2) == pytest.approx(1000)


def test_yearly_budget_exhausted_gives_zero():
    """연 예산을 다 썼으면 이번 달 예산 0 → propose_order 는 WAIT."""
    m, y = CFG["budget"]["monthly_usd"], CFG["budget"]["yearly_usd"]
    eff = effective_month_budget(m, y, y, months_left=1)
    assert eff == 0
    assert propose_order(sig(4), eff, 1, True, CFG, is_month_last_day=True)["action"] == "WAIT"


# ── T10: 보유 비중 차이 ─────────────────────────

def test_allocation_gap_finds_most_underweight():
    """현재 비중과 목표 비중의 차이(%p)를 구하고 가장 모자란 ETF 를 고른다."""
    from src.engine import allocation_gap
    holdings = {"VOO": 6, "QQQ": 1, "SCHD": 40}       # 600·100·40 → 평가액 600, 100, 300
    prices = {"VOO": 100.0, "QQQ": 100.0, "SCHD": 7.5}
    g = allocation_gap(holdings, prices, CFG)
    by = {r["etf"]: r for r in g["etfs"]}
    assert g["total_value_usd"] == pytest.approx(1000)
    assert by["VOO"]["current_pct"] == pytest.approx(60)
    assert by["QQQ"]["gap_pct"] == pytest.approx(CFG["etfs"]["QQQ"] * 100 - 10)
    assert g["most_underweight"] == "QQQ"


# ── 최소 1주 매수 (정수 주에서 금액 < 1주 가격) ─────────────────────────

def test_min_one_share_when_price_within_remaining_budget():
    """금액($415.55)이 1주($656.95)보다 적어도 남은 월 예산($831.10) 안이면 1주를 산다."""
    o = propose_order(sig(2), 831.10, 3, False, CFG, prices={"VOO": 656.95, "QQQ": 656.95, "SCHD": 656.95})
    assert o["action"] == "BUY"
    assert o["shares"] == 1
    assert o["min_one_share"] is True
    assert o["est_cost_usd"] <= 831.10          # 월 예산은 넘지 않는다
    assert o["over_target_usd"] == pytest.approx(656.95 - 415.55, abs=0.01)
    assert o["remaining_after_usd"] == pytest.approx(831.10 - 656.95, abs=0.01)
    assert "leftover_usd" not in o


def test_no_min_one_share_when_price_exceeds_remaining_budget():
    """1주 가격이 남은 월 예산보다 비싸면 사지 않고 WAIT."""
    o = propose_order(sig(2), 600.0, 3, False, CFG, prices={"VOO": 656.95, "QQQ": 656.95, "SCHD": 656.95})
    assert o["action"] == "WAIT"
    assert o["shares"] == 0


# ── H2: 1일 일괄 매수 비교 ─────────────────────────────────────────────

def test_compare_first_day_win_when_bought_cheaper():
    """첫 거래일보다 싸게 샀으면 평균 단가가 낮고 평가액이 높아 WIN."""
    r = engine.compare_first_day([{"etf": "VOO", "shares": 2, "cost_usd": 1000.0}],
                                 first_prices={"VOO": 550.0}, last_prices={"VOO": 600.0})
    row = r["etfs"][0]
    assert row["actual_avg_cost_usd"] == 500.0 and row["first_day_avg_cost_usd"] == 550.0
    assert row["avg_cost_diff_pct"] == -9.09
    assert r["actual_value_usd"] == 1200.0 and r["first_day_value_usd"] == round(1000 / 550 * 600, 2)
    assert r["result"] == "WIN" and r["value_diff_usd"] > 0


def test_compare_first_day_lose_and_same_money():
    """비싸게 샀으면 LOSE. 1일 매수 쪽도 실제와 같은 금액을 쓴다."""
    r = engine.compare_first_day([{"etf": "QQQ", "shares": 1, "cost_usd": 700.0}],
                                 first_prices={"QQQ": 650.0}, last_prices={"QQQ": 650.0})
    assert r["result"] == "LOSE"
    assert r["cost_usd"] == 700.0
    assert r["etfs"][0]["first_day_shares"] == round(700 / 650, 4)


def test_compare_first_day_sums_multiple_buys_and_etfs():
    """같은 ETF 여러 번 매수는 합쳐서 평균 단가를 내고, ETF 별 결과를 합산한다."""
    buys = [{"etf": "VOO", "shares": 1, "cost_usd": 600.0}, {"etf": "VOO", "shares": 1, "cost_usd": 640.0},
            {"etf": "SCHD", "shares": 10, "cost_usd": 300.0}]
    r = engine.compare_first_day(buys, {"VOO": 620.0, "SCHD": 30.0}, {"VOO": 620.0, "SCHD": 30.0})
    voo = next(e for e in r["etfs"] if e["etf"] == "VOO")
    assert voo["actual_shares"] == 2 and voo["actual_avg_cost_usd"] == 620.0
    assert r["cost_usd"] == 1540.0 and r["result"] == "TIE"


def test_compare_first_day_skips_missing_price():
    """가격이 없는 ETF 는 비교에서 빠진다."""
    r = engine.compare_first_day([{"etf": "VOO", "shares": 1, "cost_usd": 600.0}], {}, {"VOO": 600.0})
    assert r["etfs"] == [] and r["cost_usd"] == 0.0
