"""추천 테스트용 Supabase 목. ingredient_detail 의 _FakeQuery 패턴을 확장했다.

추천 파이프라인은 limit·order·in_·overlaps·or_ 까지 체이닝하고, "테이블이 아예 없어
예외가 나는" 상황(bsti_*·user_shelf)을 재현해야 해서 실패 주입을 함께 둔다.
"""

from typing import Any

import pytest

from app.modules.recommendations import embedding


class FakeQuery:
    """Supabase 쿼리 체이닝 흉내.

    `eq`/`in_` 는 실제로 행을 거른다 — 필터를 버리면 컬럼명을 오타내도(`name_kor`→
    `name_kr`) 조회가 그대로 성공해 조회 계약을 원리적으로 검증할 수 없다. 값이 없는
    행은 실 Postgres 의 NULL 비교와 같이 어느 필터에도 걸리지 않는다.

    모든 호출은 `calls` 에 `(테이블, 메서드, 인자)` 로 남는다 — select 컬럼 목록·
    limit 상한·3단 조회 순서처럼 결과만 봐서는 알 수 없는 계약을 단언하기 위함이다.
    """

    def __init__(self, table: str, rows: list[dict[str, Any]], calls: list[tuple]):
        self._table = table
        self._rows = rows
        self._calls = calls
        self._limit: int | None = None

    def _record(self, method: str, *args: Any) -> None:
        self._calls.append((self._table, method, args))

    def select(self, *columns: Any, **__: Any) -> "FakeQuery":
        self._record("select", *columns)
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._record("eq", column, value)
        self._rows = [row for row in self._rows if row.get(column) == value]
        return self

    def in_(self, column: str, values: Any) -> "FakeQuery":
        wanted = list(values)
        self._record("in_", column, wanted)
        self._rows = [row for row in self._rows if row.get(column) in wanted]
        return self

    def overlaps(self, *args: Any) -> "FakeQuery":
        self._record("overlaps", *args)
        return self

    def or_(self, *args: Any) -> "FakeQuery":
        self._record("or_", *args)
        return self

    def order(self, *args: Any, **__: Any) -> "FakeQuery":
        self._record("order", *args)
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._record("limit", n)
        self._limit = n
        return self

    async def execute(self) -> Any:
        return type("Result", (), {"data": self._rows[: self._limit]})()


class MissingTable(RuntimeError):
    """실 DB 에 없는 테이블을 조회했을 때 PostgREST 가 내는 오류 대역."""


class FakeRpc:
    """`client.rpc(fn, params).execute()` 체이닝 흉내 — match_rec_* 전용."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    async def execute(self) -> Any:
        return type("Result", (), {"data": self._rows})()


# RPC 함수명 → 소스 테이블. s3_retrieval.retrieve() 가 부르는 두 함수만 대역한다.
_RPC_TO_TABLE = {"match_rec_cases": "rec_cases", "match_rec_efficacy": "rec_efficacy"}


class FakeSupabase:
    """table 이름별 행을 돌려준다. `missing` 에 든 이름은 조회 시 예외를 던진다."""

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        missing: set[str] | None = None,
    ):
        self._tables = tables or {}
        self._missing = missing or set()
        # (테이블, 메서드, 인자) 호출 기록 — 조회 순서·컬럼·상한 단언용.
        self.calls: list[tuple] = []

    def table(self, name: str) -> FakeQuery:
        if name in self._missing:
            raise MissingTable(f'relation "public.{name}" does not exist')
        return FakeQuery(name, list(self._tables.get(name, [])), self.calls)

    def filters(self, table: str) -> list[tuple[str, Any]]:
        """해당 테이블에 건 `eq`/`in_` 필터를 (컬럼, 값) 으로 호출 순서대로 돌려준다."""
        return [
            (args[0], args[1])
            for name, method, args in self.calls
            if name == table and method in ("eq", "in_")
        ]

    def rpc(self, fn: str, params: dict[str, Any]) -> FakeRpc:
        """match_rec_cases/match_rec_efficacy 대역 — 테이블 행에 고정 score 를 붙인다.

        실 RPC 의 코사인 계산은 흉내낼 수 없으니, MIN_RETRIEVAL_SCORE(0.5)를 넘는
        고정값을 채워 검색 결과가 그대로 후속 단계로 흘러가게 한다.
        """
        table = _RPC_TO_TABLE[fn]
        rows = [{**row, "score": row.get("score", 0.9)} for row in self._tables.get(table, [])]
        return FakeRpc(rows)


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


@pytest.fixture(autouse=True)
def _clear_query_embedding_cache():
    """질의 임베딩 캐시는 모듈 전역이라 테스트 간에 샌다.

    앞 테스트가 캐시한 텍스트를 뒤 테스트가 그대로 받아, 실패를 기대하는 테스트가
    API 를 부르지도 않고 통과한다.
    """
    embedding._QUERY_CACHE.clear()
    yield
    embedding._QUERY_CACHE.clear()
