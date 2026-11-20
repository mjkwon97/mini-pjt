"""SQLite 매수 장부와 보유 수량. 읽기 함수와 더미 적재만 둔다.

장부는 budget_month(어느 달 예산인지) 기준으로 묶고, 체결 날짜(trade_date)로 묶지 않는다.
장부 쓰기(record_buy)는 개발 보류다.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS buys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT NOT NULL,              -- 체결일 YYYY-MM-DD
    budget_month TEXT NOT NULL,              -- 예산 귀속 월 YYYY-MM
    etf          TEXT NOT NULL,
    shares       REAL NOT NULL CHECK (shares > 0),
    price_usd    REAL NOT NULL CHECK (price_usd > 0),
    cost_usd     REAL NOT NULL CHECK (cost_usd > 0),
    note         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_buys_budget_month ON buys(budget_month);
CREATE TABLE IF NOT EXISTS initial_holdings (
    etf    TEXT PRIMARY KEY,
    shares REAL NOT NULL CHECK (shares >= 0),
    note   TEXT DEFAULT ''
);
"""


def connect(cfg: dict) -> sqlite3.Connection:
    """config 의 ledger.db_path 로 장부 DB 에 연결하고 테이블이 없으면 만든다."""
    path = ROOT / cfg["ledger"]["db_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def load_dummy(conn: sqlite3.Connection, cfg: dict) -> int:
    """평가·데모용 더미 CSV 로 장부를 통째로 다시 채운다. 적재한 매수 행 수를 돌려준다."""
    buys_csv = ROOT / cfg["ledger"]["dummy_buys_csv"]
    holdings_csv = ROOT / cfg["ledger"]["dummy_holdings_csv"]
    with conn:
        conn.execute("DELETE FROM buys")
        conn.execute("DELETE FROM initial_holdings")
        with open(buys_csv, encoding="utf-8") as f:
            rows = [(r["trade_date"], r["budget_month"], r["etf"], float(r["shares"]),
                     float(r["price_usd"]), float(r["cost_usd"]), r.get("note", ""))
                    for r in csv.DictReader(f)]
        conn.executemany(
            "INSERT INTO buys (trade_date, budget_month, etf, shares, price_usd, cost_usd, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        with open(holdings_csv, encoding="utf-8") as f:
            conn.executemany(
                "INSERT INTO initial_holdings (etf, shares, note) VALUES (?, ?, ?)",
                [(r["etf"], float(r["shares"]), r.get("note", "")) for r in csv.DictReader(f)])
    return len(rows)


def spent_in_month(conn: sqlite3.Connection, budget_month: str) -> float:
    """budget_month(YYYY-MM) 예산으로 쓴 금액 합계(USD). 체결일이 다음 달이어도 포함된다."""
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM buys WHERE budget_month = ?",
                       (budget_month,)).fetchone()
    return round(row[0], 2)


def spent_in_year(conn: sqlite3.Connection, year: int) -> float:
    """그해 예산(budget_month 가 YYYY-로 시작)으로 쓴 금액 합계(USD)."""
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM buys WHERE budget_month LIKE ?",
                       (f"{year}-%",)).fetchone()
    return round(row[0], 2)


def monthly_totals(conn: sqlite3.Connection) -> dict[str, float]:
    """budget_month 별 사용 금액 합계(USD)."""
    rows = conn.execute("SELECT budget_month, SUM(cost_usd) FROM buys"
                        " GROUP BY budget_month ORDER BY budget_month").fetchall()
    return {m: round(v, 2) for m, v in rows}


def buy_dates(conn: sqlite3.Connection, budget_month: str) -> list[str]:
    """budget_month 예산으로 매수한 체결일 목록(오름차순). 주 1회 규칙 판정에 쓴다."""
    rows = conn.execute("SELECT DISTINCT trade_date FROM buys WHERE budget_month = ?"
                        " ORDER BY trade_date", (budget_month,)).fetchall()
    return [r[0] for r in rows]


def buys_in_month(conn: sqlite3.Connection, budget_month: str) -> list[dict]:
    """budget_month 예산으로 산 매수 행 목록."""
    rows = conn.execute("SELECT trade_date, etf, shares, price_usd, cost_usd FROM buys WHERE budget_month = ?"
                        " ORDER BY trade_date, id", (budget_month,)).fetchall()
    return [{"trade_date": d, "etf": e, "shares": s, "price_usd": p, "cost_usd": c} for d, e, s, p, c in rows]


def initial_holdings(conn: sqlite3.Connection) -> dict[str, float]:
    """서비스 사용 전부터 갖고 있던 ETF 수량 (사용자가 설정 화면에서 입력)."""
    return dict(conn.execute("SELECT etf, shares FROM initial_holdings ORDER BY etf").fetchall())


def set_initial_holdings(conn: sqlite3.Connection, holdings: dict[str, float]) -> None:
    """초기 보유 수량을 통째로 바꾼다. 매수 장부(buys)는 건드리지 않는다."""
    with conn:
        conn.execute("DELETE FROM initial_holdings")
        conn.executemany("INSERT INTO initial_holdings (etf, shares, note) VALUES (?, ?, ?)",
                         [(t, float(s), "설정 화면에서 입력") for t, s in holdings.items()])


def count_buys(conn: sqlite3.Connection) -> int:
    """장부의 매수 행 수."""
    return conn.execute("SELECT COUNT(*) FROM buys").fetchone()[0]


def record_buy(conn: sqlite3.Connection, trade_date: str, budget_month: str, etf: str,
               shares: float, price_usd: float, cost_usd: float, note: str = "") -> int:
    """체결 1건을 장부에 쓴다. 새 행 id 를 돌려준다.

    사람 승인(interrupt) 뒤에만 불러야 한다. 값 검증은 호출하는 쪽(tools.record_buy)이 먼저 한다.
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO buys (trade_date, budget_month, etf, shares, price_usd, cost_usd, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_date, budget_month, etf, float(shares), float(price_usd), float(cost_usd), note))
    return cur.lastrowid


def holdings(conn: sqlite3.Connection) -> dict[str, float]:
    """ETF 별 현재 보유 수량 = 초기 보유 + 장부의 매수 수량."""
    result = dict(conn.execute("SELECT etf, shares FROM initial_holdings").fetchall())
    for etf, sh in conn.execute("SELECT etf, SUM(shares) FROM buys GROUP BY etf").fetchall():
        result[etf] = result.get(etf, 0.0) + sh
    return result


if __name__ == "__main__":
    import json
    import sys

    from src.market import load_config

    cfg = load_config()
    conn = connect(cfg)
    if "--load-dummy" in sys.argv:
        print("loaded rows:", load_dummy(conn, cfg))
    print(json.dumps({
        "monthly_totals": monthly_totals(conn),
        "spent_2026": spent_in_year(conn, 2026),
        "holdings": holdings(conn),
        "buy_dates_2026-11": buy_dates(conn, "2026-11"),
    }, ensure_ascii=False, indent=2))
