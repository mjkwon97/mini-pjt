"""RAG 파이프라인: data/docs 문서를 청크로 나누고 FAISS(Bedrock 임베딩) + BM25 하이브리드로 검색한다.

두 검색의 순위를 Reciprocal Rank Fusion(RRF)으로 합친다. 점수 척도가 달라도 순위만 쓰므로 정규화가 필요 없다.
문서가 작아(청크 수십 개) 인덱스는 프로세스 메모리에 한 번만 만든다.
"""

from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import faiss
import numpy as np
from dotenv import load_dotenv
from langchain_aws import BedrockEmbeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi

from src.market import load_config

load_dotenv()

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_HANGUL = re.compile(r"[가-힣]+")
_WORD = re.compile(r"[가-힣]+|[A-Za-z]+|\d+(?:\.\d+)?")


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """문서 앞의 --- 블록(doc_id, title, source)을 읽고 본문과 나눈다."""
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def load_chunks(cfg: dict) -> list[dict]:
    """data/docs 의 .md 문서를 ## 섹션 단위로 나누고, 긴 섹션은 다시 잘라 청크 목록을 만든다.

    청크: {"doc_id": "investment_rules#3", "source_doc": "investment_rules", "section": "...", "text": "..."}
    """
    rag = cfg["rag"]
    header_split = MarkdownHeaderTextSplitter([("#", "h1"), ("##", "h2")], strip_headers=False)
    size_split = RecursiveCharacterTextSplitter(chunk_size=rag["chunk_size"], chunk_overlap=rag["chunk_overlap"])
    chunks = []
    for path in sorted(Path(rag["docs_dir"]).glob("*.md")):
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
        doc = meta.get("doc_id", path.stem)
        n = 0
        for sec in header_split.split_text(body):
            section = sec.metadata.get("h2") or sec.metadata.get("h1", "")
            for piece in size_split.split_text(sec.page_content):
                n += 1
                chunks.append({"doc_id": f"{doc}#{n}", "source_doc": doc, "section": section, "text": piece})
    return chunks


def tokenize(text: str) -> list[str]:
    """BM25 용 토큰. 한글은 형태소 분석기 없이 단어와 글자 2-gram 을 같이 써서 조사 차이를 흡수한다."""
    tokens = []
    for w in _WORD.findall(text.lower()):
        tokens.append(w)
        if _HANGUL.fullmatch(w) and len(w) > 2:
            tokens.extend(w[i:i + 2] for i in range(len(w) - 1))
    return tokens


def _embeddings() -> BedrockEmbeddings:
    """Bedrock 임베딩 모델 (.env 의 BEDROCK_EMBED_MODEL_ID)."""
    return BedrockEmbeddings(model_id=os.environ["BEDROCK_EMBED_MODEL_ID"],
                             region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))


def _normalize(vectors: list[list[float]]) -> np.ndarray:
    """코사인 유사도를 내적으로 계산하도록 L2 정규화한다."""
    arr = np.asarray(vectors, dtype="float32")
    faiss.normalize_L2(arr)
    return arr


def warm_up() -> None:
    """인덱스를 미리 만든다. 서버·에이전트 시작 때 불러 첫 RAG 질문이 느려지지 않게 한다."""
    _index()


@lru_cache(maxsize=1)
def _index() -> tuple:
    """청크·FAISS 인덱스·BM25 인덱스·임베딩 모델을 한 번만 만든다."""
    cfg = load_config()
    chunks = load_chunks(cfg)
    emb = _embeddings()
    texts = [f"{c['section']}\n{c['text']}" for c in chunks]
    with ThreadPoolExecutor(max_workers=8) as pool:  # Titan 은 한 번에 한 문장씩 받으므로 병렬로 보낸다
        vecs = _normalize(list(pool.map(emb.embed_query, texts)))
    index = faiss.IndexFlatIP(vecs.shape[1])
    index.add(vecs)
    bm25 = BM25Okapi([tokenize(f"{c['section']} {c['text']}") for c in chunks])
    return chunks, index, bm25, emb, cfg["rag"]


def rrf_fuse(rankings: list[tuple[list[int], float]], k: int) -> dict[int, float]:
    """여러 검색의 순위 목록을 Reciprocal Rank Fusion 으로 합친다. (순수 함수)

    rankings: [(청크 번호 순위 목록, 가중치), ...] → {청크 번호: 합산 점수}
    """
    scores: dict[int, float] = {}
    for ranked, weight in rankings:
        for rank, i in enumerate(ranked):
            scores[i] = scores.get(i, 0.0) + weight / (k + rank + 1)
    return scores


def search(query: str, top_k: int | None = None) -> list[dict]:
    """질문과 가까운 문서 청크를 하이브리드로 검색해 상위 top_k 개를 돌려준다.

    결과: [{"doc_id", "source_doc", "section", "text", "score", "vector_rank", "bm25_rank"}]
    """
    chunks, index, bm25, emb, rag = _index()
    top_k = top_k or rag["top_k"]
    n = min(rag["candidates"], len(chunks))

    qv = _normalize([emb.embed_query(query)])
    _, ids = index.search(qv, n)
    vec_ranked = [int(i) for i in ids[0] if i >= 0]

    bm_scores = bm25.get_scores(tokenize(query))
    bm_ranked = [int(i) for i in np.argsort(-bm_scores)[:n] if bm_scores[i] > 0]

    fused = rrf_fuse([(vec_ranked, rag["weight_vector"]), (bm_ranked, rag["weight_bm25"])], rag["rrf_k"])
    results = []
    for i, score in sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]:
        results.append({
            **chunks[i],
            "score": round(score, 5),
            "vector_rank": vec_ranked.index(i) + 1 if i in vec_ranked else None,
            "bm25_rank": bm_ranked.index(i) + 1 if i in bm_ranked else None,
        })
    return results


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "하락한 날 더 사는 이유"
    for r in search(q):
        print(f"[{r['doc_id']}] {r['section']}  score={r['score']} vec={r['vector_rank']} bm25={r['bm25_rank']}")
        print("   ", r["text"][:120].replace("\n", " "))
