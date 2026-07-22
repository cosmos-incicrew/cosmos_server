from app.modules.ingredient_search.ingredient_matching import (
    IngredientMatchCandidate,
    normalize_ingredient_text,
    rank_ingredient_candidates,
)


def _candidate(
    ingredient_id: int,
    name_kor: str,
    name_eng: str | None = None,
    synonyms: tuple[str, ...] = (),
) -> IngredientMatchCandidate:
    return IngredientMatchCandidate(
        ingredient_id=ingredient_id,
        name_kor=name_kor,
        name_eng=name_eng,
        synonyms=synonyms,
    )


def test_normalization_ignores_formatting_and_preserves_unicode_letters() -> None:
    assert normalize_ingredient_text("비타민 C-유도체") == "비타민c유도체"
    assert normalize_ingredient_text("Crème 東京") == "crème東京"


def test_ranking_prioritizes_standard_then_synonym_match_tiers() -> None:
    candidates = [
        _candidate(6, "다른 성분", synonyms=("고순도 비타민C 복합체",)),
        _candidate(4, "비타민C유도체"),
        _candidate(2, "아스코빅애씨드", synonyms=("비타민 C",)),
        _candidate(5, "다른 비타민C 성분"),
        _candidate(3, "아스코빌글루코사이드", synonyms=("비타민C유도체",)),
        _candidate(1, "비타민 C"),
    ]

    ranked = rank_ingredient_candidates("비타민C", candidates)

    assert [candidate.ingredient_id for candidate in ranked] == [1, 2, 4, 3, 5, 6]


def test_ranking_drops_candidates_that_only_match_a_loose_database_pattern() -> None:
    candidates = [
        _candidate(1, "나이아신아마이드"),
        _candidate(2, "나트륨 이온 아마이드"),
    ]

    ranked = rank_ingredient_candidates("나이아신", candidates)

    assert [candidate.ingredient_id for candidate in ranked] == [1]


def test_ranking_is_deterministic_for_equal_matches() -> None:
    candidates = [
        _candidate(2, "판테놀B"),
        _candidate(1, "판테놀A"),
    ]

    first = rank_ingredient_candidates("판테놀", candidates)
    second = rank_ingredient_candidates("판테놀", list(reversed(candidates)))

    assert [candidate.ingredient_id for candidate in first] == [1, 2]
    assert first == second
