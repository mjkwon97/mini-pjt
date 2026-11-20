# TASKS

규칙: 한 번에 작업 하나만 한다. 끝나면 [x] 로 바꾸고 "확인"에 적힌 방법으로 검증한다.
기준 문서: SERVICE.md, CLAUDE.md. 숫자는 config.yaml, 비밀 값은 .env 에서 읽는다.

## 묶음 1 · 최소 경로 ("오늘 사야 해?" → 답변 1건)
범위: 장부·RAG·가드레일 없음. 예산은 config.yaml 값을 "이번 달 남은 예산 전액"으로 가정한다. contexts 는 빈 배열로 둔다.

- [x] T1. 환경과 설정 파일
  - requirements.txt, config.yaml(월·연 예산, 등록 ETF, 목표 비중, 신호 임계값, 배수), .env 에 AWS·Langfuse 키
  - 확인: `python -c` 로 config.yaml 과 .env 값을 읽어 출력하면 키가 모두 보인다.
- [x] T2. Bedrock 연결 확인
  - ChatBedrockConverse 를 BEDROCK_MODEL_ID 로 만들어 한 문장 질문
  - 확인: 터미널에 모델 답이 나오고, Langfuse 프로젝트에 트레이스 1건이 보인다.
  - 진행: 모델 답 확인 완료 (global.anthropic.claude-sonnet-4-6, us-east-1). Langfuse(jp.cloud) 트레이스 answer-query 확인 완료.
- [x] T3. market.py 최소판: 어젯밤 신호 점수
  - yfinance 로 ^GSPC·^VIX 일봉을 받아 등락률·20일 고점 대비·거래량 배수·VIX 점수 계산, XNYS 직전 거래일 기준 신선도 판정
  - 확인: `python -m src.market` 이 점수·항목별 근거·데이터 기준일·신선도(OK/STALE)를 출력한다.
- [x] T4. engine.py 최소판: propose_order
  - 입력(신호 점수, 남은 예산, 남은 주 수, 오늘이 주 마지막 거래일인지, 목표 비중) → BUY/WAIT, 배수, 금액(USD), ETF. 순수 함수
  - 확인: `pytest tests/` 에서 점수 0·2·4 와 STALE 데이터 케이스 4개가 통과한다.
- [x] T5. tools.py + agent.py: 도구 2개로 에이전트 1회 실행
  - get_market_signals, propose_order 를 tool 로 감싸고 create_agent 로 연결, 시스템 프롬프트에 "숫자는 도구 결과만 쓴다"
  - 확인: `python -m src.agent "오늘 사야 해?"` 답의 금액·ETF 가 propose_order 출력과 같다.
- [x] T6. api.py: POST /query
  - question 을 받아 {answer, contexts: [], trace: [{step,input,output}]} 반환, 도구 호출을 trace 에 한 단계씩 기록
  - 확인: uvicorn 실행 후 curl 로 질문하면 세 키가 오고 trace 에 도구 2단계가 들어 있다.

## 묶음 2 · 장부(읽기)와 예산 규칙
- [x] T7. ledger.py: SQLite 장부 스키마(budget_month 포함)와 더미 데이터 적재, 읽기 함수만
  - 확인: 더미 적재 후 월별 합계 조회 결과가 더미 값의 합과 같다.
- [x] T8. 거래 주·남은 예산 계산 (ISO 주 중 그달 부분, XNYS)
  - 확인: pytest 로 하루짜리 주가 있는 달과 휴장일이 낀 달의 주 수가 맞다.
- [x] T9. engine 규칙 완성: 주 1회 제한, 월말 전액 매수, 연 예산 초과 시 월 예산 축소
  - 확인: pytest 로 "이미 산 주 + VIX 35 → WAIT", "월말 → 잔액 전액", "연 예산 부족 → 축소" 케이스가 통과한다.
- [x] T10. get_budget_status, get_allocation_gap 도구 추가와 propose_order 연결
  - 확인: /query "이번 달 예산 얼마 남았어?" 답의 금액이 장부 계산값과 같고, "비중이 목표랑 얼마나 달라?" 답의 비중이 도구 출력과 같다.
- [x] T11. BUY 추천 확정 전 interrupt() 승인 (HITL)
  - propose_order 결과가 BUY 이면 그래프를 멈추고 승인을 기다린다. 승인하면 확정, 거절하면 취소 안내. WAIT 은 바로 답한다.
  - 확인: test_queries 3번 질문에서 승인 전에는 확정 문구가 없고, 승인/거절 응답에 따라 답이 갈린다.

## 묶음 3 · RAG
- [x] T12. retriever.py: data/docs 3개 청크 분할, Bedrock 임베딩 FAISS + BM25 하이브리드
  - 확인: "하락한 날 더 사는 이유" 검색 상위 3개에 investment_rules 나 dca_research 가 있다.
  - 진행: 23청크, Titan v2 임베딩 + BM25, RRF 결합. 상위 3개 = investment_rules#5, dca_research#4, dca_research#2.
- [x] T13. retrieve_docs 도구 연결, 응답 contexts 채우기
  - 확인: /query 답의 contexts 에 doc_id 와 text 가 들어 있고 trace 에 retrieve_docs 단계가 있다.
  - 진행: 4번 질문 → trace [retrieve_docs], contexts = dca_research#2, investment_rules#5, dca_research#1.

## 묶음 4 · 가드레일
- [x] T14. guardrails.py 입력 필터: 등록 외 종목·레버리지, 계좌번호 등 개인 금융정보, 프롬프트 인젝션
  - 확인: test_queries 의 guardrail 2건(11·12번)이 도구 호출 없이 거절된다.
  - 진행: 11번 [ticker, injection], 12번 [pii, injection] 거절, trace 는 input_guardrail 1단계뿐. 1~10번은 통과(오탐 0).
- [x] T15. 출력 검사: 답의 숫자가 도구 결과와 1% 안에서 일치하는지, 수익 보장 표현이 없는지
  - 확인: 일부러 틀린 숫자를 넣은 답이 검사에서 걸러진다.
  - 진행: $695.51(실제 $659.51)·지어낸 합계·"반드시 오릅니다"·"원금 보장" 걸러짐. 실제 답 5건 오탐 0. 실패 시 1회 재작성 → 그래도 실패면 코드 답.

## 묶음 5 · 평가
- [x] T16. 1차 평가: test_queries.csv 12건 실행, RAGAS + LLM-as-Judge, round1_report.md
  - 확인: evaluation/round1_report.md 에 12건 통과 여부와 RAGAS 4개 점수가 있다.
  - 진행: evaluation/run_eval.py 로 실행. 통과 6/12, faithfulness 0.522, answer_relevancy 0.393, context_precision 0.607, context_recall 0.487.
- [x] T17. 실패 케이스 수정 후 2차 평가, round2_report.md
  - 확인: 12건 중 10건 이상 통과, faithfulness ≥ 0.80, context_recall ≥ 0.70.
  - 진행: 통과 12/12 (4번 안정화 프롬프트 추가 후). RAGAS(Haiku 4.5 채점, 1·2차 동일 기준) faithfulness 0.572→0.842, context_recall 0.479→0.750.
  - 재실행(H1~H3 반영, 실제 종가 장부): 통과 9/12 (1·5·10번 실패), faithfulness 0.802, context_recall 0.767. 목표 미달 → 수정 필요.
  - 수정 후 재실행(매수 판단 시 도구 4개 필수, 비중 초과/부족 표기, WAIT rule_notes): 통과 11/12, faithfulness 0.867, context_recall 0.800. 목표 충족. 10번은 propose_order 미호출.
  - 10번 안정화 프롬프트 후: 통과 12/12, faithfulness 0.906, context_recall 0.850.
  - 대기 없는 승인 응답 추가 후 재실행: 통과 12/12, faithfulness 0.877, context_recall 0.850.
  - 13번(장부 기록) 추가, 목표 13건 중 11건(약 83%): 통과 13/13, faithfulness 0.911, context_recall 0.864.
  - 재실행(코드 변경 없음): 통과 13/13, faithfulness 0.930, context_recall 0.864.
  - RAG 응답 단축(인덱스 사전 생성·동시 검색·짧은 답) 후: 통과 13/13, faithfulness 0.880, context_recall 0.833. 4번 19.3초 → 8.6초. (현재 최종 결과)

## 시간이 부족할 때 자르는 순서
T15 출력 검사 → T8 의 휴장일 세부 케이스. 묶음 1, T11(HITL), T12~T13(RAG), T14, T16~T17 은 자르지 않는다.

## 개발 보류 (시간이 남으면 진행)
- [x] H1. record_buy: 체결을 장부에 쓰기, interrupt() 승인 후에만 실행
  - 확인: "VOO 3주 $1,512 기록해줘" → 승인 전에는 장부 행 수가 그대로이고, 승인 후 1행 늘어난다.
  - 진행: 임시 장부로 확인. 요청 22건(대기) → 승인 23건, 거절 시 그대로. 값은 사용자 메시지와 파이썬 대조.
- [x] H2. compare_with_first_day: 월별 "1일 일괄 매수" 비교 (평균 단가·평가액)
  - 확인: /query "지난달 1일 매수랑 비교해줘" 답에 두 평균 단가와 차이가 나온다.
  - 진행: 10월 WIN +$27.39 (QQQ $690.49 vs $742.03 −6.95% 등). 올해 3승 7패, −$144.15. 더미 장부를 실제 종가로 재생성.
- [x] H3. 2015~2025 백테스트 리포트 (이긴 달·진 달 수)
  - 확인: 스크립트 실행 시 이긴 달 + 진 달 = 전체 개월 수가 나온다.
  - 진행: python -m evaluation.backtest → 132개월: 이긴 달 53 + 진 달 79 + 같음 0 = 132. 평가액 +0.24%, 평균 단가 −0.10%. evaluation/backtest_report.md
