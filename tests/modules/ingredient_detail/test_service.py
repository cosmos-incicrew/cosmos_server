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


async def test_returns_unconfirmed_when_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_supabase(monkeypatch, {"rec_efficacy": [], "ingredients": []})
    _patch_gemini(monkeypatch, "이 텍스트는 나오면 안 됨")

    result = await service.get_ingredient_detail(999)

    assert result.status == "확인 불가"
    assert result.body is None
    assert result.reason == "성분 근거 없음"


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
