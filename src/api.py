"""FastAPI 서버. API 는 POST /query 하나이고, GET / 은 사용자 화면(웹 페이지)을 보여 준다.

요청: {"question": "..."}
응답: {"answer": str, "contexts": [{"doc_id", "text"}], "trace": [{"step", "input", "output"}]}
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src import agent as agent_module
from src import dashboard
from src import settings as user_settings

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 에이전트를 한 번 만들고, 종료 시 남은 트레이스를 보낸다."""
    _state["agent"] = agent_module.build_agent()
    yield
    if agent_module.langfuse is not None:
        agent_module.langfuse.shutdown()


app = FastAPI(title="ETF 적립 타이밍 Agent", lifespan=lifespan)


class QueryRequest(BaseModel):
    """질문 요청 본문."""

    question: str = Field(..., min_length=1, description="사용자 질문")


class Context(BaseModel):
    """답변 근거 문서 조각."""

    doc_id: str
    text: str


class TraceStep(BaseModel):
    """도구 호출 한 단계."""

    step: str
    input: Any
    output: Any


class QueryResponse(BaseModel):
    """제출 규약 응답 형식 (answer, contexts, trace)."""

    answer: str
    contexts: list[Context]
    trace: list[TraceStep]


PAGE = Path(__file__).resolve().parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """사용자 화면. 브라우저에서 http://127.0.0.1:8000 을 열면 질문·승인을 버튼으로 할 수 있다."""
    return FileResponse(PAGE, media_type="text/html; charset=utf-8")


@app.get("/api/dashboard", include_in_schema=False)
def dashboard_data() -> dict:
    """대시보드 숫자 (읽기 전용, LLM 호출 없음). 엔진·장부 함수 결과를 그대로 돌려준다."""
    return dashboard.build_dashboard()


@app.post("/api/settings", include_in_schema=False)
def save_settings(payload: dict) -> dict:
    """사용자 초기 설정 저장 (월·연 예산, ETF·목표 비중, 초기 보유 수량). 파이썬 규칙으로 검사한 뒤에만 쓴다."""
    return user_settings.save(payload)


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    """질문을 에이전트에 보내고 answer·contexts·trace 를 돌려준다."""
    result = agent_module.ask(req.question, agent=_state.get("agent"))
    result.pop("_trace_id", None)
    return QueryResponse(**result)
