"""추천 테스트용 Supabase 목. ingredient_detail 의 _FakeQuery 패턴을 확장했다.

추천 파이프라인은 limit·order·in_·overlaps·or_ 까지 체이닝하고, "테이블이 아예 없어
예외가 나는" 상황(bsti_*·user_shelf)을 재현해야 해서 실패 주입을 함께 둔다.
"""

from typing import Any

import pytest


class FakeQuery:
    """Supabase 쿼리 체이닝 흉내. 필터는 무시하고 준비된 행을 그대로 돌려준다."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def select(self, *_: Any, **__: Any) -> "FakeQuery":
        return self

    def eq(self, *_: Any) -> "FakeQuery":
        return self

    def in_(self, *_: Any) -> "FakeQuery":
        return self

    def overlaps(self, *_: Any) -> "FakeQuery":
        return self

    def or_(self, *_: Any) -> "FakeQuery":
        return self

    def order(self, *_: Any, **__: Any) -> "FakeQuery":
        return self

    def limit(self, *_: Any) -> "FakeQuery":
        return self

    async def execute(self) -> Any:
        return type("Result", (), {"data": self._rows})()


class MissingTable(RuntimeError):
    """실 DB 에 없는 테이블을 조회했을 때 PostgREST 가 내는 오류 대역."""


class FakeSupabase:
    """table 이름별 행을 돌려준다. `missing` 에 든 이름은 조회 시 예외를 던진다."""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        missing: set[str] | None = None,
    ):
        self._tables = tables or {}
        self._missing = missing or set()

    def table(self, name: str) -> FakeQuery:
        if name in self._missing:
            raise MissingTable(f'relation "public.{name}" does not exist')
        return FakeQuery(self._tables.get(name, []))


@pytest.fixture()
def patch_supabase(monkeypatch: pytest.MonkeyPatch):
    """service.get_supabase 를 가짜 클라이언트로 바꾼다."""

    def _apply(
        tables: dict[str, list[dict[str, Any]]] | None = None,
        missing: set[str] | None = None,
    ) -> FakeSupabase:
        from app.modules.recommendations.pipeline import s1_context as context

        client = FakeSupabase(tables, missing)

        async def _fake_get_supabase() -> FakeSupabase:
            return client

        monkeypatch.setattr(context, "get_supabase", _fake_get_supabase)
        return client

    return _apply
