"""실행 결과를 사람이 읽는 출력과 마크다운 리포트로 만든다.

터미널 출력과 리포트를 한 곳에서 만드는 이유는 둘이 갈라지면 "화면에선 통과인데
문서에는 실패"가 나기 때문이다.
"""

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from tests.modules.recommendations.usecase.runner import Case

_BADGE = {"both": "고민+타입", "concern": "고민", "bsti": "피부타입"}


def _profile_line(case: Case) -> str:
    context = case.context
    concerns = ", ".join(CONCERN_LABEL_BY_CODE.get(c, c) for c in context.concerns)
    return (
        f"{context.bsti_type or 'BSTI 미검사'} · {context.age}세 "
        f"{'여성' if context.gender == 'female' else '남성'} · {concerns}"
    )


def print_case(case: Case) -> None:
    """한 건의 실제 출력. 계약 검사와 별개로 눈으로 볼 것이 있어 함께 찍는다."""
    mark = "PASS" if case.ok else "FAIL"
    print(f"\n[{mark}] {_profile_line(case)}  ({case.seconds:.1f}s)")

    if case.error:
        print(f"  예외: {case.error}")
        return
    response = case.response
    if response is None:
        return

    advisory = response.advisory.code if response.advisory else None
    print(f"  status={response.status} advisory={advisory}")
    for card in response.top_ingredients:
        source = _BADGE.get(card.match_source or "", "?")
        similarity = "" if card.similarity is None else f" {card.similarity}"
        warnings = "".join(f" ⚠{w.type}" for w in card.warnings)
        print(f"    [{source}]{similarity} {card.name_kor}{warnings}")
    for product in response.top_products:
        source = _BADGE.get(product.match_source or "", "?")
        matched = ", ".join(product.matched_ingredients)
        print(f"    🛒 [{source}] {product.product_name} ← {matched}")
    for problem in case.problems:
        print(f"    ✗ {problem}")


def summary(cases: list[Case]) -> str:
    passed = sum(1 for c in cases if c.ok)
    return f"{passed}/{len(cases)} 통과"


def to_markdown(cases: list[Case], seed: int) -> str:
    """노션에 붙일 리포트. 실패가 있으면 그 목록이 본문의 중심이다."""
    lines = [
        f"- **실행**: {len(cases)}건 · seed `{seed}` · 실 Supabase + 실 Gemini",
        f"- **결과**: {summary(cases)}",
        "",
        "## 실행 요약",
        "",
        "| BSTI | 프로필 | 고민 | status | 성분 | 제품 | 소요 | 결과 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        context = case.context
        response = case.response
        concerns = ", ".join(CONCERN_LABEL_BY_CODE.get(c, c) for c in context.concerns)
        gender = "여성" if context.gender == "female" else "남성"
        status = case.error or (response.status if response else "-")
        ingredients = len(response.top_ingredients) if response else 0
        products = len(response.top_products) if response else 0
        lines.append(
            f"| {context.bsti_type or '미검사'} | {context.age}세 {gender} | {concerns} "
            f"| {status} | {ingredients} | {products} | {case.seconds:.1f}s "
            f"| {'통과' if case.ok else '위반 ' + str(len(case.problems))} |"
        )

    failures = [c for c in cases if not c.ok]
    lines += ["", "## 계약 위반", ""]
    if not failures:
        lines.append("없음.")
    for case in failures:
        lines.append(f"### {_profile_line(case)}")
        if case.error:
            lines.append(f"- 예외: `{case.error}`")
        lines += [f"- {problem}" for problem in case.problems]
        lines.append("")

    lines += ["", "## 출력 표본", ""]
    for case in cases[:3]:
        response = case.response
        if response is None:
            continue
        lines.append(f"### {_profile_line(case)}")
        for card in response.top_ingredients:
            source = _BADGE.get(card.match_source or "", "?")
            lines.append(f"- **[{source}] {card.name_kor}** — {card.efficacy or ''}")
        for product in response.top_products:
            matched = ", ".join(product.matched_ingredients)
            lines.append(f"- 🛒 {product.product_name} ← {matched}")
        lines.append("")
    return "\n".join(lines)
