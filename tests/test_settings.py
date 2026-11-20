"""사용자 초기 설정 검사·config.yaml 다시 쓰기 테스트. (네트워크·파일 쓰기 없음)"""

from pathlib import Path

import pytest
import yaml

from src.market import load_config
from src.settings import render_config, validate

CFG = load_config()
CONFIG_TEXT = (Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8")


def _payload(**kw):
    p = {"monthly_usd": 2000, "yearly_usd": 24000,
         "etfs": [{"ticker": "VOO", "target_pct": 50, "shares": 10},
                  {"ticker": "QQQ", "target_pct": 30, "shares": 2},
                  {"ticker": "SCHD", "target_pct": 20, "shares": 0}]}
    p.update(kw)
    return p


def test_valid_payload():
    """올바른 입력은 통과하고 정리된 값을 돌려준다."""
    r = validate(_payload(), CFG, price_ok=lambda t: True)
    assert r["errors"] == []
    assert r["clean"]["monthly_usd"] == 2000 and [e["ticker"] for e in r["clean"]["etfs"]] == ["VOO", "QQQ", "SCHD"]


@pytest.mark.parametrize("kw, msg", [
    ({"monthly_usd": 0}, "월 예산"),
    ({"yearly_usd": 1000}, "연 예산은 월 예산보다"),
    ({"etfs": []}, "하나 이상"),
    ({"etfs": [{"ticker": "VOO", "target_pct": 60}, {"ticker": "QQQ", "target_pct": 30}]}, "합계가 90%"),
    ({"etfs": [{"ticker": "360750", "target_pct": 100}]}, "올바르지 않습니다"),      # 국내 상장 코드
    ({"etfs": [{"ticker": "TQQQ", "target_pct": 100}]}, "레버리지"),
    ({"etfs": [{"ticker": "VOO", "target_pct": 50}, {"ticker": "VOO", "target_pct": 50}]}, "두 번"),
    ({"etfs": [{"ticker": "VOO", "target_pct": 100, "shares": -1}]}, "0 이상"),
])
def test_invalid_payloads(kw, msg):
    """잘못된 입력은 이유와 함께 막힌다."""
    r = validate(_payload(**kw), CFG, price_ok=lambda t: True)
    assert r["clean"] is None and any(msg in e for e in r["errors"]), r["errors"]


def test_lowercase_ticker_normalized():
    """소문자 코드는 대문자로 바꿔 받는다."""
    r = validate(_payload(etfs=[{"ticker": "voo", "target_pct": 100}]), CFG, price_ok=lambda t: True)
    assert r["errors"] == [] and r["clean"]["etfs"][0]["ticker"] == "VOO"


def test_new_etf_needs_price():
    """config 에 없는 새 ETF 는 가격을 받을 수 있어야 한다."""
    p = _payload(etfs=[{"ticker": "VOO", "target_pct": 70}, {"ticker": "VTI", "target_pct": 30}])
    assert validate(p, CFG, price_ok=lambda t: True)["errors"] == []
    r = validate(p, CFG, price_ok=lambda t: False)
    assert any("VTI" in e and "가격" in e for e in r["errors"])


def test_render_config_changes_only_target_values():
    """예산 두 값과 etfs 블록만 바뀌고, 주석·다른 설정은 그대로다."""
    clean = validate(_payload(), CFG, price_ok=lambda t: True)["clean"]
    new = render_config(CONFIG_TEXT, clean)
    parsed = yaml.safe_load(new)
    assert parsed["budget"] == {"monthly_usd": 2000, "yearly_usd": 24000}
    assert parsed["etfs"] == {"VOO": 0.5, "QQQ": 0.3, "SCHD": 0.2}
    old = yaml.safe_load(CONFIG_TEXT)
    for key in ("market", "signals", "multipliers", "order", "ledger", "rag", "guardrails", "output_check"):
        assert parsed[key] == old[key]
    assert new.count("#") == CONFIG_TEXT.count("#")  # 주석 수 유지
