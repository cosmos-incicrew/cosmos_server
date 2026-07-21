"""JSON 테이블 데이터를 조회하는 테스트 전용 Supabase 대역."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRODUCT_COMPARE_MOCK_PATH = (
    Path(__file__).parents[2] / "mock-data" / "product-compare" / "supabase-tables.json"
)


@dataclass(frozen=True)
class MockResponse:
    data: Any


class MockSupabaseQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._selected_columns: tuple[str, ...] | None = None
        self._filters: list[tuple[str, set[Any]]] = []
        self._order_columns: list[str] = []
        self._maybe_single = False

    def select(self, columns: str) -> "MockSupabaseQuery":
        self._selected_columns = tuple(column.strip() for column in columns.split(","))
        return self

    def in_(self, column: str, values: list[int]) -> "MockSupabaseQuery":
        self._filters.append((column, set(values)))
        return self

    def eq(self, column: str, value: Any) -> "MockSupabaseQuery":
        self._filters.append((column, {value}))
        return self

    def order(self, column: str) -> "MockSupabaseQuery":
        self._order_columns.append(column)
        return self

    def maybe_single(self) -> "MockSupabaseQuery":
        self._maybe_single = True
        return self

    async def execute(self) -> MockResponse:
        rows = [dict(row) for row in self._rows]
        for column, values in self._filters:
            rows = [row for row in rows if row.get(column) in values]
        if self._order_columns:
            rows.sort(
                key=lambda row: tuple(
                    _sort_value(row.get(column)) for column in self._order_columns
                )
            )
        if self._selected_columns is not None:
            rows = [{column: row.get(column) for column in self._selected_columns} for row in rows]
        if self._maybe_single:
            return MockResponse(data=rows[0] if rows else None)
        return MockResponse(data=rows)


class MockSupabaseClient:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def table(self, table_name: str) -> MockSupabaseQuery:
        try:
            rows = self._tables[table_name]
        except KeyError as error:
            raise KeyError(f"Mock Supabase table not found: {table_name}") from error
        return MockSupabaseQuery(rows)


def load_mock_supabase(path: Path) -> MockSupabaseClient:
    raw_data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raise ValueError("Mock Supabase data must be a JSON object")

    tables: dict[str, list[dict[str, Any]]] = {}
    for table_name, rows in raw_data.items():
        if not isinstance(table_name, str) or not isinstance(rows, list):
            raise ValueError("Each Mock Supabase table must contain a JSON array")
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"Mock Supabase table contains a non-object row: {table_name}")
        tables[table_name] = rows
    return MockSupabaseClient(tables)


def load_product_compare_mock_supabase() -> MockSupabaseClient:
    return load_mock_supabase(PRODUCT_COMPARE_MOCK_PATH)


def _sort_value(value: Any) -> tuple[bool, Any]:
    return (value is None, value)
