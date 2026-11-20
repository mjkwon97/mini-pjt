"""거래 주(ISO 주 중 그달 부분)·매수일·남은 예산 계산 테스트. NYSE(XNYS) 달력 기준."""

from datetime import datetime, timezone

import pytest

from src.engine import remaining_month_budget
from src.market import last_completed_session, trade_week_info

CAL = "XNYS"


def utc(*args):
    """UTC 시각을 만든다."""
    return datetime(*args, tzinfo=timezone.utc)


def test_morning_after_close_buys_next_session():
    """한국 목요일 아침(미국 수요일 장 종료 후)이면 매수일은 목요일, 11월 남은 주는 3개."""
    w = trade_week_info(CAL, utc(2026, 11, 19, 1, 0))
    assert w["trade_date"] == "2026-11-19"
    assert w["weeks_left"] == 3  # 11/16~22, 11/23~29, 11/30(하루짜리 주)
    assert w["is_week_last_day"] is False


def test_during_session_uses_previous_completed_day():
    """장중이면 오늘 장은 아직 안 끝났으므로 직전 완료 거래일은 전날이다."""
    now = utc(2026, 11, 19, 15, 0)  # 뉴욕 오전 10시
    assert last_completed_session(CAL, now).isoformat() == "2026-11-18"
    assert trade_week_info(CAL, now)["trade_date"] == "2026-11-19"


def test_weekend_rolls_to_monday():
    """토요일이면 직전 완료일은 금요일, 매수일은 월요일."""
    now = utc(2026, 11, 21, 12, 0)
    assert last_completed_session(CAL, now).isoformat() == "2026-11-20"
    w = trade_week_info(CAL, now)
    assert w["trade_date"] == "2026-11-23"
    assert w["weeks_left"] == 2


def test_holiday_makes_friday_week_last_day():
    """추수감사절(11/26) 휴장을 건너뛰고 11/27 이 매수일이며, 그 주 마지막 거래일이다."""
    w = trade_week_info(CAL, utc(2026, 11, 26, 0, 0))
    assert w["trade_date"] == "2026-11-27"
    assert w["is_week_last_day"] is True
    assert w["weeks_left"] == 2


def test_one_day_week_at_month_end():
    """11/30(월)은 그 ISO 주에서 11월에 속한 유일한 거래일 → 하루짜리 주이자 월 마지막 거래일."""
    w = trade_week_info(CAL, utc(2026, 11, 27, 22, 0))  # 11/27 조기 폐장 후
    assert w["trade_date"] == "2026-11-30"
    assert w["weeks_left"] == 1
    assert w["is_week_last_day"] is True
    assert w["is_month_last_day"] is True


def test_one_day_week_at_month_start_and_memorial_day():
    """5/1(금)은 하루짜리 첫 주. 5월은 메모리얼데이(5/25)가 있어도 거래 주 5개."""
    w = trade_week_info(CAL, utc(2026, 4, 30, 22, 0))
    assert w["trade_date"] == "2026-05-01"
    assert w["is_week_last_day"] is True
    assert w["weeks_left"] == 5  # 5/1, 5/4~8, 5/11~15, 5/18~22, 5/26~29
    assert w["is_month_last_day"] is False


@pytest.mark.parametrize("monthly, spent, expected", [
    (1500, 0, 1500),
    (1500, 668.90, 831.10),
    (1500, 1500, 0),
    (1500, 1600, 0),  # 초과 사용해도 음수가 되지 않는다
])
def test_remaining_month_budget(monthly, spent, expected):
    """남은 월 예산 = 월 예산 − 사용액, 최소 0."""
    assert remaining_month_budget(monthly, spent) == pytest.approx(expected)
