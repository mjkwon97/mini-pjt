"""입력·출력 가드레일. 입력: 등록 외 종목·레버리지, 개인 금융정보, 프롬프트 인젝션. 출력: 숫자 대조, 수익 보장 표현.

LLM 을 쓰지 않는다. 입력이 걸리면 에이전트·도구를 호출하지 않고 거절 문구로 답한다. 출력이 걸리면 agent.py 가 다시 쓰게 한다.
종목 목록은 config.yaml 의 etfs·guardrails 에서 읽는다.
"""

from __future__ import annotations

import re

# 숫자 10자리 이상(하이픈 포함)은 계좌·카드번호로 본다. 날짜(2026-11-18, 8자리)나 가격은 해당하지 않는다.
_NUMBER_RUN = re.compile(r"\d[\d -]{8,}\d")
_PII_WORDS = re.compile(r"계좌\s*번호|비밀\s*번호|비번|주민\s*(등록)?\s*번호|카드\s*번호|공인\s*인증|OTP|보안\s*카드",
                        re.I)
_BALANCE_STORE = re.compile(r"(잔고|잔액|평가금).{0,15}(기억|저장|알아\s*둬|적어\s*둬)")

_INJECTION = [re.compile(p, re.I) for p in (
    r"시스템\s*프롬프트", r"system\s*prompt", r"프롬프트.{0,6}(보여|알려|출력|공개)",
    r"(규칙|지시|명령|설정|제한).{0,4}무시", r"ignore\s+(all\s+|the\s+)?(previous|above|prior|your)",
    r"(이전|위의?)\s*(지시|명령)", r"너는\s*이제", r"역할을\s*바꿔", r"개발자\s*모드", r"developer\s*mode",
    r"jailbreak", r"탈옥", r"내부\s*(규칙|설정|지침).{0,6}(보여|알려|공개)",
)]
_TRADE_INTENT = re.compile(r"사(야|도|고|줘|려|면|자|요|ㄹ)|살[게래까]|매수|넣|담아|추천|투자|갈아타|매도|팔[아고까래]|비중")
_UPPER_TOKEN = re.compile(r"(?<![A-Za-z])[A-Z]{2,5}(?![A-Za-z])")


def count_digits(text: str) -> int:
    """문자열 안의 숫자 개수."""
    return sum(c.isdigit() for c in text)


def mask_text(text: str) -> str:
    """계좌번호처럼 보이는 숫자(10자리 이상)를 [MASKED] 로 가린다."""
    return _NUMBER_RUN.sub(lambda m: "[MASKED]" if count_digits(m.group()) >= 10 else m.group(), text)


def find_tickers(question: str, cfg: dict) -> dict:
    """질문에서 등록 외 종목·레버리지 상품을 찾는다.

    결과: {"leveraged": [...], "unregistered": [...]}. 일반 등록 외 종목은 매수 의도 단어가 있을 때만 잡는다.
    """
    g = cfg["guardrails"]
    registered = {t.upper() for t in cfg["etfs"]}
    lowered = question.lower()
    leveraged = [t for t in g["leveraged_tickers"]
                 if re.search(rf"(?<![A-Za-z]){t.lower()}(?![A-Za-z])", lowered)]
    leveraged += [w for w in g["leveraged_words"] if w.lower() in lowered]
    unregistered = [n for n in g["blocked_names"] if n in question]
    if _TRADE_INTENT.search(question):
        skip = registered | {w.upper() for w in g["non_ticker_words"]} | {t.upper() for t in g["leveraged_tickers"]}
        unregistered += [t for t in _UPPER_TOKEN.findall(question) if t not in skip]
    return {"leveraged": sorted(set(leveraged)), "unregistered": sorted(set(unregistered))}


def has_pii(question: str) -> bool:
    """계좌번호 같은 숫자, 개인 금융정보 단어, 잔고 저장 요청이 있으면 True."""
    long_number = any(count_digits(m.group()) >= 10 for m in _NUMBER_RUN.finditer(question))
    return long_number or bool(_PII_WORDS.search(question)) or bool(_BALANCE_STORE.search(question))


def has_injection(question: str) -> bool:
    """시스템 프롬프트 공개·규칙 무시·역할 변경 요구가 있으면 True."""
    return any(p.search(question) for p in _INJECTION)


def refusal_message(result: dict, cfg: dict) -> str:
    """걸린 항목별 거절 문구와 정상 안내를 코드로 만든다. 계좌번호 등은 다시 출력하지 않는다."""
    etfs = "·".join(cfg["etfs"])
    lines = ["요청하신 내용 중 도와드릴 수 없는 부분이 있습니다."]
    if "ticker" in result["categories"]:
        names = ", ".join(result["tickers"]["leveraged"] + result["tickers"]["unregistered"])
        lines.append(f"- 등록된 ETF({etfs}) 밖의 종목이나 레버리지·인버스·옵션 상품({names})은 추천하지 않습니다.")
    if "pii" in result["categories"]:
        lines.append("- 계좌번호·잔고 같은 개인 금융정보는 저장하거나 다시 보여 드리지 않습니다. 대화에 적지 않으셔도 됩니다.")
    if "injection" in result["categories"]:
        lines.append("- 시스템 프롬프트 공개나 규칙 무시 요청은 따를 수 없습니다. 매수 금액·ETF 는 항상 규칙 엔진 결과로만 안내합니다.")
    lines.append(f"\n대신 '오늘 사야 해?', '이번 달 예산 얼마 남았어?', '내 비중이 목표랑 얼마나 달라?'처럼 물어보시면 "
                 f"등록 ETF({etfs})와 정해 둔 규칙대로 안내해 드립니다.")
    lines.append("투자 자문이 아니며, 실제 주문은 직접 하세요.")
    return "\n".join(lines)


def check_input(question: str, cfg: dict) -> dict:
    """질문을 검사해 {blocked, categories, tickers, masked_question, answer} 를 돌려준다.

    categories: ticker(등록 외·레버리지), pii(개인 금융정보), injection(프롬프트 인젝션). 하나라도 있으면 blocked.
    """
    tickers = find_tickers(question, cfg)
    categories = []
    if tickers["leveraged"] or tickers["unregistered"]:
        categories.append("ticker")
    if has_pii(question):
        categories.append("pii")
    if has_injection(question):
        categories.append("injection")
    result = {"blocked": bool(categories), "categories": categories, "tickers": tickers,
              "masked_question": mask_text(question)}
    result["answer"] = refusal_message(result, cfg) if categories else None
    return result


# ── 출력 검사 ─────────────────────────────────────────────────────────

# 숫자 검사에서 빼는 표기: 날짜, 연·월·일, 문서 번호, 지수 이름 속 숫자
_SKIP_SPANS = re.compile(
    r"\d{4}-\d{2}(-\d{2})?|\d{4}\s*년|\d{1,2}\s*월|\d{1,2}\s*일|#\d+|S&P\s*500|Nasdaq-100|Dividend\s*100|\d{2,4}\s*[~∼–-]\s*\d{2,4}",
    re.I)
_ANSWER_NUMBER = re.compile(r"(\$\s?)?([-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-−]?\d+(?:\.\d+)?)\s*(%|USD|달러|배)?")
_ANY_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
# 수익 보장 표현. 바로 뒤에 부정(않/없/못/아니)이 오면 보장 표현으로 보지 않는다.
_GUARANTEE = re.compile(r"(반드시|무조건|확실히|틀림없이|100\s*%)\s*(오르|오릅|오를|올라|상승|수익|이익|벌|반등)|원금\s*보장|수익\s*(을|이)?\s*보장|"
                        r"손실\s*(이)?\s*(없|안\s*나)|절대\s*(손해|손실)|guarantee")
_NEGATION = re.compile(r"^.{0,8}(않|없|못|아니|불가)")


def _to_float(text: str) -> float:
    """'1,500.25' · '−3' 같은 문자열을 float 로 바꾼다."""
    return float(text.replace(",", "").replace("−", "-"))


def collect_numbers(obj) -> set[float]:
    """도구 출력(dict·list·문자열)에 들어 있는 모든 숫자를 모은다. 문자열 속 숫자(reason, 문서 text)도 포함."""
    found: set[float] = set()
    if isinstance(obj, bool) or obj is None:
        return found
    if isinstance(obj, (int, float)):
        found.add(float(obj))
    elif isinstance(obj, str):
        found.update(_to_float(m) for m in _ANY_NUMBER.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= collect_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= collect_numbers(v)
    return found


def answer_numbers(answer: str) -> list[tuple[str, float, str | None]]:
    """답에서 검사할 숫자를 뽑는다: $·%·USD·달러·배가 붙었거나, 소수점·천 단위 쉼표가 있는 숫자.

    날짜·연도·문서 번호·지수 이름 속 숫자, 단위 없는 작은 정수(주 수, 순번 등)는 검사하지 않는다.
    """
    text = _SKIP_SPANS.sub(" ", answer)
    out = []
    for m in _ANSWER_NUMBER.finditer(text):
        dollar, num, unit = m.group(1), m.group(2), m.group(3)
        if dollar or unit or "." in num or "," in num:
            out.append((m.group(0).strip(), _to_float(num), unit))
    return out


def _matches(value: float, unit: str | None, allowed: set[float], tol: float) -> bool:
    """value 가 허용 숫자 중 하나와 tol 비율 안에서 같은지. 부호는 보지 않는다.

    % 가 붙은 숫자만 비율(0.95 → 95%) 환산을 허용한다. 금액에는 환산을 적용하지 않는다.
    """
    v = abs(value)
    if v == 0:
        return True
    for a in allowed:
        cands = (abs(a), abs(a) * 100) if unit == "%" and abs(a) <= 2 else (abs(a),)
        for cand in cands:
            if abs(v - cand) <= tol * cand or abs(v - cand) < 0.005:
                return True
    return False


def find_guarantees(answer: str) -> list[str]:
    """수익 보장 표현을 찾는다. ('보장하지 않습니다'처럼 부정이 붙으면 제외)"""
    hits = []
    for m in _GUARANTEE.finditer(answer):
        if not _NEGATION.match(answer[m.end():]):
            hits.append(m.group(0))
    return hits


def check_output(answer: str, trace: list[dict], question: str, cfg: dict) -> dict:
    """LLM 답을 검사한다. (LLM 호출 없음)

    1) 답의 숫자가 도구 결과·질문에 있는 숫자와 tolerance_pct 안에서 같은지
    2) 수익 보장 표현이 없는지
    결과: {"ok", "unmatched_numbers", "guarantee_phrases", "checked_count"}
    """
    tol = cfg["output_check"]["tolerance_pct"] / 100
    allowed = collect_numbers([s.get("output") for s in trace]) | collect_numbers(question)
    nums = answer_numbers(answer)
    unmatched = [raw for raw, v, unit in nums if not _matches(v, unit, allowed, tol)]
    guarantees = find_guarantees(answer)
    return {"ok": not unmatched and not guarantees, "unmatched_numbers": unmatched,
            "guarantee_phrases": guarantees, "checked_count": len(nums)}


def retry_instruction(result: dict) -> str:
    """검사 실패 시 LLM 에게 다시 쓰라고 보내는 지시문."""
    parts = []
    if result["unmatched_numbers"]:
        parts.append(f"답의 숫자 {', '.join(result['unmatched_numbers'])} 는 도구 결과에 없다. "
                     "직접 계산하거나 만든 숫자를 빼고, 도구 결과에 있는 숫자만 그대로 써서")
    if result["guarantee_phrases"]:
        parts.append(f"수익 보장 표현({', '.join(result['guarantee_phrases'])})을 빼고")
    return "[자동 검사] " + " ".join(parts) + " 같은 질문에 대한 답을 다시 써라. 도구를 다시 부르지 않아도 된다."


_SUMMARY_KEYS = {
    "propose_order": ("action", "etf", "shares", "amount_usd", "est_cost_usd", "multiplier", "reason", "trade_date"),
    "get_budget_status": ("budget_month", "remaining_month_usd", "weeks_left", "bought_this_week",
                          "remaining_year_usd", "unspent_prev_month_usd"),
    "get_market_signals": ("score", "data_date", "freshness"),
    "get_allocation_gap": ("most_underweight", "total_value_usd"),
}


def fallback_answer(trace: list[dict]) -> str:
    """검사를 끝내 통과하지 못하면, LLM 문장 대신 도구 결과의 핵심 값만 코드로 보여 준다."""
    lines = ["자동 검사에서 답변의 숫자가 도구 결과와 맞지 않아, 도구 결과를 그대로 안내합니다."]
    for s in trace:
        keys = _SUMMARY_KEYS.get(s["step"])
        if keys and isinstance(s.get("output"), dict):
            vals = [f"{k}={s['output'][k]}" for k in keys if s["output"].get(k) is not None]
            lines.append(f"- {s['step']}: " + ", ".join(vals))
        elif s["step"] == "retrieve_docs" and isinstance(s.get("output"), list):
            lines.append("- 관련 문서: " + ", ".join(h["doc_id"] for h in s["output"]))
    lines.append("모든 금액은 USD 입니다. 투자 자문이 아니며, 실제 주문은 직접 하세요.")
    return "\n".join(lines)
