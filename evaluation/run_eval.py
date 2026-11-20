"""평가 실행: test_queries.csv 12건을 에이전트에 보내고 LLM-as-Judge 와 RAGAS 로 채점해 리포트를 쓴다.

사용법: python -m evaluation.run_eval --round 1
결과: evaluation/round{N}_report.md, evaluation/round{N}_results.json

통과 기준(케이스별, 판정은 파이썬이 한다):
  - Judge 가 expected_traits 를 모두 충족, forbidden 을 하나도 어기지 않음
  - expected_tools 를 모두 호출 (guardrail 처럼 비어 있으면 도구를 하나도 부르지 않아야 함)
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
import warnings
from datetime import datetime
from pathlib import Path
from unittest import mock

from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore")

from langchain_aws import BedrockEmbeddings, ChatBedrockConverse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src import agent as agent_mod  # noqa: E402

ROOT = Path(__file__).resolve().parent
TOOL_STEPS = {"get_market_signals", "get_budget_status", "get_allocation_gap", "propose_order", "retrieve_docs",
              "record_buy", "compare_with_first_day"}
PASS_RATE = 10 / 12  # SERVICE.md 성공 기준: 약 83% 이상 통과 (12건 중 10건, 13건 중 11건)


def pass_target(n: int) -> int:
    """케이스 수에 맞는 통과 목표 건수 (약 83%, 올림)."""
    return math.ceil(n * PASS_RATE - 1e-9)


RAGAS_KEYS = ["faithfulness", "answer_relevancy", "llm_context_precision_with_reference", "context_recall"]


# ── 0. 시나리오 (질문이 가정하는 상태를 평가 중에만 만든다) ─────────────────
# edge 케이스는 "이미 샀다", "예산을 다 썼다", "월말인데 데이터 오류" 같은 상태를 가정한다.
# 실제 오늘 상태와 다르므로, 장부는 임시 복사본에 행을 더하고 신호·달력은 고정값으로 바꾼다. 실제 장부는 건드리지 않는다.

def _items(ret, high, vol, vix):
    """고정 신호 항목 (값, 점수)."""
    return [{"name": "daily_return", "label": "S&P 500 하루 등락률", "value": ret[0], "unit": "%", "points": ret[1]},
            {"name": "close_vs_20d_high", "label": "20일 고점 대비 종가", "value": high[0], "unit": "%", "points": high[1]},
            {"name": "down_day_volume_ratio", "label": "하락일 거래량 배수(20일 평균 대비)", "value": vol[0], "unit": "x",
             "points": vol[1]},
            {"name": "vix", "label": "VIX", "value": vix[0], "unit": "", "points": vix[1]}]


SCENARIOS = {
    "8": {"desc": "월 마지막 거래일(2026-11-30) + 시장 데이터 조회 실패(ERROR)",
          "week": {"trade_date": "2026-11-30", "weeks_left": 1, "is_week_last_day": True, "is_month_last_day": True},
          "signals": {"score": 0, "items": [], "data_date": "2026-11-26", "expected_session": "2026-11-27",
                      "freshness": "ERROR", "error": "yfinance 조회 실패"},
          "buys": []},
    "9": {"desc": "이번 주(2026-11-17)에 이미 매수 + VIX 35",
          "signals": {"score": 3, "items": _items((-0.5, 0), (94.0, 1), (0.9, 0), (35.0, 2)), "index_close": 7300.0,
                      "data_date": "2026-11-18", "expected_session": "2026-11-18", "freshness": "OK"},
          "buys": [("2026-11-17", "2026-11", "VOO", 1, 600.0, 600.0, "eval scenario 9")]},
    "10": {"desc": "이번 달 예산 소진(지출 $1,500) + S&P 500 −3%",
           "signals": {"score": 5, "items": _items((-3.0, 2), (92.0, 1), (1.6, 1), (28.0, 1)), "index_close": 7100.0,
                       "data_date": "2026-11-18", "expected_session": "2026-11-18", "freshness": "OK"},
           # cost_usd 가 None 이면 그달 월 예산을 딱 채우는 금액으로 계산한다 (장부가 바뀌어도 '소진' 상태 유지)
           "buys": [("2026-11-12", "2026-11", "SCHD", 1, None, None, "eval scenario 10")]},
    # 장부 기록(H1): 임시 장부에 기록하고, 승인 대기 뒤 '승인'으로 이어서 행이 1개 늘어나는지 본다
    "13": {"desc": "장부 기록 요청 → 승인 (임시 장부 복사본에 기록, 실제 장부는 그대로)",
           "buys": [], "followup": "승인"},
}


@contextlib.contextmanager
def scenario(case_id: str):
    """케이스에 시나리오가 있으면 임시 장부·고정 신호·고정 달력으로 바꿨다가 끝나면 되돌린다."""
    sc = SCENARIOS.get(case_id)
    if not sc:
        yield None
        return
    from src import market, tools
    base_cfg = market.load_config()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "ledger_eval.db"
        shutil.copy(ROOT.parent / base_cfg["ledger"]["db_path"], db)
        conn = sqlite3.connect(db)
        try:
            buys = []
            for td, bm, etf, sh, price, cost, note in sc["buys"]:
                if cost is None:
                    spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM buys WHERE budget_month = ?",
                                         (bm,)).fetchone()[0]
                    cost = round(base_cfg["budget"]["monthly_usd"] - spent, 2)
                    price = cost / sh
                buys.append((td, bm, etf, sh, price, cost, note))
            with conn:  # 커밋
                conn.executemany("INSERT INTO buys (trade_date, budget_month, etf, shares, price_usd, cost_usd, note) "
                                 "VALUES (?,?,?,?,?,?,?)", buys)
        finally:
            conn.close()  # Windows 는 열린 파일을 지울 수 없다
        cfg = {**base_cfg, "ledger": {**base_cfg["ledger"], "db_path": str(db)}}
        patches = [mock.patch.object(market, "load_config", lambda *a, **k: cfg)]
        if "signals" in sc:
            patches.append(mock.patch.object(tools, "_signals", lambda: dict(sc["signals"])))
        if "week" in sc:
            patches.append(mock.patch.object(market, "trade_week_info", lambda *a, **k: dict(sc["week"])))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield sc["desc"]


# ── 1. 에이전트 실행 ────────────────────────────────────────────────────

def run_case(row: dict, agent) -> dict:
    """한 케이스를 실행한다. BUY 승인 대기로 멈추면 HITL 케이스(3번)는 '승인'으로 이어서 두 턴을 합친다."""
    agent_mod._pending.clear()
    t = time.time()
    out = agent_mod.ask(row["input"], agent)
    turns = [{"user": row["input"], "answer": out["answer"]}]
    trace, contexts, trace_ids = list(out["trace"]), list(out["contexts"]), [out.pop("_trace_id", None)]
    followup = SCENARIOS.get(row["id"], {}).get("followup") or ("승인" if "확인한 다음" in row["input"] else None)
    if agent_mod._pending and followup:
        out2 = agent_mod.ask(followup, agent)
        turns.append({"user": followup, "answer": out2["answer"]})
        trace += out2["trace"]
        contexts += out2["contexts"]
        trace_ids.append(out2.pop("_trace_id", None))
    awaiting = bool(agent_mod._pending)
    agent_mod._pending.clear()
    answer = turns[0]["answer"] if len(turns) == 1 else \
        "\n\n".join(f"[사용자] {t_['user']}\n[에이전트] {t_['answer']}" for t_ in turns)
    called = [s["step"] for s in trace if s["step"] in TOOL_STEPS]
    return {"id": row["id"], "category": row["category"], "question": row["input"], "answer": answer,
            "turns": turns, "contexts": contexts, "trace": trace, "tools_called": called,
            "awaiting_approval": awaiting, "latency_s": round(time.time() - t, 1), "trace_ids": trace_ids}


def tools_ok(expected: str, called: list[str]) -> tuple[bool, list[str]]:
    """expected_tools 를 모두 불렀는지. 비어 있으면 도구를 하나도 부르지 않아야 한다. (통과 여부, 빠진 도구)"""
    want = [t for t in expected.split(";") if t.strip()]
    if not want:
        return not called, []
    missing = [t for t in want if t not in called]
    return not missing, missing


# ── 2. LLM-as-Judge ───────────────────────────────────────────────────

class TraitCheck(BaseModel):
    item: str = Field(description="평가 항목 원문")
    ok: bool = Field(description="expected 항목이면 충족 여부, forbidden 항목이면 '위반하지 않았는지' 여부")
    why: str = Field(description="한 문장 근거")


class Judgement(BaseModel):
    expected: list[TraitCheck]
    forbidden: list[TraitCheck]
    comment: str = Field(description="전체 평가 한두 문장")


JUDGE_PROMPT = """너는 미국 ETF 적립 타이밍 에이전트의 답을 채점하는 평가자다.
질문, 에이전트 답, 도구 실행 기록(trace)을 보고 항목마다 판정하라.

규칙:
- expected 항목: 답이 그 항목을 충족하면 ok=true. "~이면 ~" 같은 조건부 항목은 조건이 해당되지 않으면 ok=true.
- forbidden 항목: 답이 그 항목을 어기지 않았으면 ok=true, 어겼으면 ok=false.
- 숫자(금액·수량·점수)는 trace 의 도구 출력과 비교한다. 도구 출력에 없는 숫자를 만들었으면 해당 항목은 실패다.
- 항목은 ';' 로 나뉜 원문 그대로 하나씩 판정한다. 너그럽게 해석하지 말고, 답에 실제로 있는지로 판정한다.

[질문]
{question}

[평가 시나리오 — 이 케이스에서 도구가 보는 실제 상태]
{scenario}

[에이전트 답]
{answer}

[trace (도구 출력)]
{trace}

[expected 항목]
{expected}

[forbidden 항목]
{forbidden}
"""


def judge(row: dict, res: dict, llm) -> dict:
    """LLM 이 항목별로 판정하고, 통과 여부는 파이썬이 정한다."""
    trace_txt = json.dumps(res["trace"], ensure_ascii=False)[:12000]
    prompt = JUDGE_PROMPT.format(question=res["question"], answer=res["answer"], trace=trace_txt,
                                 scenario=res.get("scenario") or "없음 (오늘의 실제 장부·시장 데이터)",
                                 expected="\n".join(f"- {t.strip()}" for t in row["expected_traits"].split(";")),
                                 forbidden="\n".join(f"- {t.strip()}" for t in row["forbidden"].split(";")))
    j: Judgement = llm.with_structured_output(Judgement).invoke(prompt)
    return {"expected": [c.model_dump() for c in j.expected], "forbidden": [c.model_dump() for c in j.forbidden],
            "comment": j.comment, "pass": all(c.ok for c in j.expected) and all(c.ok for c in j.forbidden)}


# ── 3. RAGAS ──────────────────────────────────────────────────────────

def ragas_contexts(res: dict) -> list[str]:
    """RAGAS 용 근거: 검색 문서(contexts) + 도구 출력. 이 에이전트는 숫자 근거가 도구 출력에 있기 때문이다."""
    ctx = [c["text"] for c in res["contexts"]]
    for s in res["trace"]:
        if s["step"] in TOOL_STEPS - {"retrieve_docs"}:
            ctx.append(f"{s['step']} 결과: " + json.dumps(s["output"], ensure_ascii=False))
    return ctx


_BOILERPLATE = ("투자 자문이 아니며", "'승인' 또는 '거절'로 답해", "증권사 앱에서 직접 주문해 주세요")


def strip_boilerplate(answer: str) -> str:
    """RAGAS 채점 전에 사실 주장이 아닌 고정 문구(면책·승인 안내) 줄을 뺀다. 리포트에 이 처리를 밝힌다."""
    return "\n".join(l for l in answer.splitlines() if not any(b in l for b in _BOILERPLATE))


def run_ragas(rows: list[dict], results: list[dict], llm, emb) -> dict:
    """근거가 있는 케이스만 RAGAS 4개 지표로 채점한다. reference 는 csv 의 reference 열(없으면 expected_traits)."""
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy

    samples, ids = [], []
    for row, res in zip(rows, results):
        ctx = ragas_contexts(res)
        if not ctx:
            continue
        samples.append({"user_input": res["question"], "response": strip_boilerplate(res["answer"]),
                        "retrieved_contexts": ctx,
                        "reference": row.get("reference") or row["expected_traits"]})
        ids.append(res["id"])
    if not samples:
        return {}
    r = evaluate(EvaluationDataset.from_list(samples),
                 metrics=[Faithfulness(), ResponseRelevancy(), LLMContextPrecisionWithReference(), LLMContextRecall()],
                 llm=LangchainLLMWrapper(llm), embeddings=LangchainEmbeddingsWrapper(emb), show_progress=False)
    df = r.to_pandas()
    return {i: {k: (None if df.iloc[n][k] != df.iloc[n][k] else round(float(df.iloc[n][k]), 3)) for k in RAGAS_KEYS}
            for n, i in enumerate(ids)}


# ── 4. 리포트 ─────────────────────────────────────────────────────────

def _mean(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def write_report(rnd: int, rows: list[dict], results: list[dict], ragas: dict, model_id: str,
                 ragas_model: str) -> Path:
    """round{N}_report.md 를 쓴다."""
    passed = sum(r["pass"] for r in results)
    avg = {k: _mean([ragas.get(r["id"], {}).get(k) for r in results]) for k in RAGAS_KEYS}
    L = [f"# {rnd}차 평가 리포트", "",
         f"- 작성: {datetime.now():%Y-%m-%d %H:%M} · 에이전트·Judge 모델: `{model_id}` · RAGAS 채점 모델: `{ragas_model}` "
         f"· 케이스 {len(results)}건",
         f"- **통과 {passed} / {len(results)}** (목표: {pass_target(len(results))}건 이상, 약 83%)",
         "- 통과 기준: Judge 가 expected_traits 모두 충족 + forbidden 위반 없음 + expected_tools 모두 호출",
         "", "## RAGAS (근거가 있는 케이스 평균)", "",
         "| 지표 | 점수 | 목표 |", "|---|---|---|",
         f"| faithfulness | {avg['faithfulness']} | ≥ 0.80 |",
         f"| answer_relevancy | {avg['answer_relevancy']} | - |",
         f"| context_precision (with reference) | {avg['llm_context_precision_with_reference']} | - |",
         f"| context_recall | {avg['context_recall']} | ≥ 0.70 |", "",
         f"채점 케이스: {', '.join(sorted(ragas, key=int)) or '없음'} (근거가 없는 가드레일·무도구 답은 제외). "
         "retrieved_contexts = 검색 문서 + 도구 출력, reference = test_queries.csv 의 reference 열. "
         "response 에서는 사실 주장이 아닌 고정 문구(면책 고지·승인 안내) 줄을 빼고 채점했다.", "",
         "시나리오 케이스: " + "; ".join(f"{r['id']}번 {r['scenario']}" for r in results if r.get("scenario")), "",
         "## 케이스별 결과", "",
         "| id | 분류 | 질문 | 통과 | Judge | 도구 | 호출한 도구 | 지연(s) | faith | relev | c_prec | c_recall |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        g = ragas.get(r["id"], {})
        L.append(f"| {r['id']} | {r['category']} | {r['question'][:28]} | {'✅' if r['pass'] else '❌'} | "
                 f"{'O' if r['judge']['pass'] else 'X'} | {'O' if r['tools_ok'] else 'X'} | "
                 f"{', '.join(dict.fromkeys(r['tools_called'])) or '-'} | {r['latency_s']} | "
                 + " | ".join(str(g.get(k, '-')) for k in RAGAS_KEYS) + " |")
    L += ["", "## 실패 케이스 상세", ""]
    for r in results:
        if r["pass"]:
            continue
        L.append(f"### {r['id']}. {r['question']}")
        if not r["tools_ok"]:
            L.append(f"- 도구: 빠짐 {r['missing_tools'] or '(도구를 부르면 안 되는 케이스)'} / 호출 {r['tools_called']}")
        for c in r["judge"]["expected"]:
            if not c["ok"]:
                L.append(f"- 미충족: {c['item']} — {c['why']}")
        for c in r["judge"]["forbidden"]:
            if not c["ok"]:
                L.append(f"- 위반: {c['item']} — {c['why']}")
        L.append(f"- Judge 코멘트: {r['judge']['comment']}")
        L.append(f"- 답 (앞부분): {r['answer'][:300].replace(chr(10), ' ')}")
        L.append("")
    path = ROOT / f"round{rnd}_report.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def push_scores(results: list[dict], ragas: dict, rnd: int) -> None:
    """Langfuse 트레이스에 평가 점수를 붙인다. (키가 없으면 건너뛴다)"""
    lf = agent_mod.langfuse
    if lf is None:
        return
    for r in results:
        tid = r["trace_ids"][0]
        if not tid:
            continue
        lf.create_score(trace_id=tid, name=f"eval_pass_round{rnd}", value=1.0 if r["pass"] else 0.0,
                        comment=r["judge"]["comment"][:500])
        for k, v in ragas.get(r["id"], {}).items():
            if v is not None:
                lf.create_score(trace_id=tid, name=f"ragas_{k}", value=v)
    lf.flush()


def run_all(rows: list[dict], judge_llm) -> list[dict]:
    """모든 케이스를 에이전트로 실행하고 도구 확인·Judge 채점까지 한다."""
    agent = agent_mod.build_agent()
    results = []
    for row in rows:
        with scenario(row["id"]) as desc:
            res = run_case(row, agent)
        res["scenario"] = desc
        res["tools_ok"], res["missing_tools"] = tools_ok(row["expected_tools"], res["tools_called"])
        res["judge"] = judge(row, res, judge_llm)
        res["pass"] = res["judge"]["pass"] and res["tools_ok"]
        print(f"[{res['id']}] {'PASS' if res['pass'] else 'FAIL'} judge={res['judge']['pass']} "
              f"tools={res['tools_ok']} {res['latency_s']}s", flush=True)
        results.append(res)
    return results


def main() -> None:
    """평가 전체를 실행한다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--only", default="", help="쉼표로 구분한 케이스 id (디버그용)")
    ap.add_argument("--ragas-only", action="store_true",
                    help="에이전트·Judge 를 다시 돌리지 않고, 저장된 round{N}_results.json 으로 RAGAS 만 다시 채점")
    ap.add_argument("--ragas-model", default="", help="RAGAS 채점 LLM 모델 ID (기본: BEDROCK_MODEL_ID)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(ROOT / "test_queries.csv", encoding="utf-8")))
    if args.only:
        rows = [r for r in rows if r["id"] in args.only.split(",")]
    model_id = os.environ["BEDROCK_MODEL_ID"]
    judge_llm = ChatBedrockConverse(model=model_id, region_name=os.environ["AWS_DEFAULT_REGION"], temperature=0)
    emb = BedrockEmbeddings(model_id=os.environ["BEDROCK_EMBED_MODEL_ID"], region_name=os.environ["AWS_DEFAULT_REGION"])

    if args.ragas_only:
        saved = json.loads((ROOT / f"round{args.round}_results.json").read_text(encoding="utf-8"))
        ids = {r["id"] for r in rows}
        results = [r for r in saved if r["id"] in ids]
        rows = [r for r in rows if r["id"] in {x["id"] for x in results}]
    else:
        results = run_all(rows, judge_llm)

    ragas_model = args.ragas_model or model_id
    ragas_llm = judge_llm if ragas_model == model_id else         ChatBedrockConverse(model=ragas_model, region_name=os.environ["AWS_DEFAULT_REGION"], temperature=0)
    ragas = run_ragas(rows, results, ragas_llm, emb)
    for r in results:
        r["ragas"] = ragas.get(r["id"])
    (ROOT / f"round{args.round}_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    path = write_report(args.round, rows, results, ragas, model_id, ragas_model)
    try:
        push_scores(results, ragas, args.round)
    except Exception as e:  # 점수 전송 실패는 평가 결과에 영향을 주지 않는다
        print("Langfuse 점수 전송 실패:", e)
    print("report:", path)


if __name__ == "__main__":
    main()
