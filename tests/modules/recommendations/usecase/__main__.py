"""유스케이스 러너 진입점.

    uv run python -m tests.modules.recommendations.usecase [--runs N] [--seed N] [--report PATH]

실 Gemini 를 건당 1회 부른다 — 기본 17건이면 수 분·실비용이 든다. pytest 로 자동 수집되지
않게 파일명을 `test_` 로 두지 않았다(CI 가 매번 과금하면 안 된다).
"""

import argparse
import asyncio
import sys
from pathlib import Path

from tests.modules.recommendations.usecase import relevance, report, runner

_DEFAULT_RUNS = 17  # BSTI 16타입 + 미검사 1


async def _main() -> int:
    parser = argparse.ArgumentParser(description="추천 응답 계약 유스케이스 검증")
    parser.add_argument("--runs", type=int, default=_DEFAULT_RUNS)
    parser.add_argument("--seed", type=int, default=20260723)
    parser.add_argument("--report", type=Path, help="마크다운 리포트 저장 경로")
    args = parser.parse_args()

    print(f"유스케이스 {args.runs}건 · seed {args.seed} · 실 Gemini 호출\n")
    cases = await runner.run_all(args.runs, args.seed)
    for case in cases:
        report.print_case(case)

    sets = await relevance.expert_sets()
    verdicts = [v for case in cases for v in relevance.judge(case, sets)]
    ungrounded = [v for v in verdicts if not v.grounded]

    print(f"\n{'=' * 70}\n계약: {report.summary(cases)}")
    print(f"적합성: {relevance.summarize(verdicts)}")
    for verdict in ungrounded:
        print(f"  ? {verdict.name} — 고민 {verdict.concerns} 에 대한 근거를 못 찾음")
    if args.report:
        markdown = report.to_markdown(cases, args.seed)
        markdown += "\n\n## 적합성\n\n- " + relevance.summarize(verdicts)
        markdown += "\n" + "\n".join(
            f"- 근거 못 찾음: **{v.name}** (고민 {', '.join(v.concerns)})" for v in ungrounded
        )
        args.report.write_text(markdown, encoding="utf-8")
        print(f"리포트: {args.report}")
    # 계약 위반이 있으면 종료 코드로 알린다 — 눈으로 스크롤하다 놓치지 않게.
    return 0 if all(case.ok for case in cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
