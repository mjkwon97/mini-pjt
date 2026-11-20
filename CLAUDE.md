# 프로젝트 규칙

미국 ETF 적립 타이밍 Agent. 서비스 정의는 SERVICE.md 를 따른다.

## 작업 규칙
- 무엇을 만드는지는 SERVICE.md가 기준이다. 애매하면 추측하지 말고 물어본다.
- 스택: Python, LangGraph, Bedrock (global.anthropic.claude-sonnet-4-6, 리전 us-east-1. claude-3-5-sonnet 은 서비스 종료. 실패 시 .env 의 BEDROCK_FALLBACK_MODEL_ID(Haiku 4.5)로 자동 전환) -> 필요 시 모델은 조정해서 사용하거나 복수의 모델을 상황에 맞춰 변경하여 중단없이 쓸 수 있게 한다.
- 괜찮은 레퍼런스 코드를 깃허브에서 찾아서 레퍼런스로 삼거나 (있을 경우) 현재 ref 폴더 내 코드를 참조한다. 절대 사용자의 허락없이 신규 프레임워크 또는 라이브러리를 들이지 않는다.
- 한 번에 TASKS.md의 작업 하나만 한다.
- 계산, 규칙 판정은 LLM이 아니라 파이썬 코드로 하는 원칙으로 진행한다. 필요시 LLM 판정부분을 덧붙여 추가한다.
- API 키는 .env에서 읽는다.

## 기술 스택
- Python, LangChain, LangGraph
- 모델은 Amazon Bedrock (ChatBedrockConverse). 모델 ID 는 .env 의 BEDROCK_MODEL_ID 에서 읽는다.
- Agent 생성은 langchain.agents 의 create_agent 를 쓴다.
- API 서버는 FastAPI 로 `POST /query` 하나만 연다. 사용자 화면(사용자 요청으로 추가): `GET /` 은 대시보드 `src/static/index.html`, `GET /api/dashboard` 는 화면용 숫자(읽기 전용, LLM 호출 없음), `POST /api/settings` 는 사용자 초기 설정(월·연 예산, ETF·목표 비중, 초기 보유 수량) 저장 (파이썬 규칙 검사 후 config.yaml·장부에 씀). 질문·승인·장부 기록은 모두 `POST /query` 로만 한다.
- 시장 데이터는 yfinance, 거래일은 pandas_market_calendars("XNYS"), 장부는 sqlite3(표준 라이브러리)로 다룬다.
- RAG 는 FAISS 벡터 검색과 BM25 를 묶은 하이브리드 검색으로 하고, 임베딩도 Bedrock 을 쓴다.
- 트레이스는 Langfuse 로 남긴다 (LangChain CallbackHandler, 키는 .env 의 LANGFUSE_*). 평가는 RAGAS 와 LLM-as-Judge, 규칙 엔진 테스트는 pytest 로 한다.

## 폴더 구조 (제출 규약. 바꾸지 않는다)
src/agent.py       메인 에이전트 그래프
src/tools.py       도메인 도구
src/retriever.py   RAG 파이프라인
data/              사용한 문서와 데이터
evaluation/        test_queries.csv 와 평가 리포트

src 안의 역할별 파일:
src/engine.py      예산·주문 규칙 (순수 함수, LLM 호출 없음)
src/market.py      yfinance 조회·신호 계산·신선도 판정
src/ledger.py      SQLite 매수 장부·보유 수량
src/guardrails.py  입력·출력 필터 (등록 외 종목, 개인 금융정보, 프롬프트 인젝션)
src/api.py         FastAPI POST /query (+ GET / 대시보드, GET /api/dashboard 화면 데이터)
src/dashboard.py   대시보드 숫자 묶기 (도구와 같은 함수 사용, LLM 호출 없음, 목표 금액 차이·ETF 가격 기준일 확인 포함)
src/settings.py    사용자 초기 설정 검사·저장 (config.yaml 예산·etfs 값만 교체, 백업 config.yaml.bak)
src/static/index.html  사용자 대시보드 화면 (외부 라이브러리 없음)
tests/             engine.py 규칙 테스트 (pytest)

## 주고받는 형식 (제출 규약)
- POST /query 로 받고 question 필드를 읽는다.
- 답은 answer, contexts, trace 세 키로 돌려준다.
- contexts 는 [{"doc_id", "text"}], trace 는 [{"step", "input", "output"}] 형식이다. 도구 호출도 trace 에 한 단계씩 남긴다.

## 도메인 규칙 (어기면 버그다)
- 매수 금액·ETF·수량은 engine.propose_order 만 정한다. LLM 은 그 결과를 설명만 하고 숫자를 만들거나 바꾸지 않는다.
- 모든 금액은 USD 다. 계좌는 미국 직투 하나뿐이고 KRW 와 국내 상장 ETF 는 다루지 않는다.
- 매수는 주 1회, 신호 점수 2 이상인 날 또는 그 주 마지막 거래일에 한다. 배수는 1.0 / 1.5 / 2.0 이다.
- 월 마지막 거래일에는 남은 예산을 전액 매수한다. 이 매수는 주 1회 제한보다 우선한다.
- 주는 ISO 주(월~일) 중 해당 월에 속한 부분으로 정한다. 장부는 budget_month 기준으로 묶고, 체결 날짜로 묶지 않는다.
- 연 예산을 넘는 추천은 하지 않는다. 남은 연 예산이 부족하면 그달 예산을 줄인다.
- 시장 데이터가 직전 NYSE 거래일 기준이 아니면 신호 기반 추천을 하지 않는다.
- 장부 기록(record_buy)은 interrupt() 로 사람 승인을 받은 뒤에만 실행한다.
- propose_order 결과가 BUY 이면 interrupt() 로 사람 승인을 받은 뒤에 확정 안내한다.
- 규칙 숫자(임계값·배수·예산)는 config.yaml 에서 읽고 코드에 적지 않는다.

## 코드 규칙
- 파일 하나에 한 가지 역할만 둔다.
- 함수와 도구에는 한국어 docstring 을 쓴다.
- 비밀 값은 .env 에서 읽고 코드에 적지 않는다.
- engine.py 를 바꾸면 tests/ 의 관련 테스트도 같이 고치고 pytest 를 돌린다.

## 하지 말 것
- 요청하지 않은 파일을 새로 만들지 않는다.
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다.
- 실제 증권사 주문 API 를 호출하는 코드를 만들지 않는다.
