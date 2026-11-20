"""2015~2025 백테스트: 규칙 엔진(propose_order)대로 산 경우 vs 매월 "1일 일괄 매수".

사용법: python -m evaluation.backtest [--start 2015-01 --end 2025-12]
결과: evaluation/backtest_report.md, evaluation/backtest_months.csv

가정 (리포트에도 적는다):
- 거래일마다 직전 거래일까지의 S&P 500·VIX 로 신호 점수를 계산한다 (market.compute_signals, 실서비스와 같은 함수).
- 매수 여부·금액·ETF·수량은 engine.propose_order 가 정한다. 장부 상태(월·연 예산, 이번 주 매수, 보유 비중)를 그대로 이어 간다.
- 체결은 그날 종가, 정수 주(config 의 fractional_us). 배당·수수료·세금·환율은 넣지 않는다.
- 비교: 그달 실제로 쓴 금액을 ETF 별로 그달 첫 거래일 종가에 한 번에 샀다고 가정 (engine.compare_first_day).
  평가액은 백테스트 마지막 거래일 종가 기준.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal

from src import engine, market

ROOT = Path(__file__).resolve().parent


def load_prices(cfg: dict, start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.Series]]:
    """지수·VIX 일봉과 ETF 종가를 받는다. 신호 계산용으로 시작일보다 60일 앞부터 받는다."""
    days = (date.today() - start).days + 60
    idx = market.fetch_daily(cfg["market"]["index_ticker"], end, days=days)
    vix = market.fetch_daily(cfg["market"]["vix_ticker"], end, days=days)
    etf = {t: market.fetch_daily(t, end, days=days)["Close"] for t in cfg["etfs"]}
    return idx, vix, etf


def week_flags(sessions: list[date]) -> dict[date, dict]:
    """거래일마다 그달 남은 거래 주 수, 주 마지막 거래일, 월 마지막 거래일 여부. (ISO 주 중 그달 부분)"""
    by_month = defaultdict(list)
    for d in sessions:
        by_month[(d.year, d.month)].append(d)
    flags = {}
    for days in by_month.values():
        for i, d in enumerate(days):
            rest = days[i:]
            wk = d.isocalendar()[:2]
            same_week = [x for x in days if x.isocalendar()[:2] == wk]
            flags[d] = {"weeks_left": len({x.isocalendar()[:2] for x in rest}),
                        "is_week_last_day": d == same_week[-1], "is_month_last_day": d == days[-1]}
    return flags


def simulate(cfg: dict, sessions: list[date], idx: pd.DataFrame, vix: pd.DataFrame, etf: dict) -> dict:
    """거래일을 하루씩 진행하며 규칙 엔진대로 매수한다. 매수 기록과 통계를 돌려준다."""
    monthly, yearly = cfg["budget"]["monthly_usd"], cfg["budget"]["yearly_usd"]
    n = cfg["market"]["lookback_days"]
    flags = week_flags(sessions)
    holdings: dict[str, float] = defaultdict(float)
    spent_month: dict[str, float] = defaultdict(float)
    spent_year: dict[int, float] = defaultdict(float)
    week_bought: set[tuple] = set()
    buys, kinds = [], Counter()
    idx_dates = idx.index

    for d in sessions:
        ts = pd.Timestamp(d)
        prev_i = idx_dates.searchsorted(ts) - 1  # 직전 거래일 (한국 아침에 보는 '어젯밤')
        if prev_i < n + 1:
            continue
        prev = idx_dates[prev_i]
        sig = market.compute_signals(idx.iloc[prev_i - n - 2: prev_i + 1], vix[vix.index <= prev], cfg)
        sig["freshness"] = "OK"
        prices = {}
        for t, s in etf.items():
            c = market.close_on(s, d)
            if c and c[1] == d.isoformat():
                prices[t] = c[0]
        if len(prices) < len(etf):
            continue  # 그날 ETF 종가가 없으면 건너뛴다
        bm = f"{d.year:04d}-{d.month:02d}"
        months_left = 12 - d.month + 1
        effective = engine.effective_month_budget(monthly, yearly, spent_year[d.year] - spent_month[bm], months_left)
        remaining = engine.remaining_month_budget(effective, spent_month[bm])
        f = flags[d]
        order = engine.propose_order(
            sig, remaining, f["weeks_left"], f["is_week_last_day"], cfg,
            current_values={t: holdings[t] * p for t, p in prices.items()}, prices=prices,
            bought_this_week=(bm, d.isocalendar()[:2]) in week_bought, is_month_last_day=f["is_month_last_day"])
        if order["action"] != "BUY" or not order.get("shares"):
            continue
        t, sh = order["etf"], order["shares"]
        cost = round(sh * prices[t], 2)
        holdings[t] += sh
        spent_month[bm] += cost
        spent_year[d.year] += cost
        week_bought.add((bm, d.isocalendar()[:2]))
        kind = "month_end" if f["is_month_last_day"] else f"x{order.get('multiplier', 1.0)}"
        kinds[kind] += 1
        buys.append({"trade_date": d.isoformat(), "budget_month": bm, "etf": t, "shares": sh,
                     "price_usd": round(prices[t], 2), "cost_usd": cost, "score": sig["score"], "kind": kind})
    return {"buys": buys, "kinds": kinds, "spent_month": dict(spent_month), "spent_year": dict(spent_year)}


def compare_months(cfg: dict, buys: list[dict], etf: dict, months: list[str], end_day: date) -> list[dict]:
    """월마다 실제 매수와 1일 일괄 매수를 비교한다. (engine.compare_first_day)"""
    by_month = defaultdict(list)
    for b in buys:
        by_month[b["budget_month"]].append(b)
    last = {t: market.close_on(s, end_day)[0] for t, s in etf.items()}
    rows = []
    for m in months:
        if not by_month[m]:
            continue
        first = market.first_session(cfg["market"]["calendar"], m)
        first_px = {t: market.close_on(s, first)[0] for t, s in etf.items()}
        r = engine.compare_first_day(by_month[m], first_px, last)
        bm_cost = sum(b["cost_usd"] for b in by_month[m])
        avg_diff = sum(e["avg_cost_diff_pct"] * e["cost_usd"] for e in r["etfs"]) / bm_cost
        rows.append({"month": m, "first_session": first.isoformat(), "buys": len(by_month[m]),
                     "cost_usd": r["cost_usd"], "actual_value_usd": r["actual_value_usd"],
                     "first_day_value_usd": r["first_day_value_usd"], "value_diff_usd": r["value_diff_usd"],
                     "value_diff_pct": r["value_diff_pct"], "avg_cost_diff_pct": round(avg_diff, 2),
                     "result": r["result"]})
    return rows


def write_report(cfg: dict, start: str, end: str, end_day: date, sim: dict, rows: list[dict]) -> Path:
    """backtest_report.md 와 backtest_months.csv 를 쓴다."""
    with open(ROOT / "backtest_months.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    cnt = Counter(r["result"] for r in rows)
    win, lose, tie, total = cnt["WIN"], cnt["LOSE"], cnt["TIE"], len(rows)
    cost = sum(r["cost_usd"] for r in rows)
    va, vb = sum(r["actual_value_usd"] for r in rows), sum(r["first_day_value_usd"] for r in rows)
    avg_cost_diff = sum(r["avg_cost_diff_pct"] * r["cost_usd"] for r in rows) / cost
    budget_total = sum(min(cfg["budget"]["monthly_usd"] * 12, cfg["budget"]["yearly_usd"])
                       for _ in {r["month"][:4] for r in rows})
    by_year = defaultdict(list)
    for r in rows:
        by_year[r["month"][:4]].append(r)
    L = [f"# 백테스트 리포트 ({start} ~ {end})", "",
         "규칙 엔진(engine.propose_order)대로 산 경우와 매월 \"1일 일괄 매수\"(그달 첫 거래일 종가에 같은 금액)를 비교한다.", "",
         "## 결과 요약", "",
         f"- **전체 {total}개월: 이긴 달 {win} · 진 달 {lose} · 같음 {tie}** (이긴 달 + 진 달 + 같음 = {win + lose + tie})",
         f"- 투자 금액 합계: ${cost:,.2f} (예산 ${budget_total:,.0f} 중 {cost / budget_total * 100:.1f}% 집행)",
         f"- {end_day} 평가액: 규칙 ${va:,.2f} vs 1일 매수 ${vb:,.2f} → 차이 **${va - vb:,.2f} ({(va / vb - 1) * 100:+.2f}%)**",
         f"- 금액 가중 평균 단가 차이: {avg_cost_diff:+.2f}% (음수면 규칙이 더 싸게 샀다는 뜻)",
         f"- 매수 횟수: {len(sim['buys'])}회 (월 평균 {len(sim['buys']) / total:.1f}회) · "
         + ", ".join(f"{k} {v}회" for k, v in sorted(sim["kinds"].items())), "",
         "## 연도별", "", "| 연도 | 이긴 달 | 진 달 | 투자 금액 | 평가액 차이 |", "|---|---|---|---|---|"]
    for y, rs in sorted(by_year.items()):
        L.append(f"| {y} | {sum(r['result'] == 'WIN' for r in rs)} | {sum(r['result'] == 'LOSE' for r in rs)} | "
                 f"${sum(r['cost_usd'] for r in rs):,.2f} | ${sum(r['value_diff_usd'] for r in rs):,.2f} |")
    srt = sorted(rows, key=lambda r: r["value_diff_pct"])
    L += ["", "## 가장 많이 진 달 · 이긴 달 (평가액 차이 %)", "", "| 구분 | 월 | 평균 단가 차이 | 평가액 차이 |", "|---|---|---|---|"]
    L += [f"| 진 달 | {r['month']} | {r['avg_cost_diff_pct']:+.2f}% | ${r['value_diff_usd']:,.2f} ({r['value_diff_pct']:+.2f}%) |"
          for r in srt[:5]]
    L += [f"| 이긴 달 | {r['month']} | {r['avg_cost_diff_pct']:+.2f}% | ${r['value_diff_usd']:,.2f} ({r['value_diff_pct']:+.2f}%) |"
          for r in reversed(srt[-5:])]
    L += ["", "## 가정과 한계", "",
          "- 신호: 거래일마다 직전 거래일까지의 ^GSPC·^VIX 로 계산 (실서비스와 같은 market.compute_signals). 데이터는 항상 신선(OK)으로 본다.",
          "- 매수: engine.propose_order 결과대로. 월·연 예산, 주 1회, 월말 전액 매수, 최소 1주 규칙을 모두 적용. 체결은 그날 종가, 정수 주.",
          "- 비교: 그달 실제로 쓴 금액을 ETF 별로 그달 첫 거래일 종가에 샀다고 가정 (소수 주 허용). 평가액은 마지막 거래일 종가 기준.",
          "- 정수 주 때문에 쓰지 못한 금액은 양쪽 모두 비교에서 빠진다 (1일 매수도 실제 쓴 금액만큼만 산다).",
          "- 배당·수수료·세금·환율은 넣지 않았다. 가격은 yfinance 종가(분할 반영).",
          f"- 월별 상세: evaluation/backtest_months.csv · 설정: config.yaml (월 ${cfg['budget']['monthly_usd']:,}, "
          f"ETF {', '.join(f'{t} {w:.0%}' for t, w in cfg['etfs'].items())})"]
    path = ROOT / "backtest_report.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def main() -> None:
    """백테스트를 실행하고 이긴 달·진 달 수를 출력한다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01")
    ap.add_argument("--end", default="2025-12")
    args = ap.parse_args()
    cfg = market.load_config()
    start = date.fromisoformat(args.start + "-01")
    ey, em = map(int, args.end.split("-"))
    end = (pd.Timestamp(ey, em, 1) + pd.offsets.MonthEnd(0)).date()
    sessions = [d.date() for d in mcal.get_calendar(cfg["market"]["calendar"]).schedule(start, end).index]
    idx, vix, etf = load_prices(cfg, start, end)
    end_day = sessions[-1]
    sim = simulate(cfg, sessions, idx, vix, etf)
    months = sorted({f"{d.year:04d}-{d.month:02d}" for d in sessions})
    rows = compare_months(cfg, sim["buys"], etf, months, end_day)
    path = write_report(cfg, args.start, args.end, end_day, sim, rows)
    cnt = Counter(r["result"] for r in rows)
    print(f"전체 {len(rows)}개월: 이긴 달 {cnt['WIN']} + 진 달 {cnt['LOSE']} + 같음 {cnt['TIE']} = "
          f"{cnt['WIN'] + cnt['LOSE'] + cnt['TIE']}")
    print("report:", path)


if __name__ == "__main__":
    main()
