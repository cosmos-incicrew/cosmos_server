"""③ 질의 임베딩 — 검색 질의를 gemini-embedding-001 벡터로 변환한다.

코퍼스(rec_cases·rec_efficacy 의 embedding)를 채운 규칙과 반드시 같아야(모델·차원·
정규화) 코사인 검색이 맞는다. 이 모듈이 그 규칙의 단일 정의처다 — 코퍼스 적재
(scripts/backfill_rec_embeddings.py)도 이 모듈을 재사용한다. 질의는 RETRIEVAL_QUERY,
코퍼스는 RETRIEVAL_DOCUMENT 로 구분한다(비대칭 검색).
"""

import asyncio
import math
import time

from google.genai import types

from app.core.config import Settings, get_settings
from app.core.gemini import get_gemini

# 질의 임베딩 캐시. 워커 프로세스 수명 동안만 살고 재시작하면 비므로 결과 일관성에
# 영향이 없다(같은 텍스트 → 같은 벡터). 1536 float × 512 ≈ 3MB
_QUERY_CACHE: dict[str, list[float]] = {}
_QUERY_CACHE_MAX = 512


def _normalize(values: list[float]) -> list[float]:
    """L2 정규화. output_dimensionality<3072 절단 시 SDK 가 정규화하지 않으므로 직접 한다."""
    norm = math.sqrt(sum(x * x for x in values)) or 1.0
    return [x / norm for x in values]


def _config(settings: Settings, task_type: str) -> types.EmbedContentConfig:
    """질의·코퍼스 공통 임베딩 설정. task_type(QUERY/DOCUMENT) 만 다르다."""
    return types.EmbedContentConfig(
        task_type=task_type,
        output_dimensionality=settings.embedding_dimensions,
    )


def _vector(res: types.EmbedContentResponse) -> list[float]:
    """embed_content 응답에서 정규화된 벡터를 뽑는다. 빈 응답은 예외로 올린다."""
    if not res.embeddings or not res.embeddings[0].values:
        raise ValueError("Gemini embed_content returned no embedding values")
    return _normalize(list(res.embeddings[0].values))


async def embed_query(text: str) -> list[float]:
    """질의 텍스트를 정규화된 1536차원 벡터로. 빈 문자열은 embed API 가 거부하므로 공백 치환.

    질의 경로는 사용자 대면 지연에 직접 영향 — 재시도를 2회(1회 재시도)로 짧게 잡아
    transient 429 는 흡수하되 지연 폭증은 막는다(backfill 은 5회로 더 끈질기게).

    같은 텍스트는 프로세스 안에서 재사용한다. 임베딩은 결정적이라 캐시가 결과를 바꾸지
    않고, efficacy leg 질의는 `CONCERN_SEARCH_KEYWORDS` 로 만든 **고정 문자열 8개**뿐이라
    사용자가 누구든 같다. 실측 건당 1.6~2.0초가 두 번째 요청부터 0 이 된다.
    """
    cached = _QUERY_CACHE.get(text)
    if cached is not None:
        return cached
    settings = get_settings()
    client = get_gemini()
    for attempt in range(2):
        try:
            res = await client.aio.models.embed_content(
                model=settings.embedding_model,
                contents=text or " ",
                config=_config(settings, "RETRIEVAL_QUERY"),
            )
            vector = _vector(res)
            # 상한을 두는 이유는 cases leg 질의가 프로필마다 달라 무한히 늘기 때문이다.
            # 정작 노리는 efficacy leg 8종은 매 요청 다시 들어와 상한에 밀려도 곧 복귀한다.
            if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
                _QUERY_CACHE.clear()
            _QUERY_CACHE[text] = vector
            return vector
        except Exception:  # noqa: BLE001 — transient 429/일시 오류 1회 재시도
            if attempt == 1:
                raise
            await asyncio.sleep(1)
    raise RuntimeError("unreachable")  # range(2) 는 반드시 return/raise 로 끝난다


def to_pgvector(values: list[float]) -> str:
    """pgvector 입력 리터럴 '[x,y,...]'. RPC 의 vector 인자로 넘긴다."""
    return "[" + ",".join(f"{x:.7f}" for x in values) + "]"


def embed_documents(texts: list[str]) -> list[list[float]]:
    """코퍼스 임베딩 (RETRIEVAL_DOCUMENT, 동기 배치). backfill 전용.

    질의(embed_query)와 동일한 모델·차원·정규화 규칙을 공유해, env 변경 시 코퍼스와
    질의가 어긋나지 않게 한다. 대량(1만+) 이라 스레드풀 병렬, 결과는 원래 순서로.
    """
    from concurrent.futures import ThreadPoolExecutor

    settings = get_settings()
    client = get_gemini()

    def _one(text: str) -> list[float]:
        # backfill 은 1만+ 행을 배치로 계산한다 — 깊은 지점의 429/일시 오류 한 건이
        # 그 leg 전체를 무효화하지 않게 지수 백오프로 흡수한다(DB 쓰기 _update 와 대칭).
        for attempt in range(5):
            try:
                res = client.models.embed_content(
                    model=settings.embedding_model,
                    contents=text or " ",
                    config=_config(settings, "RETRIEVAL_DOCUMENT"),
                )
                return _vector(res)
            except Exception:  # noqa: BLE001 — 429/일시 오류 백오프 재시도
                if attempt == 4:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")  # range(5) 는 반드시 return/raise 로 끝난다

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(_one, texts))
