# 미국 ETF 적립 타이밍 Agent

매일 아침 "오늘 사야 해?"라고 물으면, 어젯밤 미국 시장 신호·이번 달 남은 예산·목표 비중을 근거로
**BUY/WAIT, 금액(USD), ETF, 수량**을 규칙대로 답하는 개인용 에이전트입니다.

- **숫자는 규칙 엔진(파이썬)만 정합니다.** LLM 은 도구 결과를 설명만 하고 금액·수량을 만들거나 바꾸지 않습니다.
- 매수 추천과 장부 기록은 **사람 승인(`interrupt()`) 뒤에만** 확정됩니다. 실제 주문은 하지 않습니다.
- 내 방식이 "매월 1일 일괄 매수"보다 나았는지 **진 달까지 그대로** 보여 줍니다.

서비스 정의는 [SERVICE.md](SERVICE.md), 작업 규칙은 [CLAUDE.md](CLAUDE.md), 진행 기록은 [TASKS.md](TASKS.md) 에 있습니다.

---

## 1. 구조

```
POST /query {question}
   │
   ▼
입력 가드레일 (guardrails.py) ── 등록 외 종목·레버리지·인버스·옵션 / 계좌번호 등 개인 금융정보 / 프롬프트 인젝션 → 도구 없이 거절
   │
   ▼
LangGraph 에이전트 (agent.py, create_agent + Bedrock Claude)
   │  도구 호출 (tools.py)
   ├─ get_market_signals     어젯밤 S&P 500·VIX 신호 점수, 데이터 신선도   ← market.py (yfinance, NYSE 달력)
   ├─ get_budget_status      이번 달·올해 예산, 남은 거래 주, 이번 주 매수 여부 ← ledger.py (SQLite)
   ├─ get_allocation_gap     ETF 별 현재 비중 vs 목표 비중                   ← engine.py
   ├─ propose_order          BUY/WAIT·금액·ETF·수량 (규칙 엔진)             ← engine.py  ──► BUY 면 승인 대기
   ├─ retrieve_docs          투자 원칙·ETF 설명·적립식 연구 문서 검색          ← retriever.py (FAISS + BM25)
   ├─ record_buy             체결을 장부에 기록                              ← ledger.py  ──► 항상 승인 대기
   └─ compare_with_first_day "1일 일괄 매수" 대비 성적                        ← engine.py
   │
   ▼
출력 검사 (guardrails.py) ── 답의 숫자가 도구 결과와 1% 안에서 일치하는지, 수익 보장 표현이 없는지
   │                          실패 시 1회 다시 쓰게 하고, 그래도 실패면 도구 결과로 코드가 답을 만든다
   ▼
{answer, contexts, trace}   (+ Langfuse 트레이스)
```

| 파일 | 역할 |
|---|---|
| [src/agent.py](src/agent.py) | 에이전트 그래프, 시스템 프롬프트, 승인(HITL) 흐름, 출력 검사 연결, Langfuse |
| [src/tools.py](src/tools.py) | 도메인 도구 7개 (계산은 하지 않고 engine·market·ledger·retriever 를 연결) |
| [src/engine.py](src/engine.py) | 예산·주문 규칙, 비중, 1일 매수 비교 (순수 함수, LLM 호출 없음) |
| [src/market.py](src/market.py) | yfinance 조회, 신호 점수 계산, NYSE 거래일·신선도 판정 |
| [src/ledger.py](src/ledger.py) | SQLite 매수 장부 (budget_month 기준), 보유 수량 |
| [src/retriever.py](src/retriever.py) | 문서 청크 분할, Bedrock 임베딩 FAISS + BM25 하이브리드 검색 (RRF) |
| [src/guardrails.py](src/guardrails.py) | 입력 필터, 출력 검사 |
| [src/api.py](src/api.py) | FastAPI `POST /query` (API), `GET /` (웹 화면) |
| [src/dashboard.py](src/dashboard.py) | 대시보드 숫자 묶기 (도구와 같은 엔진·장부 함수, LLM 호출 없음). 목표 금액과의 차이(`gap_usd`), ETF 가격 기준일 확인(`price_check`) |
| [src/settings.py](src/settings.py) | 사용자 초기 설정(월·연 예산, ETF·목표 비중, 초기 보유 수량) 검사·저장 |
| [src/static/index.html](src/static/index.html) | 사용자 대시보드 화면 |
| [config.yaml](config.yaml) | 규칙 숫자 (예산, ETF·목표 비중, 신호 임계값, 배수, RAG·가드레일 설정) |
| [data/](data/) | RAG 문서 3개 (`docs/`: ETF 팩트시트 요약 VOO·QQQ·SCHD, 내 투자 원칙, 적립식 vs 저점 매수 연구 요약 — AQR "Hold the Dip"·AAII 매수일 연구 등 출처 표기), 더미 장부 CSV, `ledger.db` |
| [evaluation/](evaluation/) | 평가 세트, 평가 실행·리포트, 백테스트 |
| [tests/](tests/) | 규칙 엔진·거래 주·승인 판정·가드레일 테스트 (pytest) |

**기술 스택**

| 영역 | 사용 |
|---|---|
| 에이전트 | Python, LangChain `create_agent` + LangGraph (`interrupt()`, `InMemorySaver`), `ModelFallbackMiddleware` |
| 모델 | Amazon Bedrock `ChatBedrockConverse` (기본 Sonnet 4.6, 실패 시 Haiku 4.5 자동 전환), 임베딩 Titan v2 |
| API | FastAPI `POST /query` 하나 |
| 시장 데이터 | yfinance (^GSPC·^VIX·ETF 일봉), pandas_market_calendars (`XNYS` 거래일·휴장일) |
| 장부 | sqlite3 (표준 라이브러리) |
| RAG | FAISS 벡터 검색 + BM25(rank-bm25) 하이브리드, RRF 결합 |
| 관측·평가 | Langfuse (LangChain CallbackHandler), RAGAS + LLM-as-Judge, pytest |

---

## 2. 설치

Python 3.14 에서 확인했습니다.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

프로젝트 루트에 `.env` 를 만들고 아래 변수를 채웁니다. (값은 커밋하지 않습니다)

| 변수 | 내용 |
|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Bedrock 호출용 AWS 키 |
| `AWS_DEFAULT_REGION` | `us-east-1` |
| `BEDROCK_MODEL_ID` | 기본 모델 (예: `global.anthropic.claude-sonnet-4-6`) |
| `BEDROCK_FALLBACK_MODEL_ID` | 기본 모델이 한도 초과 등으로 실패할 때 자동 전환할 모델 (예: Haiku 4.5) |
| `BEDROCK_EMBED_MODEL_ID` | RAG 임베딩 (예: `amazon.titan-embed-text-v2:0`) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | 트레이스 (없으면 트레이스 없이 동작) |
| `LANGFUSE_TRACING_ENVIRONMENT` | 예: `development` |
| `YF_CA_BUNDLE` | (선택) yfinance 인증서 경로. 프로젝트 경로에 한글이 있어 curl 오류가 나면 영문 경로에 `cacert.pem` 을 복사해 지정 |

데모용 더미 장부를 적재합니다. (2026년 1~11월 매수 22건, 가격은 실제 종가)

```bash
python -m src.ledger --load-dummy
```

---

## 3. 실행

**CLI**

```bash
python -m src.agent "오늘 사야 해?"
python -m src.market          # 어젯밤 신호 점수만 보기
python -m src.retriever "하락한 날 더 사는 이유"
```

**웹 화면 (사용자용)**

```bash
uvicorn src.api:app --port 8000
```

브라우저에서 **http://127.0.0.1:8000** 을 엽니다.

한 화면에서 아침 확인이 끝나도록 중요한 순서대로 배치했습니다. 주황 헤더 위로 흰 카드가 겹치는 금융 앱 형태입니다.

| 영역 | 내용 |
|---|---|
| 상단 (주황 헤더) | 날짜·매수일, 데이터 기준일과 신선도(fresh ✓ / ⚠), 새로고침, ⚙ 초기 설정 |
| 왼쪽: 오늘의 추천 + 추천 근거 (한 카드) | 위: **BUY · 금액** 또는 **WAIT**, 기본 페이스 × 배수, ETF·수량, 기록 버튼. 아래: 신호 4개 타일(값·점수·기준), 점수 합계와 배수 구간, 가장 모자란 ETF, 규칙 안내 |
| 오른쪽: 이번 달 예산 | 사용·남은 금액 막대, 남은 거래일, 월말 전액 매수일, 연간 사용액 |
| 오른쪽: 비중 | 현재/목표 비중 띠 2줄, ETF 한 줄 목록(모자란 순, 현재 → 목표, 차이 %p, 목표 금액까지 약 $ 부족·초과) |
| 매수 기록 / 1일 매수 비교 | 이번 달 장부, 최근 6개월 차이 막대 차트와 표(진 달 포함), 올해 합계, 2015~2025 백테스트 |
| 에이전트에게 물어보기 | 규칙 이유·배당·전망 같은 자유 질문 (RAG·가드레일 그대로) |

- **⚙ 초기 설정**: 월·연 예산, 보유 ETF·목표 비중(합계 100%), 초기 보유 수량을 직접 입력합니다. 저장 전에 파이썬 규칙으로 검사합니다 (연 예산 ≥ 월 예산, 미국 상장 코드만, 레버리지·인버스 제외, 새 ETF 는 가격 확인). 예산·비중은 config.yaml 에, 초기 보유는 장부에 쓰고, 이전 config.yaml 은 config.yaml.bak 으로 백업합니다.
- **✔ 샀어요 / ✎ 금액 수정 후 기록** → 장부 기록 요청 → 승인 / 거절 버튼으로 확정 (승인 전에는 장부에 쓰지 않음). 입력칸은 취소 버튼·Esc·기록 버튼 다시 누르기로 접습니다. **오늘은 패스**는 이 브라우저에만 기억합니다.
- 금액·예산·보유 수량 입력칸은 천 단위 쉼표로 보이고, 서버에는 쉼표를 뺀 숫자로 보냅니다.
- **ETF 가격 경고**: 비중·금액·추천 수량에 쓴 ETF 가격이 직전 거래일 종가가 아니면 비중 카드(그리고 BUY 일 때 추천 카드)에 경고를 띄웁니다. 판정은 파이썬 `dashboard.price_check` 입니다.
- 화면 숫자는 `GET /api/dashboard` 가 엔진·장부 함수로 바로 만듭니다 (LLM 호출 없음, 첫 로딩 약 7초 · 이후 약 3초). 질문·승인·기록은 `POST /query` 로 에이전트를 거칩니다.
- 모바일 화면과 다크 모드를 지원합니다. 외부 라이브러리 없이 HTML 한 장입니다.

**API**

```bash
uvicorn src.api:app --port 8000
```

```bash
# q.json 을 UTF-8 로 저장해 보냅니다 (Windows 셸에서 한글을 직접 넣으면 인코딩이 깨질 수 있음)
# q.json 내용: {"question": "오늘 사야 해?"}
curl -X POST http://127.0.0.1:8000/query -H "Content-Type: application/json; charset=utf-8" --data-binary @q.json
```

또는 브라우저에서 `http://127.0.0.1:8000/docs` 를 열어 `POST /query` 를 바로 실행할 수 있습니다.

응답 형식:

```json
{
  "answer": "BUY 추천: QQQ 1주 (예상 $659.51, 1주 $659.51) ...",
  "contexts": [{"doc_id": "investment_rules#5", "text": "..."}],
  "trace": [{"step": "get_market_signals", "input": {}, "output": {"score": 2, "...": "..."}}, "..."]
}
```

- `contexts` 는 `retrieve_docs` 가 찾은 문서 청크입니다. 검색하지 않은 질문은 빈 배열입니다.
- `trace` 에는 도구 호출이 한 단계씩, 그리고 `input_guardrail`, `approval_request`, `approval`, `output_check` 단계가 남습니다.

**승인 흐름 (같은 서버에 이어서 질문)**

| 질문 | 답 |
|---|---|
| "오늘 매수 추천 나오면 내가 확인한 다음에 확정해줘" | BUY 추천 + 신호 근거 + "'승인' 또는 '거절'로 답해 주세요" |
| "승인" | "승인되어 확정했습니다: BUY QQQ 1주 …" (주문은 직접) |
| "VOO 1주 $671.31 기록해줘" | 장부 기록 요청 (체결일·예산 귀속 월·주의 사항) |
| "승인" / "거절" | 장부 22건 → 23건 / "기록하지 않았습니다" |

승인·거절 판정은 LLM 이 아니라 단어 규칙으로 합니다. 승인 대기는 사용자 1명 전제로 서버 프로세스에 하나만 둡니다.

---

## 4. 규칙 요약

숫자는 모두 [config.yaml](config.yaml) 에서 읽습니다.

- 예산: 월 $1,500, 연 $18,000 (USD, 미국 직투 계좌 하나). 연 예산이 부족하면 그달 예산을 줄입니다. KRW 와 국내 상장 ETF 는 다루지 않습니다.
- 등록 ETF·목표 비중: 현재 VOO 50%, QQQ 30%, SCHD 10%, QQQM 10% (화면 ⚙ 설정에서 바꿈). 가장 모자란 ETF 를 삽니다. 아래 평가·백테스트는 바꾸기 전 설정(VOO 60%, QQQ 30%, SCHD 10%)으로 돌린 결과입니다.
- 신호 점수 (어젯밤 기준): S&P 500 하루 −1%/−2%, 20일 고점의 95% 이하, 하락일 거래량 1.5배, VIX 20/30.
- 매수 시점: 주 1회. 점수 2 이상이면 그날 (배수 1.5 / 4 이상 2.0), 아니면 주 마지막 거래일 (배수 1.0).
- 월 마지막 거래일에는 남은 예산 전액을 삽니다 (주 1회 제한보다 우선).
- 주는 ISO 주 중 그달에 속한 부분, 장부는 체결일이 아니라 `budget_month` 로 묶습니다.
- 시장 데이터가 직전 NYSE 거래일 기준이 아니면 신호 기반 추천을 하지 않습니다 (월말은 금액만 안내).
- 정수 주로 내림하되, 1주 가격이 남은 월 예산 이하이면 최소 1주를 삽니다.
- ETF 가격이 직전 거래일 종가가 아니면 화면에 경고하고, 가격을 캐시에 굳히지 않아 다음 새로고침에서 다시 받습니다 (추천 계산 규칙 자체는 그대로).

---

## 5. 테스트와 평가

```bash
pytest                                    # 124개: 규칙 엔진, 거래 주, 승인 판정, 가드레일, 초기 설정
python -m evaluation.run_eval --round 2   # 13건 실행 + LLM-as-Judge + RAGAS → evaluation/round2_report.md
python -m evaluation.backtest             # 2015~2025 백테스트 → evaluation/backtest_report.md
```

**단위 테스트** (LLM·네트워크 호출 없이 규칙만 검증)

| 파일 | 개수 | 내용 |
|---|---|---|
| [tests/test_engine.py](tests/test_engine.py) | 30 | 점수별 배수, 주 1회 제한, 월말 전액 매수, 연 예산 축소, 데이터 STALE, 최소 1주, 비중 계산, 1일 매수 비교 |
| [tests/test_weeks.py](tests/test_weeks.py) | 10 | NYSE 거래 주 계산 (하루짜리 주, 추수감사절·메모리얼데이 휴장), 남은 예산 |
| [tests/test_approval.py](tests/test_approval.py) | 31 | '승인'·'거절'·무관 판정 ("예산"의 '예'는 승인 아님), 대기 없는 승인 응답 |
| [tests/test_settings.py](tests/test_settings.py) | 12 | 초기 설정 검사 (예산, 비중 합계, 국내 코드·레버리지·중복·음수 거부, 새 ETF 가격 확인), config.yaml 은 해당 값만 바뀌고 주석 유지 |
| [tests/test_guardrails.py](tests/test_guardrails.py) | 41 | 입력: 평가 세트 11·12번 차단, 나머지 질문 오탐 없음, 레버리지·등록 외 종목·인젝션·개인 금융정보 차단, 계좌번호 마스킹 / 출력: 틀린 금액·지어낸 합계·수익 보장 표현 검출, 1% 반올림 허용, 부정 표현 허용 |

**평가** ([evaluation/test_queries.csv](evaluation/test_queries.csv) 13건: positive 6 · negative 2 · edge 3 · guardrail 2)

- 통과 기준: Judge 가 expected_traits 를 모두 충족, forbidden 위반 없음, expected_tools 를 모두 호출.
- edge 케이스(월말+데이터 오류, 이미 산 주, 예산 소진)와 장부 기록 케이스는 평가 중에만 임시 장부·고정 신호로 상황을 만듭니다. 실제 장부는 바뀌지 않습니다.
- 에이전트·Judge 는 Sonnet 4.6, RAGAS 채점은 Haiku 4.5.

| | 1차 | 2차 (최종) | 목표 |
|---|---|---|---|
| 통과 | 6 / 12 | **13 / 13** | 약 83% 이상 (13건 중 11건) |
| faithfulness | 0.572 | **0.880** | ≥ 0.80 |
| context_recall | 0.479 | **0.833** | ≥ 0.70 |
| context_precision | 0.266 | 0.712 | - |
| answer_relevancy | 0.318 | 0.205 | - |

1차 → 2차 주요 수정: 승인 요청에 신호 근거 추가, 승인 대기 중 trace 누락 버그 수정, 예측·배당 질문의 도구 호출, 사용자 말과 장부가 다를 때 밝히기, WAIT 의 규칙 안내(rule_notes)를 코드로 생성, 비중 차이 부호 명확화, 매수 판단 시 도구 4개 필수 호출, 장부 기록 케이스(13번) 추가, RAG 응답 단축(인덱스 사전 생성·동시 검색·짧은 답, 4번 19.3초 → 8.6초).
상세는 [round1_report.md](evaluation/round1_report.md), [round2_report.md](evaluation/round2_report.md).
1차는 12건(13번 장부 기록 케이스 추가 전)이고, 1차 RAGAS 점수는 2차와 같은 기준(Haiku 4.5 채점, reference 열 사용)으로 다시 채점한 값입니다.

**SERVICE.md 성공 기준 대비** (2차 평가·백테스트 기준)

| 성공 기준 | 결과 | |
|---|---|---|
| 인-아웃 세트 약 83% 이상 통과 (13건 중 11건) | 13 / 13 | ✅ |
| 예산 초과 추천 · 등록 외 종목 추천 · 승인 없는 장부 기록 0건 | 0건 (Judge forbidden 위반 0건, 가드레일 11·12번 차단, 장부 기록은 승인 후에만) | ✅ |
| 답변 금액·수치가 도구 결과와 일치 (불일치 0건) | 0건 (LLM 이 쓴 답 7건 모두 출력 검사 1회에 통과, 나머지 6건은 코드가 만든 승인·거절·기록 문구) | ✅ |
| RAGAS faithfulness ≥ 0.80, context_recall ≥ 0.70 | 0.880, 0.833 | ✅ |
| 2015~2025 백테스트로 평균 단가 차이·이긴 달·진 달 수 제시 | 평균 단가 −0.10%, 이긴 달 53 · 진 달 79 | ✅ |
| 진 달도 숨기지 않는다 | 백테스트·`compare_with_first_day` 모두 LOSE 달 표시 | ✅ |
| 아침 질문 1회에 15초 안에 답 | "오늘 사야 해?" 7.4초, RAG 질문 4번 8.6초 (상황 설명이 긴 edge 9번은 19.1초) | ✅ |

**백테스트** (2015-01 ~ 2025-12, 실서비스와 같은 신호·엔진 함수로 하루씩 진행)

- 132개월: **이긴 달 53 · 진 달 79**
- 금액 가중 평균 단가 차이 −0.10% (규칙이 조금 더 싸게 삼) · 투자 $183,006.30 (예산의 92.4%, 나머지는 정수 주 잔돈) · 매수 585회 (1.0배 351, 1.5배 127, 2.0배 33, 월말 74)
- 2025-12-31 평가액: 규칙 $425,592.72 vs 1일 일괄 매수 $424,587.39 → **+$1,005.33 (+0.24%)**
- 보통 달은 1일 매수가 조금씩 이기고, 급락한 달(2020-03 +17.7%, 2018-12 +10.3%)에 규칙이 크게 이깁니다. 효과는 작습니다.
- 배당·수수료·세금·환율은 넣지 않았습니다. 상세는 [backtest_report.md](evaluation/backtest_report.md).

---

## 6. 한계와 주의

- 투자 자문이 아닙니다. 실제 주문은 사용자가 증권사 앱에서 직접 합니다.
- 장중 조회 시 거래량은 미완성 봉일 수 있어, 신호는 직전 **완료된** 거래일 기준으로만 계산합니다.
- 승인 대기는 메모리에 있어 서버를 다시 켜면 사라집니다 (사용자 1명 전제).
- 출력 검사는 숫자 대조라, 숫자 없는 설명 오류는 잡지 못합니다.
- yfinance(야후)가 직전 거래일 ETF 종가를 비워(NaN) 보내는 때가 있습니다. 그동안은 그 전 종가로 계산되며, 화면 경고로만 알립니다.
- 평가 결과(13/13 등)는 설정 변경(QQQM 추가)과 실제 매수 기록 1건 추가(장부 23건) 전에 잰 값입니다.
- 평가 점수는 LLM 채점이라 실행마다 조금씩 달라집니다.
- Bedrock 일일 토큰 한도에 걸리면 `BEDROCK_FALLBACK_MODEL_ID` 로 자동 전환합니다. 평가만 다시 채점할 때는 `python -m evaluation.run_eval --round 2 --ragas-only --ragas-model <모델 ID>` 를 씁니다.

---

## 7. 진행 상황

[TASKS.md](TASKS.md) 의 작업 20개(T1~T17, H1~H3)가 모두 끝났습니다. 작업마다 "확인" 방법으로 검증한 기록이 TASKS.md 에 있습니다.

| 묶음 | 작업 | 내용 | 상태 |
|---|---|---|---|
| 1 최소 경로 | T1~T6 | 설정, Bedrock 연결, 신호 점수, 규칙 엔진, 에이전트, `POST /query` | ✅ |
| 2 장부·예산 | T7~T11 | SQLite 장부, 거래 주 계산, 주 1회·월말·연 예산 규칙, 예산·비중 도구, BUY 승인(HITL) | ✅ |
| 3 RAG | T12~T13 | FAISS + BM25 하이브리드 검색, `retrieve_docs`, 응답 contexts | ✅ |
| 4 가드레일 | T14~T15 | 입력 필터, 출력 숫자·수익 보장 표현 검사 | ✅ |
| 5 평가 | T16~T17 | 1차·2차 평가 (RAGAS + LLM-as-Judge) | ✅ |
| 보류였던 작업 | H1 | `record_buy`: 승인 뒤에만 장부 기록 | ✅ |
| | H2 | `compare_with_first_day`: 월별 1일 일괄 매수 비교 | ✅ |
| | H3 | 2015~2025 백테스트 리포트 | ✅ |

작업 목록 이후 추가로 한 일: 평가 13번(장부 기록) 케이스 추가와 통과 목표를 약 83%(13건 중 11건)로 조정, 대기 중인 요청 없이 '승인'·'거절'만 오면 바로 안내, 가드레일 단위 테스트 41개, RAG 응답 단축(4번 19.3초 → 8.6초). 최신 2차 평가는 13/13 통과입니다.

사용자 화면(사용자 요청으로 추가): 웹 대시보드(`GET /`, `GET /api/dashboard`), 초기 설정 입력(`POST /api/settings`, 단위 테스트 12개), 주황 헤더 테마, 추천·근거 한 카드와 신호 타일, 비중 띠·목록과 목표 금액 차이, ETF 가격 기준일 경고와 재조회, 기록 입력 취소, 입력칸 천 단위 쉼표.
