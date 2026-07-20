"""ingredient_detail service 로직 테스트.

외부 서비스(Supabase·Gemini)는 호출하지 않는다. monkeypatch로 경계를 대체하고
불변식만 검증한다: 근거 없으면 "확인 불가", 안전성 없으면 "안전성 확인 불가",
환각 출처는 제거, 근거 있으면 해설 생성.
"""

from typing import Any

import pytest

from app.modules.ingredient_detail import service


class _FakeQuery:
    """Supabase 쿼리 체이닝(table().select().eq().execute()) 흉내."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def select(self, *_: Any) -> "_FakeQuery":
        return self

    def eq(self, *_: Any) -> "_FakeQuery":
        return self

    async def execute(self) -> Any:
        return type("Result", (), {"data": self._rows})()


class _FakeSupabase:
    """table 이름에 따라 미리 준비한 행을 돌려주는 가짜 클라이언트."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self._tables = tables

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self._tables.get(name, []))


def _patch_supabase(
    monkeypatch: pytest.MonkeyPatch, tables: dict[str, list[dict[str, Any]]]
) -> None:
    async def _fake_get_supabase() -> _FakeSupabase:
        return _FakeSupabase(tables)

    monkeypatch.setattr(service, "get_supabase", _fake_get_supabase)


def _patch_gemini(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Gemini 생성 응답을 고정 텍스트로 대체."""

    class _FakeModels:
        async def generate_content(self, **_: Any) -> Any:
            return type("Resp", (), {"text": text})()

    class _FakeAio:
        models = _FakeModels()

    class _FakeGemini:
        aio = _FakeAio()

    monkeypatch.setattr(service, "get_gemini", lambda: _FakeGemini())
    monkeypatch.setattr(service, "gemini_model_for", lambda complex_query=False: "gemini-flash")

    # langfuse get_client 대체 (트레이싱 부작용 제거)
    class _FakeLangfuse:
        def update_current_generation(self, **_: Any) -> None:
            pass

    monkeypatch.setattr(service, "get_client", lambda: _FakeLangfuse())


async def test_generates_explanation_when_evidence_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 1,
                    "name_kr": "나이아신아마이드",
                    "inci": "NIACINAMIDE",
                    "efficacy": "피부 톤 개선",
                    "safety_note": "자극 낮음",
                    "reference_source": "Niacinamide-0001",
                }
            ],
            "ingredients": [{"origin_definition": "니코틴산 유도체"}],
        },
    )
    _patch_gemini(monkeypatch, "나이아신아마이드는 피부 톤 개선에 도움을 줍니다.")

    result = await service.get_ingredient_detail(1)

    assert result.status == "ok"
    assert result.name == "나이아신아마이드"
    assert result.body is not None
    assert result.safety == "자극 낮음"


async def test_raises_not_found_when_ingredient_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """성분이 DB에 아예 없으면 404 예외(잘못된 요청). '근거 부족'과 구분된다."""
    _patch_supabase(monkeypatch, {"rec_efficacy": [], "ingredients": []})
    _patch_gemini(monkeypatch, "이 텍스트는 나오면 안 됨")

    with pytest.raises(service.IngredientNotFoundError):
        await service.get_ingredient_detail(999)


async def test_marks_safety_unconfirmed_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 2,
                    "name_kr": "가지추출물",
                    "inci": "EGGPLANT",
                    "efficacy": "피부 컨디셔닝",  # 효능 있음 → 해설은 됨
                    # safety_note 없음
                }
            ],
            "ingredients": [],
        },
    )
    _patch_gemini(monkeypatch, "가지추출물은 피부 컨디셔닝에 쓰입니다.")

    result = await service.get_ingredient_detail(2)

    assert result.status == "ok"
    assert result.safety == "안전성 확인 불가"


async def test_removes_hallucinated_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 3,
                    "name_kr": "성분",
                    "inci": "X",
                    "efficacy": "보습",
                    "reference_source": "PubChem",
                }
            ],
            "ingredients": [],
        },
    )
    # 근거에 없는 PMID를 지어낸 응답
    _patch_gemini(monkeypatch, "보습에 좋습니다. PMID: 12345678")

    result = await service.get_ingredient_detail(3)

    assert result.source_verified is False
    assert "PMID" not in (result.body or "")


async def test_generates_product_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 1,
                    "name_kr": "성분A",
                    "inci": "A",
                    "efficacy": "보습",
                    "recommended_skin_types": "건성",
                    "reference_source": "PubChem",
                }
            ],
            "ingredients": [],
        },
    )
    _patch_gemini(monkeypatch, "이 제품은 건성 피부에 적합한 보습 중심 제품입니다.")

    result = await service.get_product_summary([1, 2, 3])

    assert result.status == "ok"
    assert result.summary is not None
    assert len(result.top_ingredients) >= 1


async def test_product_summary_empty_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await service.get_product_summary([])
    assert result.status == "확인 불가"
    assert result.reason == "성분 목록 없음"


async def test_includes_official_restriction_in_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """공식 규제가 있으면 주의사항에 명시된다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 5,
                    "name_kr": "규제성분",
                    "inci": "R",
                    "efficacy": "보존",
                }
            ],
            "ingredients": [],
            "restrictions": [
                {
                    "restriction_id": 10,
                    "regulate_type": "사용 한도",
                    "limit_cond": "0.5% 이하",
                    "provis_atrcl": None,
                    "is_registered_korea": True,
                }
            ],
        },
    )
    _patch_gemini(monkeypatch, "이 성분은 보존 목적으로 쓰이며 사용 한도가 있습니다.")

    result = await service.get_ingredient_detail(5)

    assert result.status == "ok"
    assert result.safety is not None
    assert "공식 규제" in result.safety
    assert "0.5% 이하" in result.safety


async def test_multiple_restrictions_all_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """한 성분에 규제가 여러 건이면 모두 주의사항에 포함된다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 6,
                    "name_kr": "다중규제",
                    "inci": "M",
                    "efficacy": "기능",
                }
            ],
            "ingredients": [],
            "restrictions": [
                {"restriction_id": 1, "regulate_type": "사용 한도", "limit_cond": "1% 이하"},
                {"restriction_id": 2, "regulate_type": "사용 금지", "provis_atrcl": "영유아 제품"},
            ],
        },
    )
    _patch_gemini(monkeypatch, "규제가 있는 성분입니다.")

    result = await service.get_ingredient_detail(6)

    assert result.safety is not None
    assert "1% 이하" in result.safety
    assert "영유아 제품" in result.safety


async def test_safety_unknown_when_no_restriction_and_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """규제도 안전성 문구도 없으면 '안전성 확인 불가'."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 7,
                    "name_kr": "무정보",
                    "inci": "N",
                    "efficacy": "보습",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "보습 성분입니다.")

    result = await service.get_ingredient_detail(7)

    assert result.safety == "안전성 확인 불가"


# ── 게이트 경계: 근거 조합별 판정 ──────────────────────────────


async def test_generates_with_only_origin_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """효능이 없어도 정의(origin_definition)만 있으면 해설을 생성한다.

    효능 컬럼이 아직 적재되지 않은 초기 DB 상태를 모사한다.
    """
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [],
            "ingredients": [
                {
                    "origin_definition": "해조류에서 얻은 추출물",
                    "name_kor": "구멍쇠미역추출물",
                    "name_eng": "AGARUM EXTRACT",
                }
            ],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "해조류에서 얻은 성분입니다.")

    result = await service.get_ingredient_detail(3)

    assert result.status == "ok"
    assert result.name == "구멍쇠미역추출물"
    assert result.safety == "안전성 확인 불가"


async def test_returns_unconfirmed_when_only_name_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이름만 있고 효능·특성·정의가 모두 없으면 생성하지 않는다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [{"ingredient_id": 4, "name_kr": "가공소금", "inci": None}],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "이 텍스트는 나오면 안 됨")

    result = await service.get_ingredient_detail(4)

    assert result.status == "확인 불가"
    assert result.body is None
    assert result.reason == "해설 근거(효능·특성) 없음"


async def test_generates_with_only_product_traits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """제품 특성만 있어도 해설 근거로 인정된다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 8,
                    "name_kr": "특성만",
                    "inci": "T",
                    "product_traits": "점도를 높여 제형을 안정시킴",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "제형을 안정시키는 역할을 합니다.")

    result = await service.get_ingredient_detail(8)

    assert result.status == "ok"
    assert result.body is not None


# ── 출처 검증 경계 ────────────────────────────────────────────


async def test_keeps_body_when_no_source_cited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """생성문에 출처 표기가 없으면 검증을 통과하고 본문은 그대로 유지된다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 9,
                    "name_kr": "무출처",
                    "inci": "N",
                    "efficacy": "보습",
                    "reference_source": "PubChem",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "피부에 수분을 공급하는 성분입니다.")

    result = await service.get_ingredient_detail(9)

    assert result.source_verified is True
    assert result.body == "피부에 수분을 공급하는 성분입니다."


async def test_keeps_valid_source_from_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """근거에 있는 출처를 인용하면 제거하지 않는다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 10,
                    "name_kr": "정상출처",
                    "inci": "V",
                    "efficacy": "진정",
                    "reference_source": "PubChem",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "진정 효과가 있습니다. 출처: PubChem")

    result = await service.get_ingredient_detail(10)

    assert result.source_verified is True
    assert "PubChem" in (result.body or "")


# ── 제품 요약 경계 ────────────────────────────────────────────


async def test_product_summary_skips_ingredients_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """근거 없는 성분이 섞여 있어도, 근거 있는 성분으로 요약을 생성한다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 1,
                    "name_kr": "유효성분",
                    "inci": "A",
                    "efficacy": "보습",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "보습 중심 제품입니다.")

    result = await service.get_product_summary([1, 2, 3])

    assert result.status == "ok"
    assert result.summary is not None


async def test_product_summary_top_ingredients_limited_to_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """성분이 많아도 대표 성분은 배합순 상위 3개까지만 반환한다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 1,
                    "name_kr": "성분",
                    "inci": "A",
                    "efficacy": "보습",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "요약입니다.")

    result = await service.get_product_summary([1, 2, 3, 4, 5, 6])

    assert len(result.top_ingredients) == 3


async def test_product_summary_raises_not_found_when_all_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """요청한 성분이 하나도 DB에 없으면 404 예외."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "나오면 안 되는 텍스트")

    with pytest.raises(service.IngredientNotFoundError):
        await service.get_product_summary([1, 2])


async def test_product_summary_unconfirmed_when_evidence_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """성분은 있으나 해설 근거가 부족하면 정상 응답 + 확인 불가."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [{"ingredient_id": 1, "name_kr": "이름만", "inci": None}],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "나오면 안 되는 텍스트")

    result = await service.get_product_summary([1])

    assert result.status == "확인 불가"
    assert result.summary is None
    assert result.reason == "제품 성분 근거 없음"


# ── restrictions 경계 ─────────────────────────────────────────


async def test_ignores_empty_restriction_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """내용이 비어 있는 규제 행은 주의사항에 넣지 않는다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 11,
                    "name_kr": "빈규제",
                    "inci": "E",
                    "efficacy": "보습",
                    "safety_note": "자극 낮음",
                }
            ],
            "ingredients": [],
            "restrictions": [
                {
                    "restriction_id": 1,
                    "regulate_type": None,
                    "limit_cond": None,
                    "provis_atrcl": None,
                },
            ],
        },
    )
    _patch_gemini(monkeypatch, "보습 성분입니다.")

    result = await service.get_ingredient_detail(11)

    assert result.safety == "자극 낮음"
    assert "공식 규제" not in (result.safety or "")


async def test_restriction_without_safety_note_still_marks_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """safety_note가 없어도 공식 규제가 있으면 '확인 불가'가 아니다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 12,
                    "name_kr": "규제만",
                    "inci": "O",
                    "efficacy": "보존",
                }
            ],
            "ingredients": [],
            "restrictions": [
                {"restriction_id": 2, "regulate_type": "사용 한도", "limit_cond": "1% 이하"},
            ],
        },
    )
    _patch_gemini(monkeypatch, "보존 성분입니다.")

    result = await service.get_ingredient_detail(12)

    assert result.safety is not None
    assert result.safety != "안전성 확인 불가"
    assert "1% 이하" in result.safety


# ── 예외 처리 ─────────────────────────────────────────────────


async def test_raises_evidence_unavailable_when_db_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """근거 조회(Supabase) 실패는 '근거 없음'이 아니라 장애로 구분된다."""

    async def _broken_supabase() -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(service, "get_supabase", _broken_supabase)

    with pytest.raises(service.EvidenceUnavailableError):
        await service.get_ingredient_detail(1)


async def test_raises_generation_failed_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """생성(Gemini) 실패는 GenerationFailedError로 올라온다."""
    _patch_supabase(
        monkeypatch,
        {
            "rec_efficacy": [
                {
                    "ingredient_id": 1,
                    "name_kr": "성분",
                    "inci": "A",
                    "efficacy": "보습",
                }
            ],
            "ingredients": [],
            "restrictions": [],
        },
    )
    _patch_gemini(monkeypatch, "정상 응답")

    class _BrokenModels:
        async def generate_content(self, **_: Any) -> Any:
            raise RuntimeError("rate limit exceeded")

    class _BrokenAio:
        models = _BrokenModels()

    class _BrokenGemini:
        aio = _BrokenAio()

    monkeypatch.setattr(service, "get_gemini", lambda: _BrokenGemini())

    with pytest.raises(service.GenerationFailedError):
        await service.get_ingredient_detail(1)


async def test_product_summary_survives_partial_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """성분 일부 조회가 실패해도 나머지 근거로 요약을 만든다."""
    call_count = {"n": 0}
    original_tables = {
        "rec_efficacy": [
            {
                "ingredient_id": 1,
                "name_kr": "성분A",
                "inci": "A",
                "efficacy": "보습",
            }
        ],
        "ingredients": [],
        "restrictions": [],
    }

    _patch_supabase(monkeypatch, original_tables)
    _patch_gemini(monkeypatch, "요약입니다.")

    real_fetch = service._fetch_evidence

    async def _flaky_fetch(ingredient_id: int) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise service.EvidenceUnavailableError("일시 오류")
        return await real_fetch(ingredient_id)

    monkeypatch.setattr(service, "_fetch_evidence", _flaky_fetch)

    result = await service.get_product_summary([1, 2])

    assert result.status == "ok"
    assert result.summary is not None


async def test_product_summary_raises_when_all_fetches_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """모든 성분 조회가 실패하면 장애로 올린다('근거 없음'과 구분)."""

    async def _always_fail(ingredient_id: int) -> Any:
        raise service.EvidenceUnavailableError("연결 실패")

    monkeypatch.setattr(service, "_fetch_evidence", _always_fail)

    with pytest.raises(service.EvidenceUnavailableError):
        await service.get_product_summary([1, 2, 3])
