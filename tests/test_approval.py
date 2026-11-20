"""승인 대기 중 사용자 답을 승인/거절/무관으로 판정하는 규칙 테스트."""

import pytest

from src.agent import is_bare_approval, parse_approval


@pytest.mark.parametrize("text", ["승인", "승인할게", "네", "네!", "응 확정해줘", "좋아요 진행해", "OK", "yes"])
def test_approve(text):
    """승인 표현은 True."""
    assert parse_approval(text) is True


@pytest.mark.parametrize("text", ["거절", "아니요", "아니 취소해", "안 살래", "no", "승인 말고 취소"])
def test_reject(text):
    """거절 표현은 False. 승인과 거절이 섞이면 거절이 우선."""
    assert parse_approval(text) is False


@pytest.mark.parametrize("text", ["이번 달 예산 얼마 남았어?", "오늘 사야 해?", "VOO 배당금 얼마야?", "내 비중 보여줘"])
def test_unrelated_question(text):
    """승인과 무관한 새 질문은 None ('예산'의 '예'를 승인으로 보지 않는다)."""
    assert parse_approval(text) is None


@pytest.mark.parametrize("text", ["승인", "승인할게", "네", "OK", "거절", "아니, 취소해", "안 살래", "확정해줘"])
def test_bare_approval(text):
    """승인·거절 단어로만 된 메시지는 True (대기 중인 요청이 없으면 바로 안내한다)."""
    assert is_bare_approval(text) is True


@pytest.mark.parametrize("text", ["네 오늘 사야 해?", "아니 이번 달 예산 얼마야?", "오늘 사야 해?", "VOO 1주 $671.31 기록해줘", ""])
def test_not_bare_approval(text):
    """다른 내용이 섞인 질문은 False (LLM 으로 처리)."""
    assert is_bare_approval(text) is False
