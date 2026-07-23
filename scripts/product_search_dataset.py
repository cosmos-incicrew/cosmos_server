"""제품명 검색 개발용·최종 평가용 데이터셋을 생성하고 검증한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import AsyncClient

from app.core.supabase import create_supabase_client
from app.modules.ingredient_search.repository import SupabaseIngredientSearchRepository

ProductCategory = Literal["스킨케어", "마스크팩", "선케어", "클렌징"]
SearchScenario = Literal[
    "full_product_name",
    "brand_and_core_name",
    "core_product_name",
    "sales_and_capacity_removed",
    "format_variation",
    "not_registered",
]
ExpectedResult = Literal["found", "empty"]
ReviewStatus = Literal["draft", "approved", "excluded"]
DatasetKind = Literal["development", "final", "confirmation"]

DATASET_VERSION = "1.0.0"
DEFAULT_SAMPLING_SEED = 20260722
DEFAULT_DATASET_DIRECTORY = Path("evaluation/product_search/datasets")
DEFAULT_CANDIDATE_OUTPUT = Path(
    f"artifacts/search-evaluation/candidate-pool-v{DATASET_VERSION}.json"
)
_PRODUCT_PAGE_SIZE = 1_000
_INGREDIENT_PAGE_SIZE = 1_000
_PRODUCT_ID_CHUNK_SIZE = 20
_PRODUCT_LOOKUP_CHUNK_SIZE = 100
_EXTRA_CANDIDATES_PER_CATEGORY = 20
_MAX_FINAL_PRODUCTS_PER_BRAND = 2
_FINAL_CANDIDATE_POOL_MULTIPLIER = 2
_MIN_QUERY_LENGTH = 2
_MAX_QUERY_LENGTH = 100

FINAL_LAYOUT: dict[ProductCategory, dict[SearchScenario, int]] = {
    "스킨케어": {
        "full_product_name": 4,
        "brand_and_core_name": 7,
        "core_product_name": 5,
        "sales_and_capacity_removed": 4,
        "format_variation": 5,
    },
    "마스크팩": {
        "full_product_name": 4,
        "brand_and_core_name": 6,
        "core_product_name": 5,
        "sales_and_capacity_removed": 5,
        "format_variation": 5,
    },
    "선케어": {
        "full_product_name": 3,
        "brand_and_core_name": 6,
        "core_product_name": 5,
        "sales_and_capacity_removed": 3,
        "format_variation": 3,
    },
    "클렌징": {
        "full_product_name": 4,
        "brand_and_core_name": 6,
        "core_product_name": 5,
        "sales_and_capacity_removed": 3,
        "format_variation": 2,
    },
}

DEVELOPMENT_LAYOUT: dict[ProductCategory, dict[SearchScenario, int]] = {
    category: {
        "full_product_name": 1,
        "brand_and_core_name": 1,
        "core_product_name": 1,
        "sales_and_capacity_removed": 1,
        "format_variation": 1,
    }
    for category in FINAL_LAYOUT
}

NOT_REGISTERED_QUERIES = [
    "코스모스랩 울트라 수분 장벽 크림 137ml",
    "문라이트 보태니컬 진정 세럼 83ml",
    "오로라랩 비타 글로우 토너 142ml",
    "블루코멧 시카 리페어 앰플 47ml",
    "그린웨이브 판테놀 보습 로션 126ml",
    "소프트플래닛 콜라겐 탄력 에센스 61ml",
    "데일리오빗 약산성 클렌징 폼 173ml",
    "퓨어노바 히알루론 수분 젤 92ml",
    "스킨포레스트 세라마이드 장벽 밤 38ml",
    "라이트블룸 나이아신 톤업 크림 74ml",
]

CONFIRMATION_NOT_REGISTERED_QUERIES = (
    "셀레스티얼랩 프로바이오 장벽 토너 119ml",
    "마린오로라 펩타이드 탄력 앰플 44ml",
    "포레스트문 어성초 카밍 젤 78ml",
    "클라우드베리 세라마이드 보습 크림 53ml",
    "루미나코스트 비타민 광채 에센스 67ml",
    "디어플래닛 판테놀 진정 로션 128ml",
    "블룸스피어 약산성 클렌징 워터 214ml",
    "아쿠아노바 히알루론 수분 마스크 6매",
    "소프트코멧 콜라겐 아이 세럼 31ml",
    "그린오빗 데일리 무기자차 선크림 72ml",
)

_DEVELOPMENT_CASE_COUNT = sum(
    count for scenarios in DEVELOPMENT_LAYOUT.values() for count in scenarios.values()
)
_FINAL_REGISTERED_CASE_COUNT = sum(
    count for scenarios in FINAL_LAYOUT.values() for count in scenarios.values()
)
_FINAL_CASE_COUNT = _FINAL_REGISTERED_CASE_COUNT + len(NOT_REGISTERED_QUERIES)

_PROMOTION_TERMS = ("기획", "단독", "증정", "new", "공식", "한정", "올리브영", "pick")
_BRACKET_PATTERN = re.compile(r"\[([^]]+)]")
_PARENTHESIS_PATTERN = re.compile(r"\(([^()]*)\)")
_COMBINED_CAPACITY_PATTERN = re.compile(
    r"(?<![0-9A-Za-z가-힣])\d+(?:\.\d+)?\s*\+\s*\d+(?:\.\d+)?\s*(?:ml|g)"
    r"(?![0-9A-Za-z가-힣])",
    re.IGNORECASE,
)
_CAPACITY_PATTERN = re.compile(
    r"(?<![0-9A-Za-z가-힣])\d+(?:\.\d+)?\s*(?:ml|g|매입|개입|매|개)"
    r"(?![0-9A-Za-z가-힣])",
    re.IGNORECASE,
)
_BUNDLE_PATTERN = re.compile(r"(?<![0-9A-Za-z가-힣])\d+\s*\+\s*\d+(?!\d)|본품\s*\+\s*(?:리필|증정)")
_MARKETING_TOKEN_PATTERN = re.compile(
    r"(?:더블\s*)?기획|추가\s*증정|증정\s*기획|단독\s*기획|\b단품\b",
    re.IGNORECASE,
)
_SPACE_PATTERN = re.compile(r"\s+")
_MEANINGFUL_PATTERN = re.compile(r"[0-9A-Za-z가-힣]")
_AMBIGUOUS_OPTION_PATTERN = re.compile(r"(?:\d+\s*종\s*)?택\s*1|옵션", re.IGNORECASE)
_VERSION_PATTERN = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")


class ProductRecord(BaseModel):
    id: int
    product_name: str
    brand: str | None
    main_category: ProductCategory
    ingredient_ids: list[int]
    unmapped_ingredient_count: int = 0


class ProductMetadata(BaseModel):
    id: int
    product_name: str
    brand: str | None
    main_category: ProductCategory


class EvaluationCase(BaseModel):
    case_id: str
    query: str
    scenario: SearchScenario
    product_category: ProductCategory | None
    source_product_id: int | None
    source_product_name: str | None
    source_brand: str | None
    acceptable_product_ids: list[int]
    expected_result: ExpectedResult
    review_status: ReviewStatus = "draft"
    reviewed_by: list[str] = Field(default_factory=list)
    reviewed_at: date | None = None
    review_note: str


class EvaluationDataset(BaseModel):
    dataset_version: str
    dataset_kind: DatasetKind
    sampling_seed: int
    generated_at: datetime
    cases: list[EvaluationCase]


class DatasetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_url: str
    supabase_service_role_key: str


def build_draft_datasets(
    products: list[ProductRecord], seed: int = DEFAULT_SAMPLING_SEED
) -> tuple[EvaluationDataset, EvaluationDataset]:
    """확정된 배분으로 사람 검수 전 개발용·최종 평가용 초안을 만든다."""

    eligible = [
        product for product in products if product.ingredient_ids and product.product_name.strip()
    ]
    grouped: dict[ProductCategory, list[ProductRecord]] = defaultdict(list)
    for product in eligible:
        grouped[product.main_category].append(product)

    randomizer = random.Random(seed)
    for category in grouped:
        grouped[category].sort(key=lambda product: product.id)
        randomizer.shuffle(grouped[category])

    final_pools: dict[ProductCategory, list[ProductRecord]] = {}
    development_pools: dict[ProductCategory, list[ProductRecord]] = {}
    for category, products_in_category in grouped.items():
        final_count = sum(FINAL_LAYOUT[category].values())
        final_pool_size = final_count * _FINAL_CANDIDATE_POOL_MULTIPLIER
        final_pools[category] = products_in_category[:final_pool_size]
        development_pools[category] = products_in_category[final_pool_size:]

    used_product_ids: set[int] = set()
    final_cases = _allocate_registered_cases(
        final_pools,
        FINAL_LAYOUT,
        case_prefix="PS",
        used_product_ids=used_product_ids,
        enforce_brand_limit=True,
    )
    development_cases = _allocate_registered_cases(
        development_pools,
        DEVELOPMENT_LAYOUT,
        case_prefix="DEV",
        used_product_ids=used_product_ids,
        enforce_brand_limit=False,
    )
    final_cases.extend(_not_registered_cases())

    generated_at = datetime.now(UTC)
    development = EvaluationDataset(
        dataset_version=DATASET_VERSION,
        dataset_kind="development",
        sampling_seed=seed,
        generated_at=generated_at,
        cases=development_cases,
    )
    final = EvaluationDataset(
        dataset_version=DATASET_VERSION,
        dataset_kind="final",
        sampling_seed=seed,
        generated_at=generated_at,
        cases=final_cases,
    )
    return development, final


def build_confirmation_dataset(
    products: list[ProductRecord],
    excluded_product_ids: set[int],
    seed: int,
) -> EvaluationDataset:
    """기존 회귀셋과 제품이 겹치지 않는 미확인 평가 초안을 만든다."""

    grouped: dict[ProductCategory, list[ProductRecord]] = defaultdict(list)
    for product in products:
        if (
            product.id not in excluded_product_ids
            and product.ingredient_ids
            and product.product_name.strip()
        ):
            grouped[product.main_category].append(product)

    randomizer = random.Random(seed)
    for category in grouped:
        grouped[category].sort(key=lambda product: product.id)
        randomizer.shuffle(grouped[category])

    used_product_ids = set(excluded_product_ids)
    cases = _allocate_registered_cases(
        grouped,
        FINAL_LAYOUT,
        case_prefix="CONF",
        used_product_ids=used_product_ids,
        enforce_brand_limit=True,
    )
    cases.extend(_not_registered_cases(CONFIRMATION_NOT_REGISTERED_QUERIES, "CONF-NR"))
    return EvaluationDataset(
        dataset_version=DATASET_VERSION,
        dataset_kind="confirmation",
        sampling_seed=seed,
        generated_at=datetime.now(UTC),
        cases=cases,
    )


def _allocate_registered_cases(
    grouped: dict[ProductCategory, list[ProductRecord]],
    layout: dict[ProductCategory, dict[SearchScenario, int]],
    case_prefix: str,
    used_product_ids: set[int],
    enforce_brand_limit: bool,
) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    brand_counts: Counter[str] = Counter()
    for category, scenario_counts in layout.items():
        for scenario, count in scenario_counts.items():
            for _ in range(count):
                product = _take_product(
                    grouped[category],
                    scenario,
                    used_product_ids,
                    brand_counts,
                    enforce_brand_limit,
                )
                used_product_ids.add(product.id)
                if product.brand:
                    brand_counts[product.brand.casefold()] += 1
                query = query_for_scenario(product, scenario)
                cases.append(
                    EvaluationCase(
                        case_id=f"{case_prefix}-{len(cases) + 1:03d}",
                        query=query,
                        scenario=scenario,
                        product_category=category,
                        source_product_id=product.id,
                        source_product_name=product.product_name,
                        source_brand=product.brand,
                        acceptable_product_ids=[product.id],
                        expected_result="found",
                        review_note=_draft_review_note(product),
                    )
                )
    return cases


def _draft_review_note(product: ProductRecord) -> str:
    if _AMBIGUOUS_OPTION_PATTERN.search(product.product_name):
        return "규칙 기반 자동 생성 초안 — 복수 옵션 상품 여부 교차 검수 필요"
    return "규칙 기반 자동 생성 초안 — 사람 검수 필요"


def _take_product(
    candidates: list[ProductRecord],
    scenario: SearchScenario,
    used_product_ids: set[int],
    brand_counts: Counter[str],
    enforce_brand_limit: bool,
) -> ProductRecord:
    for product in candidates:
        if product.id in used_product_ids:
            continue
        if (
            enforce_brand_limit
            and product.brand
            and brand_counts[product.brand.casefold()] >= _MAX_FINAL_PRODUCTS_PER_BRAND
        ):
            continue
        query = query_for_scenario(product, scenario)
        if _valid_generated_query(query) and _suitable_for_scenario(product, scenario, query):
            return product
    raise ValueError(f"{scenario} 시나리오에 사용할 제품 후보가 부족합니다.")


def _suitable_for_scenario(product: ProductRecord, scenario: SearchScenario, query: str) -> bool:
    if scenario in {"brand_and_core_name", "core_product_name"} and not product.brand:
        return False
    if scenario == "sales_and_capacity_removed":
        return query != product.product_name.strip()
    if scenario == "format_variation":
        return query != product.product_name.strip()
    return True


def query_for_scenario(product: ProductRecord, scenario: SearchScenario) -> str:
    product_name = _SPACE_PATTERN.sub(" ", product.product_name).strip()
    if scenario == "full_product_name":
        return product_name
    if scenario in {"brand_and_core_name", "sales_and_capacity_removed"}:
        return core_product_name(product_name)
    if scenario == "core_product_name":
        core_name = core_product_name(product_name)
        if product.brand:
            core_name = re.sub(
                re.escape(product.brand), " ", core_name, count=1, flags=re.IGNORECASE
            )
        return _clean_spacing(core_name)
    if scenario == "format_variation":
        return _SPACE_PATTERN.sub("", product_name)
    raise ValueError(f"등록 제품에 지원하지 않는 시나리오입니다: {scenario}")


def core_product_name(product_name: str) -> str:
    """판매·포장·용량 표현만 보수적으로 제거한 사람이 읽는 핵심 제품명."""

    def replace_bracket(match: re.Match[str]) -> str:
        content = match.group(1)
        identifying_parts: list[str] = []
        for part in re.split(r"[/,|]", content):
            value = _BUNDLE_PATTERN.sub(" ", part)
            value = _CAPACITY_PATTERN.sub(" ", value)
            value = _MARKETING_TOKEN_PATTERN.sub(" ", value)
            for term in _PROMOTION_TERMS:
                value = re.sub(re.escape(term), " ", value, flags=re.IGNORECASE)
            if cleaned := _clean_spacing(value):
                identifying_parts.append(cleaned)
        return f" {' '.join(identifying_parts)} "

    def replace_parenthesis(match: re.Match[str]) -> str:
        content = match.group(1)
        lowered = content.casefold()
        if (
            content.lstrip().startswith("+")
            or any(term in lowered for term in (*_PROMOTION_TERMS, "리필", "단품"))
            or re.search(r"\d+\s*(?:ml|g|매|개입|ea)", content, re.IGNORECASE)
        ):
            return " "
        return f" {content} "

    value = unicodedata.normalize("NFKC", product_name)
    value = _BRACKET_PATTERN.sub(replace_bracket, value)
    value = _PARENTHESIS_PATTERN.sub(replace_parenthesis, value)
    value = _COMBINED_CAPACITY_PATTERN.sub(" ", value)
    value = _BUNDLE_PATTERN.sub(" ", value)
    value = _CAPACITY_PATTERN.sub(" ", value)
    value = _MARKETING_TOKEN_PATTERN.sub(" ", value)
    return _clean_spacing(value)


def _clean_spacing(value: str) -> str:
    value = re.sub(r"\s*[/,_]\s*", " ", value)
    value = re.sub(r"\s+\+\s+", " ", value)
    return _SPACE_PATTERN.sub(" ", value).strip(" -()")


def _valid_generated_query(query: str) -> bool:
    meaningful_length = len(_MEANINGFUL_PATTERN.findall(query))
    return meaningful_length >= _MIN_QUERY_LENGTH and len(query) <= _MAX_QUERY_LENGTH


def _not_registered_cases(
    queries: Sequence[str] = NOT_REGISTERED_QUERIES, case_prefix: str = "NR"
) -> list[EvaluationCase]:
    return [
        EvaluationCase(
            case_id=f"{case_prefix}-{index:03d}",
            query=query,
            scenario="not_registered",
            product_category=None,
            source_product_id=None,
            source_product_name=None,
            source_brand=None,
            acceptable_product_ids=[],
            expected_result="empty",
            review_note="미등록 제품명 자동 생성 초안 — DB 결과 0건 및 사람 검수 필요",
        )
        for index, query in enumerate(queries, start=1)
    ]


def validate_confirmation_dataset(
    confirmation: EvaluationDataset,
    excluded_product_ids: set[int],
    *,
    require_approved: bool,
) -> list[str]:
    errors: list[str] = []
    if confirmation.dataset_kind != "confirmation":
        errors.append("확인 데이터셋의 dataset_kind는 confirmation이어야 합니다.")
    if len(confirmation.cases) != _FINAL_CASE_COUNT:
        errors.append(f"확인 데이터셋은 {_FINAL_CASE_COUNT}개 케이스여야 합니다.")
    _validate_cases(confirmation.cases, errors, require_approved=require_approved)
    _validate_layout(confirmation.cases, FINAL_LAYOUT, errors, label="확인용")
    if overlap := _all_product_ids(confirmation.cases) & excluded_product_ids:
        errors.append(f"기존 데이터셋과 중복된 제품 ID가 있습니다: {sorted(overlap)}")
    not_registered_count = sum(case.scenario == "not_registered" for case in confirmation.cases)
    if not_registered_count != len(CONFIRMATION_NOT_REGISTERED_QUERIES):
        errors.append(
            "확인 데이터셋의 미등록 검색어는 "
            f"{len(CONFIRMATION_NOT_REGISTERED_QUERIES)}개여야 합니다."
        )
    return errors


def validate_dataset_pair(
    development: EvaluationDataset,
    final: EvaluationDataset,
    *,
    require_approved: bool,
) -> list[str]:
    """두 데이터셋의 로컬 계약 위반을 사람이 읽을 수 있는 오류 목록으로 반환한다."""

    errors: list[str] = []
    if development.dataset_kind != "development":
        errors.append("개발용 데이터셋의 dataset_kind는 development여야 합니다.")
    if final.dataset_kind != "final":
        errors.append("최종 데이터셋의 dataset_kind는 final이어야 합니다.")
    if len(development.cases) != _DEVELOPMENT_CASE_COUNT:
        errors.append(f"개발용 데이터셋은 {_DEVELOPMENT_CASE_COUNT}개 케이스여야 합니다.")
    if len(final.cases) != _FINAL_CASE_COUNT:
        errors.append(f"최종 데이터셋은 {_FINAL_CASE_COUNT}개 케이스여야 합니다.")
    if development.dataset_version != final.dataset_version:
        errors.append("개발용·최종 평가용 데이터셋 버전이 다릅니다.")
    if not _VERSION_PATTERN.fullmatch(development.dataset_version):
        errors.append("데이터셋 버전은 Semantic Versioning 형식이어야 합니다.")
    if development.sampling_seed != final.sampling_seed:
        errors.append("개발용·최종 평가용 샘플링 시드가 다릅니다.")

    _validate_cases(development.cases, errors, require_approved=require_approved)
    _validate_cases(final.cases, errors, require_approved=require_approved)

    development_ids = _all_product_ids(development.cases)
    final_ids = _all_product_ids(final.cases)
    if development_ids & final_ids:
        errors.append("개발용 제품과 최종 평가용 제품이 중복됩니다.")

    _validate_layout(development.cases, DEVELOPMENT_LAYOUT, errors, label="개발용")
    _validate_layout(final.cases, FINAL_LAYOUT, errors, label="최종")
    not_registered_count = sum(case.scenario == "not_registered" for case in final.cases)
    if not_registered_count != len(NOT_REGISTERED_QUERIES):
        errors.append(
            f"최종 데이터셋의 미등록 검색어는 {len(NOT_REGISTERED_QUERIES)}개여야 합니다."
        )

    final_brands = Counter(
        case.source_brand.casefold()
        for case in final.cases
        if case.expected_result == "found" and case.source_brand
    )
    if over_limit := [
        brand for brand, count in final_brands.items() if count > _MAX_FINAL_PRODUCTS_PER_BRAND
    ]:
        errors.append(
            "동일 브랜드가 "
            f"{_MAX_FINAL_PRODUCTS_PER_BRAND}개를 초과했습니다: {', '.join(sorted(over_limit))}"
        )
    return errors


def _validate_cases(
    cases: list[EvaluationCase], errors: list[str], *, require_approved: bool
) -> None:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        errors.append("case_id가 중복됩니다.")
    queries = [case.query.casefold() for case in cases]
    if len(queries) != len(set(queries)):
        errors.append("검색어가 중복됩니다.")

    source_ids = [case.source_product_id for case in cases if case.source_product_id is not None]
    if len(source_ids) != len(set(source_ids)):
        errors.append("동일한 원본 제품이 둘 이상의 검색 시나리오에 사용됐습니다.")

    owners_by_product_id: dict[int, list[str]] = defaultdict(list)
    for case in cases:
        if len(case.acceptable_product_ids) != len(set(case.acceptable_product_ids)):
            errors.append(f"{case.case_id}: 허용 제품 ID가 중복됩니다.")
        for product_id in set(case.acceptable_product_ids):
            owners_by_product_id[product_id].append(case.case_id)
        if not _valid_generated_query(case.query):
            errors.append(
                f"{case.case_id}: 검색어 길이는 의미 있는 문자 {_MIN_QUERY_LENGTH}자 이상, "
                f"{_MAX_QUERY_LENGTH}자 이하여야 합니다."
            )
        if case.expected_result == "found":
            if case.source_product_id is None or not case.acceptable_product_ids:
                errors.append(f"{case.case_id}: 등록 제품 케이스의 제품 ID가 비어 있습니다.")
            elif case.source_product_id not in case.acceptable_product_ids:
                errors.append(f"{case.case_id}: 원본 제품 ID가 허용 제품 ID에 포함되지 않았습니다.")
        elif case.acceptable_product_ids or case.source_product_id is not None:
            errors.append(f"{case.case_id}: 미등록 검색어에는 제품 ID를 지정할 수 없습니다.")
        if require_approved and (
            case.review_status != "approved" or not case.reviewed_by or case.reviewed_at is None
        ):
            errors.append(f"{case.case_id}: 사람 검수 승인이 완료되지 않았습니다.")

    shared_ids = {
        product_id: owners for product_id, owners in owners_by_product_id.items() if len(owners) > 1
    }
    if shared_ids:
        errors.append(f"하나의 허용 제품 ID가 여러 평가 케이스에 사용됐습니다: {shared_ids}")


def _validate_layout(
    cases: list[EvaluationCase],
    expected: dict[ProductCategory, dict[SearchScenario, int]],
    errors: list[str],
    *,
    label: str,
) -> None:
    actual = Counter(
        (case.product_category, case.scenario) for case in cases if case.expected_result == "found"
    )
    for category, scenarios in expected.items():
        for scenario, count in scenarios.items():
            if actual[(category, scenario)] != count:
                errors.append(f"{label} {category}/{scenario} 케이스는 {count}개여야 합니다.")


def _all_product_ids(cases: list[EvaluationCase]) -> set[int]:
    return {
        product_id
        for case in cases
        for product_id in (
            ([case.source_product_id] if case.source_product_id is not None else [])
            + case.acceptable_product_ids
        )
    }


def replace_excluded_cases(
    development: EvaluationDataset,
    final: EvaluationDataset,
    products: list[ProductRecord],
    *,
    new_version: str,
) -> tuple[EvaluationDataset, EvaluationDataset, list[EvaluationCase]]:
    """제외 케이스를 같은 고정 후보 목록의 다음 제품으로 교체한다."""

    if development.dataset_version != final.dataset_version:
        raise ValueError("교체할 개발용·최종 평가용 데이터셋 버전이 다릅니다.")
    if not _VERSION_PATTERN.fullmatch(new_version):
        raise ValueError("새 버전은 Semantic Versioning 형식이어야 합니다.")
    current_major, current_minor, _current_patch = map(int, development.dataset_version.split("."))
    new_major, new_minor, _new_patch = map(int, new_version.split("."))
    if new_major < current_major or (new_major == current_major and new_minor <= current_minor):
        raise ValueError("제품 교체 결과는 기존보다 높은 MINOR 또는 MAJOR 버전이어야 합니다.")

    new_development = development.model_copy(deep=True)
    new_final = final.model_copy(deep=True)
    history: list[EvaluationCase] = []
    used_product_ids = _all_product_ids(new_development.cases + new_final.cases)
    final_brand_counts = Counter(
        case.source_brand.casefold()
        for case in new_final.cases
        if case.review_status != "excluded" and case.source_brand
    )

    grouped: dict[ProductCategory, list[ProductRecord]] = defaultdict(list)
    for product in products:
        if product.ingredient_ids and product.product_name.strip():
            grouped[product.main_category].append(product)
    randomizer = random.Random(development.sampling_seed)
    for category in grouped:
        grouped[category].sort(key=lambda product: product.id)
        randomizer.shuffle(grouped[category])

    for dataset, enforce_brand_limit in (
        (new_development, False),
        (new_final, True),
    ):
        for index, case in enumerate(dataset.cases):
            if case.review_status != "excluded":
                continue
            if case.product_category is None or case.scenario == "not_registered":
                raise ValueError(f"{case.case_id}: 미등록 검색어는 자동 교체할 수 없습니다.")
            history.append(case.model_copy(deep=True))
            product = _take_product(
                grouped[case.product_category],
                case.scenario,
                used_product_ids,
                final_brand_counts,
                enforce_brand_limit,
            )
            used_product_ids.add(product.id)
            if enforce_brand_limit and product.brand:
                final_brand_counts[product.brand.casefold()] += 1
            dataset.cases[index] = EvaluationCase(
                case_id=case.case_id,
                query=query_for_scenario(product, case.scenario),
                scenario=case.scenario,
                product_category=case.product_category,
                source_product_id=product.id,
                source_product_name=product.product_name,
                source_brand=product.brand,
                acceptable_product_ids=[product.id],
                expected_result="found",
                review_note=(
                    f"{_draft_review_note(product)}; {case.source_product_id} 제외 후 자동 교체"
                ),
            )

    generated_at = datetime.now(UTC)
    for dataset in (new_development, new_final):
        dataset.dataset_version = new_version
        dataset.generated_at = generated_at
    return new_development, new_final, history


async def fetch_candidate_records(
    client: AsyncClient,
    seed: int = DEFAULT_SAMPLING_SEED,
    excluded_product_ids: set[int] | None = None,
) -> list[ProductRecord]:
    """전체 제품 메타데이터에서 필요한 후보만 고른 뒤 성분 매핑을 결합한다."""

    metadata = await _fetch_product_metadata(client)
    selected = _select_metadata_candidates(metadata, seed, excluded_product_ids or set())
    ingredients_by_product, unmapped_by_product = await _fetch_ingredient_mappings(
        client, [product.id for product in selected]
    )
    return [
        ProductRecord(
            id=product.id,
            product_name=product.product_name,
            brand=product.brand,
            main_category=product.main_category,
            ingredient_ids=sorted(ingredients_by_product.get(product.id, set())),
            unmapped_ingredient_count=unmapped_by_product.get(product.id, 0),
        )
        for product in selected
        if ingredients_by_product.get(product.id)
    ]


async def fetch_candidate_records_by_ids(
    client: AsyncClient, product_ids: list[int]
) -> list[ProductRecord]:
    """저장된 후보 ID 순서를 유지하며 현재 DB의 후보 상세를 읽는다."""

    products_by_id = await _fetch_products_by_id(client, product_ids)
    missing_ids = [product_id for product_id in product_ids if product_id not in products_by_id]
    if missing_ids:
        raise ValueError(f"저장된 후보 제품이 현재 DB에 없습니다: {missing_ids}")
    ingredients_by_product, unmapped_by_product = await _fetch_ingredient_mappings(
        client, product_ids
    )

    records: list[ProductRecord] = []
    for product_id in product_ids:
        row = products_by_id[product_id]
        product_name = row.get("product_name")
        category = row.get("main_category")
        brand = row.get("brand")
        if not isinstance(product_name, str) or category not in FINAL_LAYOUT:
            raise ValueError(
                f"{product_id}: 저장된 후보의 현재 제품 메타데이터가 유효하지 않습니다."
            )
        ingredient_ids = sorted(ingredients_by_product.get(product_id, set()))
        if not ingredient_ids:
            raise ValueError(f"{product_id}: 저장된 후보에 매핑된 성분 ID가 없습니다.")
        records.append(
            ProductRecord(
                id=product_id,
                product_name=product_name,
                brand=brand if isinstance(brand, str) and brand.strip() else None,
                main_category=category,
                ingredient_ids=ingredient_ids,
                unmapped_ingredient_count=unmapped_by_product.get(product_id, 0),
            )
        )
    return records


async def _fetch_product_metadata(client: AsyncClient) -> list[ProductMetadata]:
    products: list[ProductMetadata] = []
    offset = 0
    while True:
        response = await (
            client.table("products")
            .select("id,product_name,brand,main_category")
            .order("id")
            .range(offset, offset + _PRODUCT_PAGE_SIZE - 1)
            .execute()
        )
        rows = _rows(response.data)
        for row in rows:
            product_id = row.get("id")
            product_name = row.get("product_name")
            category = row.get("main_category")
            brand = row.get("brand")
            if (
                isinstance(product_id, int)
                and isinstance(product_name, str)
                and category in FINAL_LAYOUT
            ):
                products.append(
                    ProductMetadata(
                        id=product_id,
                        product_name=product_name,
                        brand=brand if isinstance(brand, str) and brand.strip() else None,
                        main_category=category,
                    )
                )
        if len(rows) < _PRODUCT_PAGE_SIZE:
            return products
        offset += len(rows)


def _select_metadata_candidates(
    products: list[ProductMetadata], seed: int, excluded_product_ids: set[int]
) -> list[ProductMetadata]:
    grouped: dict[ProductCategory, list[ProductMetadata]] = defaultdict(list)
    for product in products:
        if product.id not in excluded_product_ids:
            grouped[product.main_category].append(product)

    randomizer = random.Random(seed)
    selected: list[ProductMetadata] = []
    for category in FINAL_LAYOUT:
        category_products = sorted(grouped[category], key=lambda product: product.id)
        randomizer.shuffle(category_products)
        final_pool_size = sum(FINAL_LAYOUT[category].values()) * _FINAL_CANDIDATE_POOL_MULTIPLIER
        required = final_pool_size + sum(DEVELOPMENT_LAYOUT[category].values())
        selected.extend(category_products[: required + _EXTRA_CANDIDATES_PER_CATEGORY])
    return selected


async def _fetch_ingredient_mappings(
    client: AsyncClient, product_ids: list[int]
) -> tuple[dict[int, set[int]], dict[int, int]]:
    ingredients_by_product: dict[int, set[int]] = defaultdict(set)
    unmapped_by_product: dict[int, int] = defaultdict(int)
    for chunk_start in range(0, len(product_ids), _PRODUCT_ID_CHUNK_SIZE):
        chunk = product_ids[chunk_start : chunk_start + _PRODUCT_ID_CHUNK_SIZE]
        offset = 0
        while True:
            response = await (
                client.table("product_ingredients")
                .select("id,product_id,ingredient_id")
                .in_("product_id", chunk)
                .order("id")
                .range(offset, offset + _INGREDIENT_PAGE_SIZE - 1)
                .execute()
            )
            rows = _rows(response.data)
            for row in rows:
                product_id = row.get("product_id")
                ingredient_id = row.get("ingredient_id")
                if not isinstance(product_id, int):
                    continue
                if isinstance(ingredient_id, int):
                    ingredients_by_product[product_id].add(ingredient_id)
                else:
                    unmapped_by_product[product_id] += 1
            if len(rows) < _INGREDIENT_PAGE_SIZE:
                break
            offset += len(rows)
    return ingredients_by_product, unmapped_by_product


async def validate_datasets_against_supabase(
    client: AsyncClient,
    development: EvaluationDataset,
    final: EvaluationDataset,
) -> list[str]:
    """현재 DB의 제품·성분 매핑 및 미등록 검색어 정합성을 확인한다."""

    return await validate_dataset_collection_against_supabase(client, [development, final])


async def validate_dataset_collection_against_supabase(
    client: AsyncClient, datasets: list[EvaluationDataset]
) -> list[str]:
    """하나 이상의 데이터셋과 현재 DB 사이의 정합성을 확인한다."""

    errors: list[str] = []
    cases = [case for dataset in datasets for case in dataset.cases]
    product_ids = sorted(_all_product_ids(cases))
    products_by_id = await _fetch_products_by_id(client, product_ids)
    missing_ids = sorted(set(product_ids) - set(products_by_id))
    if missing_ids:
        errors.append(f"현재 DB에 없는 제품 ID가 있습니다: {missing_ids}")

    ingredients_by_product, unmapped_by_product = await _fetch_ingredient_mappings(
        client, product_ids
    )
    for case in cases:
        if case.expected_result != "found" or case.source_product_id is None:
            continue
        if current_product := products_by_id.get(case.source_product_id):
            if current_product.get("product_name") != case.source_product_name:
                errors.append(f"{case.case_id}: 원본 제품명이 현재 DB와 다릅니다.")
            if current_product.get("main_category") != case.product_category:
                errors.append(f"{case.case_id}: 제품군이 현재 DB와 다릅니다.")
            current_brand = current_product.get("brand") or None
            if current_brand != case.source_brand:
                errors.append(f"{case.case_id}: 브랜드가 현재 DB와 다릅니다.")
        if not ingredients_by_product.get(case.source_product_id):
            errors.append(f"{case.case_id}: 원본 제품에 매핑된 성분 ID가 없습니다.")
        acceptable_ids = case.acceptable_product_ids
        if len(acceptable_ids) <= 1:
            continue
        ingredient_sets = [
            ingredients_by_product.get(product_id, set()) for product_id in acceptable_ids
        ]
        if any(unmapped_by_product.get(product_id, 0) for product_id in acceptable_ids):
            errors.append(f"{case.case_id}: 복수 허용 제품 중 미매핑 성분이 있는 제품이 있습니다.")
        if not ingredient_sets or any(
            values != ingredient_sets[0] for values in ingredient_sets[1:]
        ):
            errors.append(f"{case.case_id}: 복수 허용 제품의 성분 ID 집합이 다릅니다.")

    repository = SupabaseIngredientSearchRepository(client)
    for case in cases:
        if case.expected_result != "empty":
            continue
        if await repository.search_products(case.query, limit=1):
            errors.append(f"{case.case_id}: 미등록 검색어가 현재 DB 제품을 반환합니다.")
    return errors


async def _fetch_products_by_id(
    client: AsyncClient, product_ids: list[int]
) -> dict[int, dict[str, Any]]:
    products: dict[int, dict[str, Any]] = {}
    for chunk_start in range(0, len(product_ids), _PRODUCT_LOOKUP_CHUNK_SIZE):
        chunk = product_ids[chunk_start : chunk_start + _PRODUCT_LOOKUP_CHUNK_SIZE]
        response = await (
            client.table("products")
            .select("id,product_name,brand,main_category")
            .in_("id", chunk)
            .execute()
        )
        for row in _rows(response.data):
            if isinstance((product_id := row.get("id")), int):
                products[product_id] = row
    return products


def write_dataset(dataset: EvaluationDataset, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(
            json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        )


def load_dataset(path: Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(path.read_text(encoding="utf-8"))


def load_candidate_product_ids(path: Path) -> list[int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise ValueError("후보 파일의 candidates 목록이 유효하지 않습니다.")
    product_ids: list[int] = []
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance((product_id := candidate.get("id")), int):
            product_ids.append(product_id)
    if len(product_ids) != len(candidates) or len(product_ids) != len(set(product_ids)):
        raise ValueError("후보 파일의 제품 ID가 누락되었거나 중복됐습니다.")
    return product_ids


def _write_candidate_summary(records: list[ProductRecord], path: Path, seed: int) -> None:
    payload = {
        "dataset_version": DATASET_VERSION,
        "sampling_seed": seed,
        "generated_at": datetime.now(UTC).isoformat(),
        "candidates": [
            {
                "id": product.id,
                "product_name": product.product_name,
                "brand": product.brand,
                "main_category": product.main_category,
                "mapped_ingredient_count": len(product.ingredient_ids),
                "unmapped_ingredient_count": product.unmapped_ingredient_count,
            }
            for product in records
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _ensure_outputs_do_not_exist(paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("기존 버전을 덮어쓸 수 없습니다: " + ", ".join(existing))


async def _client_from_environment() -> AsyncClient:
    settings = DatasetSettings()
    return await create_supabase_client(settings.supabase_url, settings.supabase_service_role_key)


async def _generate(args: argparse.Namespace) -> None:
    development_path = args.output_dir / f"development-v{DATASET_VERSION}.json"
    final_path = args.output_dir / f"final-v{DATASET_VERSION}.json"
    _ensure_outputs_do_not_exist([development_path, final_path, args.candidate_output])
    client = await _client_from_environment()
    records = await fetch_candidate_records(client, seed=args.seed)
    development, final = build_draft_datasets(records, seed=args.seed)
    errors = validate_dataset_pair(development, final, require_approved=False)
    errors.extend(await validate_datasets_against_supabase(client, development, final))
    if errors:
        raise SystemExit("초안 생성 검증 실패:\n- " + "\n- ".join(errors))

    write_dataset(development, development_path)
    write_dataset(final, final_path)
    _write_candidate_summary(records, args.candidate_output, args.seed)
    print(f"개발용 초안: {development_path}")
    print(f"최종 평가용 초안: {final_path}")
    print(f"후보 검수 참고: {args.candidate_output}")


async def _generate_confirmation(args: argparse.Namespace) -> None:
    output = args.output_dir / f"confirmation-v{DATASET_VERSION}.json"
    _ensure_outputs_do_not_exist([output])
    excluded_datasets = [load_dataset(path) for path in args.exclude]
    excluded_product_ids = _all_product_ids(
        [case for dataset in excluded_datasets for case in dataset.cases]
    )

    client = await _client_from_environment()
    records = await fetch_candidate_records(
        client,
        seed=args.seed,
        excluded_product_ids=excluded_product_ids,
    )
    confirmation = build_confirmation_dataset(records, excluded_product_ids, args.seed)
    errors = validate_confirmation_dataset(
        confirmation,
        excluded_product_ids,
        require_approved=False,
    )
    errors.extend(await validate_dataset_collection_against_supabase(client, [confirmation]))
    if errors:
        raise SystemExit("확인 데이터셋 초안 생성 검증 실패:\n- " + "\n- ".join(errors))

    write_dataset(confirmation, output)
    print(f"신규 미확인 평가 초안: {output}")
    print(f"기존 데이터셋과 제외한 제품 ID: {len(excluded_product_ids)}개")


async def _replace(args: argparse.Namespace) -> None:
    development = load_dataset(args.development)
    final = load_dataset(args.final)
    development_path = args.output_dir / f"development-v{args.new_version}.json"
    final_path = args.output_dir / f"final-v{args.new_version}.json"
    history_path = args.output_dir / f"excluded-v{args.new_version}.json"
    _ensure_outputs_do_not_exist([development_path, final_path, history_path])

    client = await _client_from_environment()
    candidate_ids = load_candidate_product_ids(args.candidate_input)
    records = await fetch_candidate_records_by_ids(client, candidate_ids)
    new_development, new_final, history = replace_excluded_cases(
        development,
        final,
        records,
        new_version=args.new_version,
    )
    if not history:
        raise SystemExit("교체할 excluded 케이스가 없습니다.")
    errors = validate_dataset_pair(new_development, new_final, require_approved=False)
    errors.extend(await validate_datasets_against_supabase(client, new_development, new_final))
    if errors:
        raise SystemExit("교체 초안 검증 실패:\n- " + "\n- ".join(errors))

    write_dataset(new_development, development_path)
    write_dataset(new_final, final_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("x", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "previous_version": development.dataset_version,
                    "new_version": args.new_version,
                    "excluded_cases": [case.model_dump(mode="json") for case in history],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    print(f"교체된 개발용 초안: {development_path}")
    print(f"교체된 최종 평가용 초안: {final_path}")
    print(f"제외 이력: {history_path}")


async def _validate(args: argparse.Namespace) -> None:
    development = load_dataset(args.development)
    final = load_dataset(args.final)
    errors = validate_dataset_pair(
        development,
        final,
        require_approved=not args.allow_draft,
    )
    client = await _client_from_environment()
    errors.extend(await validate_datasets_against_supabase(client, development, final))
    if errors:
        raise SystemExit("데이터셋 검증 실패:\n- " + "\n- ".join(errors))
    print("데이터셋 검증 통과")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="제품명 검색 평가 데이터셋 생성·검증")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="실제 DB에서 검수 전 JSON 초안 생성")
    generate.add_argument("--seed", type=int, default=DEFAULT_SAMPLING_SEED)
    generate.add_argument("--output-dir", type=Path, default=DEFAULT_DATASET_DIRECTORY)
    generate.add_argument("--candidate-output", type=Path, default=DEFAULT_CANDIDATE_OUTPUT)

    confirmation = subparsers.add_parser(
        "generate-confirmation",
        help="기존 데이터셋과 제품이 겹치지 않는 확인용 JSON 초안 생성",
    )
    confirmation.add_argument("--seed", type=int, default=DEFAULT_SAMPLING_SEED + 1)
    confirmation.add_argument("--output-dir", type=Path, default=DEFAULT_DATASET_DIRECTORY)
    confirmation.add_argument(
        "--exclude",
        type=Path,
        nargs="+",
        default=[
            DEFAULT_DATASET_DIRECTORY / f"development-v{DATASET_VERSION}.json",
            DEFAULT_DATASET_DIRECTORY / f"final-v{DATASET_VERSION}.json",
        ],
        help="제품 ID 중복을 금지할 기존 데이터셋 경로",
    )

    validate = subparsers.add_parser("validate", help="JSON 구조와 실제 DB 정합성 검증")
    validate.add_argument(
        "--development",
        type=Path,
        default=DEFAULT_DATASET_DIRECTORY / f"development-v{DATASET_VERSION}.json",
    )
    validate.add_argument(
        "--final",
        type=Path,
        default=DEFAULT_DATASET_DIRECTORY / f"final-v{DATASET_VERSION}.json",
    )
    validate.add_argument("--allow-draft", action="store_true")

    replace = subparsers.add_parser(
        "replace-excluded",
        help="excluded 케이스를 다음 고정 후보로 교체해 새 버전 생성",
    )
    replace.add_argument("--new-version", required=True)
    replace.add_argument(
        "--development",
        type=Path,
        default=DEFAULT_DATASET_DIRECTORY / f"development-v{DATASET_VERSION}.json",
    )
    replace.add_argument(
        "--final",
        type=Path,
        default=DEFAULT_DATASET_DIRECTORY / f"final-v{DATASET_VERSION}.json",
    )
    replace.add_argument("--output-dir", type=Path, default=DEFAULT_DATASET_DIRECTORY)
    replace.add_argument(
        "--candidate-input",
        type=Path,
        default=DEFAULT_CANDIDATE_OUTPUT,
        help="최초 생성 시 보존한 고정 후보 파일",
    )
    return parser.parse_args()


def _rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def main() -> None:
    args = _parse_args()
    if args.command == "generate":
        asyncio.run(_generate(args))
    elif args.command == "generate-confirmation":
        asyncio.run(_generate_confirmation(args))
    elif args.command == "replace-excluded":
        asyncio.run(_replace(args))
    else:
        asyncio.run(_validate(args))


if __name__ == "__main__":
    main()
