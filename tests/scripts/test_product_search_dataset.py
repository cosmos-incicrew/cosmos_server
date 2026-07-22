from collections import Counter
from datetime import date
from pathlib import Path

from scripts.product_search_dataset import (
    ProductRecord,
    build_confirmation_dataset,
    build_draft_datasets,
    core_product_name,
    load_candidate_product_ids,
    load_dataset,
    query_for_scenario,
    replace_excluded_cases,
    validate_confirmation_dataset,
    validate_dataset_pair,
    write_dataset,
)


def _products() -> list[ProductRecord]:
    products: list[ProductRecord] = []
    product_id = 1
    for category in ("스킨케어", "마스크팩", "선케어", "클렌징"):
        for _index in range(80):
            products.append(
                ProductRecord(
                    id=product_id,
                    product_name=(f"[기획] 브랜드{product_id} 핵심제품{product_id} 크림 80ml"),
                    brand=f"브랜드{product_id}",
                    main_category=category,
                    ingredient_ids=[product_id, product_id + 10_000],
                    unmapped_ingredient_count=0,
                )
            )
            product_id += 1
    return products


def test_build_draft_datasets_follows_the_approved_distribution() -> None:
    development, final = build_draft_datasets(_products(), seed=20260722)

    assert len(development.cases) == 20
    assert len(final.cases) == 100
    assert Counter(case.product_category for case in development.cases) == {
        "스킨케어": 5,
        "마스크팩": 5,
        "선케어": 5,
        "클렌징": 5,
    }
    assert Counter(case.scenario for case in development.cases) == {
        "full_product_name": 4,
        "brand_and_core_name": 4,
        "core_product_name": 4,
        "sales_and_capacity_removed": 4,
        "format_variation": 4,
    }
    assert Counter(case.product_category for case in final.cases) == {
        "스킨케어": 25,
        "마스크팩": 25,
        "선케어": 20,
        "클렌징": 20,
        None: 10,
    }
    assert Counter(case.scenario for case in final.cases) == {
        "full_product_name": 15,
        "brand_and_core_name": 25,
        "core_product_name": 20,
        "sales_and_capacity_removed": 15,
        "format_variation": 15,
        "not_registered": 10,
    }

    development_ids = {
        case.source_product_id for case in development.cases if case.source_product_id is not None
    }
    final_ids = {
        case.source_product_id for case in final.cases if case.source_product_id is not None
    }
    assert len(development_ids) == 20
    assert len(final_ids) == 90
    assert development_ids.isdisjoint(final_ids)
    assert all(case.review_status == "draft" for case in development.cases + final.cases)
    assert validate_dataset_pair(development, final, require_approved=False) == []


def test_confirmation_dataset_excludes_existing_products() -> None:
    products = _products()
    development, final = build_draft_datasets(products, seed=20260722)
    excluded_ids = {
        case.source_product_id
        for case in development.cases + final.cases
        if case.source_product_id is not None
    }

    confirmation = build_confirmation_dataset(products, excluded_ids, seed=20260723)

    assert len(confirmation.cases) == 100
    assert confirmation.dataset_kind == "confirmation"
    assert validate_confirmation_dataset(
        confirmation, excluded_ids, require_approved=False
    ) == []


def test_query_drafts_remove_only_the_scenario_specific_parts() -> None:
    product = ProductRecord(
        id=1,
        product_name="[기획/증정] 에스트라 아토베리어365 크림 80ml + 10ml",
        brand="에스트라",
        main_category="스킨케어",
        ingredient_ids=[1, 2],
    )

    assert core_product_name(product.product_name) == "에스트라 아토베리어365 크림"
    assert query_for_scenario(product, "brand_and_core_name") == ("에스트라 아토베리어365 크림")
    assert query_for_scenario(product, "core_product_name") == "아토베리어365 크림"
    assert query_for_scenario(product, "format_variation") == (
        "[기획/증정]에스트라아토베리어365크림80ml+10ml"
    )


def test_core_product_name_removes_leading_marketing_and_gift_parentheses() -> None:
    assert (
        core_product_name(
            "[모공/광채/PICK] 바이오던스 포어 퍼펙팅 세럼 50ml 기획 (+크림10ml 추가 증정)"
        )
        == "모공 광채 바이오던스 포어 퍼펙팅 세럼"
    )


def test_core_product_name_preserves_identifying_bracket_content() -> None:
    assert core_product_name("[PDRN] 브랜드 리페어 세럼 30ml") == "PDRN 브랜드 리페어 세럼"
    assert core_product_name("[무향] 브랜드 로션 100ml") == "무향 브랜드 로션"
    assert core_product_name("[1+1/피치톤업] 브랜드 선크림 50+50ml") == "피치톤업 브랜드 선크림"


def test_core_product_name_removes_combined_capacity_and_bundle_markers() -> None:
    assert (
        core_product_name("[1+1/피치톤업] 이니스프리 비타민C 피치 톤업 선크림 50+50ml 1+1기획")
        == "피치톤업 이니스프리 비타민C 피치 톤업 선크림"
    )


def test_core_product_name_preserves_product_identifier_before_capacity() -> None:
    assert (
        core_product_name("라로슈포제 시카플라스트 밤 B5+ 100ml")
        == "라로슈포제 시카플라스트 밤 B5+"
    )


def test_validation_requires_human_approval_for_the_official_evaluation() -> None:
    development, final = build_draft_datasets(_products(), seed=20260722)

    errors = validate_dataset_pair(development, final, require_approved=True)

    assert len(errors) == 120
    assert errors[0].endswith("사람 검수 승인이 완료되지 않았습니다.")


def test_validation_rejects_an_acceptable_product_shared_between_sets() -> None:
    development, final = build_draft_datasets(_products(), seed=20260722)
    final_product_id = final.cases[0].source_product_id
    assert final_product_id is not None
    development.cases[0].acceptable_product_ids.append(final_product_id)

    errors = validate_dataset_pair(development, final, require_approved=False)

    assert "개발용 제품과 최종 평가용 제품이 중복됩니다." in errors


def test_committed_draft_datasets_follow_the_local_contract() -> None:
    dataset_directory = Path("evaluation/product_search/datasets")
    development = load_dataset(dataset_directory / "development-v1.0.0.json")
    final = load_dataset(dataset_directory / "final-v1.0.0.json")

    assert validate_dataset_pair(development, final, require_approved=False) == []


def test_write_dataset_does_not_overwrite_an_existing_version(tmp_path: Path) -> None:
    development, _final = build_draft_datasets(_products(), seed=20260722)
    path = tmp_path / "development-v1.0.0.json"
    write_dataset(development, path)

    try:
        write_dataset(development, path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("기존 데이터셋 버전을 덮어썼습니다.")


def test_load_candidate_product_ids_preserves_the_saved_order(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text(
        '{"candidates":[{"id":31},{"id":7},{"id":19}]}',
        encoding="utf-8",
    )

    assert load_candidate_product_ids(path) == [31, 7, 19]


def test_replace_excluded_case_uses_next_fixed_candidate_and_keeps_history() -> None:
    products = _products()
    development, final = build_draft_datasets(products, seed=20260722)
    excluded = final.cases[0]
    excluded.review_status = "excluded"
    excluded.reviewed_by = ["검수자"]
    excluded.reviewed_at = date(2026, 7, 22)
    excluded.review_note = "복수 옵션 상품"
    old_product_id = excluded.source_product_id

    new_development, new_final, history = replace_excluded_cases(
        development,
        final,
        products,
        new_version="1.1.0",
    )

    replacement = new_final.cases[0]
    assert new_development.dataset_version == "1.1.0"
    assert new_final.dataset_version == "1.1.0"
    assert replacement.case_id == excluded.case_id
    assert replacement.source_product_id != old_product_id
    assert replacement.product_category == excluded.product_category
    assert replacement.scenario == excluded.scenario
    assert replacement.review_status == "draft"
    assert history == [excluded]


def test_replace_excluded_case_requires_a_minor_or_major_version_increase() -> None:
    products = _products()
    development, final = build_draft_datasets(products, seed=20260722)
    final.cases[0].review_status = "excluded"

    for invalid_version in ("1.0.1", "0.9.0"):
        try:
            replace_excluded_cases(
                development,
                final,
                products,
                new_version=invalid_version,
            )
        except ValueError as error:
            assert "MINOR" in str(error)
        else:
            raise AssertionError(f"잘못된 교체 버전을 허용했습니다: {invalid_version}")
