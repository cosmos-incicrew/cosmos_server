"""임의 프로필 생성 → 실제 파이프라인 실행 → 계약 검사 → 리포트.

프로필은 `user_profiles` 에 쓰지 않는다. 검증하려고 운영 데이터를 만들 이유가 없고,
지우는 것을 잊으면 다음 사람의 실행 결과가 조용히 달라진다.
"""

import random
import time
from dataclasses import dataclass, field

from app.common.skin_concerns import CONCERN_CODES
from app.modules.recommendations import service
from app.modules.recommendations.bsti_ingredients import BSTI_RECOMMENDED, recommended_for
from app.modules.recommendations.constants import MAX_CONCERNS
from app.modules.recommendations.schemas import RecommendationResponse, UserContext
from tests.modules.recommendations.usecase import contract

BSTI_TYPES: tuple[str, ...] = tuple(sorted(BSTI_RECOMMENDED))

# 나이는 서비스 대상대로 넓게 흔든다. 질의 문장에만 쓰여 경계값이 따로 없다.
_AGE_RANGE = (19, 59)
_GENDERS = ("female", "male")


@dataclass
class Case:
    """유스케이스 한 건의 실행 결과."""

    context: UserContext
    response: RecommendationResponse | None = None
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and not self.problems


def build_contexts(runs: int, seed: int) -> list[UserContext]:
    """16타입을 먼저 한 번씩 채우고 남는 횟수를 임의 타입으로 채운다.

    무작위로만 뽑으면 16타입 중 몇 개는 한 번도 안 나온다 — 그러면 "타입별 검증"이라는
    목적을 못 이룬다. BSTI 미검사(None) 경로도 한 건 넣는다: 그 경로는 ⑦ 가지가 통째로
    비어 ⑧이 고민 축만으로 대표를 채우는 다른 흐름이다.
    """
    rng = random.Random(seed)
    types: list[str | None] = [*BSTI_TYPES, None]
    while len(types) < runs:
        types.append(rng.choice(BSTI_TYPES))
    return [_context(rng, index, code) for index, code in enumerate(types[:runs])]


def _context(rng: random.Random, index: int, bsti_type: str | None) -> UserContext:
    concerns = rng.sample(list(CONCERN_CODES), rng.randint(1, MAX_CONCERNS))
    return UserContext(
        # 실 사용자와 섞이지 않게 표식을 남긴다. 쓰기를 하지 않아 DB 에는 남지 않는다.
        user_id=f"usecase-{index:02d}-{bsti_type or 'none'}",
        age=rng.randint(*_AGE_RANGE),
        gender=rng.choice(_GENDERS),
        bsti_type=bsti_type,
        bsti_recommended=recommended_for(bsti_type),
        concerns=concerns,
    )


async def run_case(context: UserContext) -> Case:
    """한 건 실행. 예외도 결과로 담는다 — 한 건이 터져도 나머지를 계속 본다."""
    case = Case(context=context)
    started = time.monotonic()
    try:
        case.response = await service.run_for_context(context)
        case.problems = contract.check(case.response, context)
    except Exception as exc:  # noqa: BLE001 — 유스케이스 러너라 종류를 가리지 않고 기록한다
        case.error = f"{type(exc).__name__}: {exc}"
    case.seconds = time.monotonic() - started
    return case


async def run_all(runs: int, seed: int) -> list[Case]:
    """순차 실행한다 — 동시에 띄우면 Gemini 쿼터에 걸려 실패가 계약 위반처럼 보인다."""
    return [await run_case(context) for context in build_contexts(runs, seed)]
