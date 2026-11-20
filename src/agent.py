"""메인 에이전트. Bedrock 모델 + 도메인 도구를 create_agent 로 묶는다."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid

from botocore.config import Config as BotoConfig
from dotenv import load_dotenv

load_dotenv()  # Langfuse 를 import 하기 전에 키를 읽어야 한다

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, ToolMessage
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src import guardrails, retriever
from src.market import load_config
from src.tools import TOOLS

TRACE_NAME = "answer-query"  # 트레이스 이름은 필터·대시보드 기준이므로 바꾸지 않는다


def mask_pii(*, data, **kwargs):
    """트레이스로 보내기 전에 계좌번호처럼 보이는 숫자(숫자 10자리 이상)를 가린다.

    날짜(2026-11-18)나 가격처럼 짧은 숫자는 그대로 둔다.
    """
    if isinstance(data, str):
        return guardrails.mask_text(data)
    if isinstance(data, dict):
        return {k: mask_pii(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask_pii(data=v) for v in data]
    return data


# 키가 있을 때만 트레이스를 켠다. 환경(development 등)은 LANGFUSE_TRACING_ENVIRONMENT 에서 읽는다.
langfuse = (
    Langfuse(mask=mask_pii)
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    else None
)

SYSTEM_PROMPT = """너는 미국 ETF 적립 타이밍 도우미다. 사용자는 미국 직투 계좌 하나로 매월 정한 달러 예산만큼 ETF 를 모은다.

규칙:
- 매수 판단 질문(오늘 사야 해?, 매수 추천, 확정해줘 등)에는 get_market_signals, get_budget_status, get_allocation_gap, propose_order 네 도구를 모두 호출한다. 앞의 세 도구로 근거(신호·예산·비중)를 확인한 뒤 마지막에 propose_order 를 부른다.
- 남은 예산이 0 이거나, 이미 샀거나, 데이터가 안 되는 상황이라도 매수 여부를 묻거나 더 사고 싶다는 질문이면 propose_order 를 반드시 호출해 엔진 결과(WAIT 와 rule_notes)로 답한다. 도구 결과만 보고 스스로 판단해 propose_order 를 건너뛰지 않는다.
- 예산 질문은 get_budget_status, 비중 질문은 get_allocation_gap 결과로 답한다. 비중 차이는 current_minus_target_pct 와 status(초과/부족)를 그대로 쓰고, 부호를 바꾸지 않는다.
- 규칙의 이유·근거, ETF 설명, 적립식·저점 매수 연구를 묻는 질문은 retrieve_docs 로 문서를 찾아 그 내용만으로 답하고, 근거 문서의 doc_id 를 [investment_rules#5] 처럼 밝힌다.
- 매수 금액·배수 규칙의 이유를 설명할 때는 그 규칙을 제한하는 규칙도 함께 설명한다: 배수 상한, 주 1회 매수 제한, 월 마지막 거래일의 남은 예산 전액 매수, 월·연 예산 한도. 이런 규칙 이유 질문은 첫 턴에 retrieve_docs 를 두 번 동시에 호출한다 (질문 내용으로 한 번, "매수 시점 주 1회 월말 전액 매수"로 한 번). 그 결과로 바로 답하고 더 검색하지 않는다. 문서에 없는 내용(배당금 예측, 시장 전망 등)은 모른다고 답한다.
- 배당금·보수율 등 ETF 정보 질문은 먼저 retrieve_docs 로 문서를 찾는다. 미래 값(다음 배당금 등)은 알 수 없다고 하고, 문서에 있는 내용만 출처와 함께 말하고 운용사 공시에서 확인하라고 안내한다.
- 시장 방향 예측(내일 오를까 등) 질문은 예측할 수 없다고 밝힌 뒤, get_market_signals 를 호출해 어젯밤 데이터 기준 신호 점수와 rule_tier(그 점수가 규칙상 어떤 날인지, 배수)를 알려준다. 이 질문에는 propose_order 를 부르지 않는다.
- 사용자가 말한 상황(이미 샀다, 예산을 다 썼다, 월말이다, 데이터가 안 된다 등)과 도구 결과(장부·달력·데이터)가 다르면, 다르다는 점을 먼저 밝히고 도구 결과 기준으로 답한다. 사용자 말만 믿고 숫자를 바꾸지 않는다.
- 사용자가 실제 체결 내역을 알려주며 기록(기록해줘, 적어줘, 장부에 남겨줘)을 요청하면 바로 record_buy 를 호출한다. 예: "VOO 3주 $1,512 기록해줘" → record_buy(etf="VOO", shares=3, total_cost_usd=1512). "QQQ 1주 주당 659.51달러" → record_buy(etf="QQQ", shares=1, price_usd=659.51).
  - 인자에는 사용자가 말한 값만 그대로 넣고 계산하지 않는다. 체결일·예산 귀속 월은 사용자가 말하지 않았으면 묻지 말고 비워 둔다 (도구가 정한다).
  - ETF·수량·금액(총액 또는 1주 가격) 중 하나라도 빠졌을 때만 도구를 부르지 말고 그 값을 물어본다. 실제 주문은 하지 않는다.
- "1일 매수랑 비교", "내 방식 성적" 같은 질문은 compare_with_first_day 를 호출한다 (지난달이면 month 를 비우고, 연도 없이 월만 말하면 month="8" 처럼 월 숫자만, 연도를 말했으면 YYYY-MM, 올해 전체는 month="year". 연도를 추측하지 않는다). ETF 별 실제 평균 단가와 1일 매수 평균 단가, 차이(%), 평가액 차이(USD)를 도구 값 그대로 보여 주고, 진 달(LOSE)도 숨기지 않고 밝힌다. 차이의 부호는 도구의 fields 설명대로 해석한다. 비교 기준(basis)과 평가 기준일도 적는다.
- 금액·ETF·수량·점수 같은 숫자는 도구 결과에 있는 값만 그대로 쓴다. 직접 계산하거나 바꾸지 않는다.
- 첫 줄에 BUY 또는 WAIT 를 밝히고, 금액(USD)·ETF·수량을 적는다. WAIT 이면 이유를 적는다.
- 데이터 기준일과 신호 근거(등락률, 20일 고점 대비, 거래량, VIX)를 짧게 설명한다.
- freshness 가 OK 가 아니면 신호 기반 추천을 하지 않는다고 밝힌다. 그래도 매수 판단·상황 질문이면 propose_order 는 반드시 호출한다 (월 마지막 거래일에는 데이터가 없어도 엔진이 남은 예산 전액 금액을 준다).
- propose_order 결과에 rule_notes 가 있으면 그 내용을 빠짐없이 설명한다. WAIT 이유를 설명할 때 관련 규칙의 예외·다음 단계도 함께 밝힌다: 주 1회 제한이면 다음 주에 다시 가능하고 월 마지막 거래일에는 남은 예산 전액 매수 예외가 있다는 점, 월 예산 소진이면 다음 달 예산으로 사고 연 예산을 넘는 추천은 하지 않는다는 점.
- 모든 금액은 USD 다. 수익을 보장하는 표현은 쓰지 않는다.
- 도구 결과에 없는 숫자(점수 만점, 확률, 목표가 등)를 만들지 않는다. 예: 신호 점수는 "2점"이라고만 쓰고 "2/10"처럼 쓰지 않는다.
- propose_order 결과의 approval 이 approved 면 "승인되어 확정"이라고, rejected 면 "추천을 취소했고 매수하지 않는다"고 밝힌다.
- 마지막 줄에 "투자 자문이 아니며, 실제 주문은 직접 하세요."라고 적는다.
- 규칙의 이유·연구 근거 질문은 표·제목·구분선 없이 글머리표 5~7개, 600자 안쪽으로 핵심만 답한다 (문서 인용 포함). 배수 규칙을 말할 때는 문서에 있는 점수 구간과 배수 값을 그대로 밝힌다.
- 한국어로 간결하게 답한다."""


def build_model(model_id: str | None = None, fail_fast: bool = False) -> ChatBedrockConverse:
    """Bedrock 모델을 만든다. model_id 가 없으면 .env 의 BEDROCK_MODEL_ID 를 쓴다.

    fail_fast=True 면 AWS 재시도를 하지 않는다. 예비 모델이 있을 때 기본 모델이 한도 초과 등으로
    실패하면 기다리지 않고 바로 예비 모델로 넘어가게 하려는 것이다.
    """
    extra = {"config": BotoConfig(retries={"max_attempts": 1, "mode": "standard"})} if fail_fast else {}
    return ChatBedrockConverse(
        model=model_id or os.environ["BEDROCK_MODEL_ID"],
        region_name=os.environ["AWS_DEFAULT_REGION"],
        name="generate-response",  # 트레이스의 generation 이름 (모델명은 별도 속성으로 남는다)
        **extra,
    )


def build_agent():
    """도구와 시스템 프롬프트를 연결한 에이전트 그래프를 만든다.

    .env 에 BEDROCK_FALLBACK_MODEL_ID 가 있으면, 기본 모델이 실패(토큰 한도 초과 등)할 때
    그 모델로 자동 전환한다.
    """
    retriever.warm_up()  # RAG 인덱스를 미리 만들어 첫 검색 질문의 지연을 없앤다
    middleware = []
    fallback_id = os.getenv("BEDROCK_FALLBACK_MODEL_ID")
    if fallback_id:
        middleware.append(ModelFallbackMiddleware(build_model(fallback_id)))
    return create_agent(build_model(fail_fast=bool(fallback_id)), tools=TOOLS,
                        system_prompt=SYSTEM_PROMPT, middleware=middleware, name="etf-timing-agent",
                        checkpointer=InMemorySaver())  # interrupt() 후 같은 thread 로 재개하려면 필요


def _parse(text: str):
    """도구 출력(JSON 문자열)을 가능하면 dict 로 바꾼다."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def to_trace(messages: list) -> list[dict]:
    """에이전트 메시지에서 도구 호출을 한 단계씩 [{step, input, output}] 로 뽑는다."""
    outputs = {m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage)}
    trace = []
    for m in messages:
        if isinstance(m, AIMessage):
            for call in m.tool_calls:
                trace.append({"step": call["name"], "input": call["args"],
                              "output": _parse(outputs.get(call["id"]))})
    return trace


def to_contexts(trace: list[dict]) -> list[dict]:
    """trace 의 retrieve_docs 단계 출력에서 응답 contexts [{doc_id, text}] 를 만든다. (중복 doc_id 는 한 번만)"""
    contexts, seen = [], set()
    for step in trace:
        if step["step"] != "retrieve_docs" or not isinstance(step["output"], list):
            continue
        for hit in step["output"]:
            if hit.get("doc_id") not in seen:
                seen.add(hit["doc_id"])
                contexts.append({"doc_id": hit["doc_id"], "text": hit["text"]})
    return contexts


def _text(content) -> str:
    """AIMessage content(문자열 또는 블록 목록)를 문자열로 만든다."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


# 승인 대기 중인 요청(매수 추천 또는 장부 기록). 사용자 1명 전제이므로 프로세스당 하나만 둔다.
# {"thread_id", "type": "buy_approval" | "record_approval", "payload": interrupt 값}
_pending: dict = {}
_INTERRUPT_STEP = {"buy_approval": "propose_order", "record_approval": "record_buy"}  # 멈추는 도구

_REJECT_PREFIX = ("거절", "취소", "아니", "안살")       # 단어가 이것으로 시작하면 거절
_APPROVE_PREFIX = ("승인", "확정", "진행", "좋아")      # 단어가 이것으로 시작하면 승인
_APPROVE_EXACT = ("네", "넵", "응", "예", "yes", "ok", "y")
_REJECT_EXACT = ("no", "n", "노")


def parse_approval(text: str) -> bool | None:
    """승인 대기 중의 답을 승인(True)·거절(False)·무관(None)으로 판정한다. (LLM 이 아니라 규칙으로)

    단어 단위로 본다. "예산"처럼 '예'로 시작하는 다른 단어는 승인으로 보지 않는다. 거절이 우선이다.
    """
    words = re.findall(r"[0-9A-Za-z가-힣]+", text.lower().replace("안 살", "안살"))
    if any(w.startswith(_REJECT_PREFIX) or w in _REJECT_EXACT for w in words):
        return False
    if any(w.startswith(_APPROVE_PREFIX) or w in _APPROVE_EXACT for w in words):
        return True
    return None


def is_bare_approval(text: str) -> bool:
    """메시지가 승인·거절 단어로만 이루어졌는지. ("승인", "아니, 취소해" → True / "네 오늘 사야 해?" → False)"""
    words = re.findall(r"[0-9A-Za-z가-힣]+", text.lower().replace("안 살", "안살"))
    return bool(words) and all(w.startswith(_REJECT_PREFIX + _APPROVE_PREFIX) or w in _APPROVE_EXACT + _REJECT_EXACT
                               for w in words)


NO_PENDING_MESSAGE = ("지금 승인을 기다리는 요청이 없습니다. (매수 추천이나 장부 기록 요청이 먼저 있어야 합니다)\n"
                      "'오늘 사야 해?'로 추천을 받거나, 'VOO 1주 $671.31 기록해줘'처럼 체결 내역을 알려 주세요.")


def signal_lines(order: dict) -> str:
    """승인 요청에 넣을 데이터 기준일·신호 근거 줄을 propose_order 결과로 만든다."""
    if order.get("freshness") != "OK":
        price = f", 가격 기준일 {order['price_date']}" if order.get("price_date") else ""
        return (f"- 시장 데이터: 기준일 {order.get('data_date') or '없음'}{price}, 신선도 {order.get('freshness')} "
                "→ 최신이 아니어서 신호 판단은 보류합니다.\n")
    items = ", ".join(f"{i.get('label', i['name'])} {i['value']}{i.get('unit', '')} (+{i['points']})"
                      for i in order.get("signal_items", []))
    return (f"- 신호 근거 (데이터 기준일 {order.get('data_date')}, 직전 미국 거래일): "
            f"점수 {order.get('signal_score')} = {items}\n")


def approval_message(order: dict) -> str:
    """승인 요청 문구를 엔진 결과 숫자로 만든다. (LLM 을 거치지 않는다)"""
    qty = f"{order['shares']}주 (예상 ${order['est_cost_usd']:,.2f}, 1주 ${order['price']:,.2f})" \
        if "shares" in order else f"${order['amount_usd']:,.2f} (수량은 증권사 앱 현재가로 계산)"
    return (f"BUY 추천: {order['etf']} {qty}\n"
            f"- 이유: {order['reason']}\n"
            f"{signal_lines(order)}"
            f"- 예산 귀속 월: {order.get('budget_month')}, 매수일: {order.get('trade_date')}\n\n"
            "이대로 확정할까요? '승인' 또는 '거절'로 답해 주세요.\n"
            "투자 자문이 아니며, 실제 주문은 직접 하세요.")


def decision_message(order: dict) -> str:
    """승인/거절 뒤 최종 답을 엔진 결과로 만든다. (LLM 문구에 기대지 않는다)"""
    tail = "\n투자 자문이 아니며, 실제 주문은 직접 하세요."
    if order.get("approval") != "approved":
        return (f"추천을 취소했습니다. 이번에는 매수하지 않습니다.\n"
                f"- 취소한 추천: {order['etf']} {order.get('shares', '')}주"
                f" (${order.get('est_cost_usd', order['amount_usd']):,.2f})\n"
                f"- 이번 달 남은 예산은 그대로 ${order['remaining_usd']:,.2f} 입니다." + tail)
    qty = f"{order['shares']}주, 예상 ${order['est_cost_usd']:,.2f}" if "shares" in order \
        else f"${order['amount_usd']:,.2f} (수량은 증권사 앱 현재가로 계산)"
    return (f"승인되어 확정했습니다: BUY {order['etf']} {qty}\n"
            f"- 매수일: {order.get('trade_date')} (예산 귀속 월 {order.get('budget_month')})\n"
            f"- 이유: {order['reason']}\n"
            "증권사 앱에서 직접 주문해 주세요. 체결되면 'QQQ 1주 $659.51 기록해줘'처럼 알려 주시면 장부에 기록합니다." + tail)


def record_approval_message(value: dict) -> str:
    """장부 기록 승인 요청 문구를 검사된 값으로 만든다. (LLM 을 거치지 않는다)"""
    e = value["entry"]
    lines = [f"장부 기록 요청: {e['etf']} {e['shares']:g}주, 총 ${e['cost_usd']:,.2f} (1주 ${e['price_usd']:,.2f})",
             f"- 체결일: {e['trade_date']}, 예산 귀속 월: {e['budget_month']}"]
    lines += [f"- 주의: {w}" for w in value.get("warnings", [])]
    lines += ["", "이대로 장부에 기록할까요? '승인' 또는 '거절'로 답해 주세요."]
    return "\n".join(lines)


def record_decision_message(result: dict) -> str:
    """장부 기록 승인/거절 뒤 최종 답을 도구 결과로 만든다."""
    e = result["entry"]
    if result.get("approval") != "approved":
        return (f"기록하지 않았습니다. 장부는 그대로입니다 ({result.get('rows_before')}건).\n"
                f"- 취소한 기록: {e['etf']} {e['shares']:g}주, 총 ${e['cost_usd']:,.2f}")
    return (f"장부에 기록했습니다: {e['etf']} {e['shares']:g}주, 총 ${e['cost_usd']:,.2f} (1주 ${e['price_usd']:,.2f})\n"
            f"- 체결일 {e['trade_date']}, 예산 귀속 월 {e['budget_month']}\n"
            f"- 장부 {result['rows_before']}건 → {result['rows_after']}건\n"
            f"- {e['budget_month']} 사용액 ${result['month_spent_usd']:,.2f}, 남은 예산 ${result['month_remaining_usd']:,.2f}")


def pending_message(kind: str, value: dict) -> str:
    """승인 대기 종류에 맞는 승인 요청 문구."""
    return record_approval_message(value) if kind == "record_approval" else approval_message(value["order"])


def final_message(kind: str, result: dict) -> str:
    """승인/거절 뒤 종류에 맞는 최종 답."""
    return record_decision_message(result) if kind == "record_approval" else decision_message(result)


def _invoke(agent, payload, thread_id: str, callbacks: list) -> dict:
    """에이전트를 한 번 실행(또는 재개)하고 {answer, contexts, trace} 로 정리한다.

    도중에 interrupt() 로 멈추면 승인 요청 문구를 답으로 돌려주고 _pending 에 기억한다.
    """
    config = {"configurable": {"thread_id": thread_id}, "callbacks": callbacks}
    result = agent.invoke(payload, config=config)
    messages = result["messages"]
    trace = to_trace(messages)
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        value = interrupts[0].value
        kind = value.get("type", "buy_approval")
        _pending.update({"thread_id": thread_id, "type": kind, "payload": value})
        shown = value.get("order") or value.get("entry")
        # 멈춘 도구는 아직 결과를 돌려주지 않았으므로, trace 에 도구가 만든 값(승인 대기)을 대신 채운다.
        for step in reversed(trace):
            if step["step"] == _INTERRUPT_STEP.get(kind) and step["output"] is None:
                step["output"] = {**value, "approval": "pending"} if kind == "record_approval"                     else {**shown, "approval": "pending"}
                break
        trace.append({"step": "approval_request", "input": {"type": kind, **value}, "output": "pending"})
        return {"answer": pending_message(kind, value), "contexts": to_contexts(trace), "trace": trace}
    return {"answer": _text(messages[-1].content), "contexts": to_contexts(trace), "trace": trace}


def _route(agent, question: str, callbacks: list) -> tuple[dict, str]:
    """승인 대기 중이면 답을 승인/거절로 재개하고, 아니면 새 질문으로 실행한다. (결과, thread_id)

    먼저 입력 가드레일을 거친다. 걸리면 에이전트·도구를 부르지 않고 거절 문구로 답한다.
    """
    guard = guardrails.check_input(question, load_config())
    if guard["blocked"]:
        _pending.clear()  # 대기 중인 추천이 있었다면 취소한다
        step = {"step": "input_guardrail", "input": {"question": guard["masked_question"]},
                "output": {"blocked": True, "categories": guard["categories"], "tickers": guard["tickers"]}}
        return {"answer": guard["answer"], "contexts": [], "trace": [step]}, None
    if _pending:
        decision = parse_approval(question)
        thread_id, kind, value = _pending["thread_id"], _pending["type"], _pending["payload"]
        _pending.clear()
        if decision is not None:
            out = _invoke(agent, Command(resume={"approved": decision}), thread_id, callbacks)
            out["trace"].insert(0, {"step": "approval", "input": {"question": question},
                                    "output": "approved" if decision else "rejected"})
            if _pending:  # 재개 뒤 또 다른 승인 요청으로 멈췄으면 그 요청 문구를 그대로 쓴다
                return out, thread_id
            decided = [s["output"] for s in out["trace"]
                       if s["step"] == _INTERRUPT_STEP[kind] and isinstance(s["output"], dict)
                       and s["output"].get("approval") in ("approved", "rejected")]
            fallback = {**(value.get("order") or value), "approval": "approved" if decision else "rejected"}
            out["answer"] = final_message(kind, decided[-1] if decided else fallback)
            return out, thread_id
        # 승인/거절이 아닌 새 질문이면 대기 중인 추천은 취소하고 새로 시작한다.
    elif is_bare_approval(question):
        # 대기 중인 요청 없이 '승인'·'거절'만 오면 LLM 에 넘기지 않고 바로 안내한다.
        step = {"step": "approval", "input": {"question": question}, "output": "no_pending_request"}
        return {"answer": NO_PENDING_MESSAGE, "contexts": [], "trace": [step]}, None
    thread_id = uuid.uuid4().hex
    out = _invoke(agent, {"messages": [{"role": "user", "content": question}]}, thread_id, callbacks)
    return _check_output(agent, out, question, thread_id, callbacks), thread_id


def _check_output(agent, out: dict, question: str, thread_id: str, callbacks: list) -> dict:
    """LLM 답을 출력 검사한다. 실패하면 같은 대화에서 다시 쓰게 하고, 끝내 실패하면 코드가 만든 답으로 바꾼다.

    승인 요청(코드가 만든 문구)으로 멈춘 경우는 검사하지 않는다.
    """
    if _pending:
        return out
    cfg = load_config()
    for attempt in range(cfg["output_check"]["max_retries"] + 1):
        result = guardrails.check_output(out["answer"], out["trace"], question, cfg)
        out["trace"].append({"step": "output_check", "input": {"attempt": attempt + 1}, "output": result})
        if result["ok"]:
            return out
        if attempt < cfg["output_check"]["max_retries"]:
            checks = [s for s in out["trace"] if s["step"] == "output_check"]
            retry = {"messages": [{"role": "user", "content": guardrails.retry_instruction(result)}]}
            out = _invoke(agent, retry, thread_id, callbacks)
            if _pending:  # 다시 쓰는 중에 승인 요청으로 멈추면 그 문구를 그대로 쓴다
                return out
            out["trace"] = out["trace"] + checks  # 대화 전체의 도구 단계 + 앞선 검사 기록
    out["answer"] = guardrails.fallback_answer(out["trace"])
    return out


def ask(question: str, agent=None) -> dict:
    """질문 하나를 에이전트에 보내고 {answer, contexts, trace} 를 돌려준다.

    BUY 추천이면 승인 요청으로 멈추고, 다음 질문의 '승인'/'거절'로 이어서 확정한다.
    Langfuse 가 켜져 있으면 질문 1건을 트레이스 1건(answer-query)으로 남기고,
    추천 → 승인처럼 이어지는 질문은 같은 session_id(thread_id)로 묶는다.
    """
    agent = agent or build_agent()
    if langfuse is None:
        return _route(agent, question, [])[0]

    with langfuse.start_as_current_observation(
        as_type="agent", name=TRACE_NAME, input={"question": question}
    ) as root:
        session = _pending.get("thread_id") or uuid.uuid4().hex
        with propagate_attributes(trace_name=TRACE_NAME, session_id=session,
                                  metadata={"model_id": os.environ["BEDROCK_MODEL_ID"]}):
            result, thread_id = _route(agent, question, [CallbackHandler()])
        root.update(
            output={"answer": result["answer"]},
            metadata={"tools_called": [s["step"] for s in result["trace"]], "thread_id": thread_id,
                      "awaiting_approval": bool(_pending)},
        )
        result["_trace_id"] = root.trace_id
    return result


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "오늘 사야 해?"
    out = ask(q)
    trace_id = out.pop("_trace_id", None)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if langfuse is not None:
        langfuse.flush()  # 짧은 스크립트는 종료 전에 전송을 끝낸다
        print(f"trace: {langfuse.get_trace_url(trace_id=trace_id)}", file=sys.stderr)
