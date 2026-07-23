"""유스케이스 검증 — 임의 프로필로 실제 파이프라인을 돌려 응답 계약을 확인한다.

단위 테스트는 각 단계를 목으로 고정하므로 "실제 데이터에서 무엇이 새어 나가는가"를
못 잡는다. 실제로 새어 나간 것들이 있었다 — BSTI 축 성분의 영어 원문, 같은 성분의
별칭 3개가 대표 카드를 독점, 프로필 하나로만 확인해 다른 15개 타입은 미검증.

여기서는 실 Supabase·실 Gemini 를 그대로 태우고 응답을 계약(`contract.py`)으로 검사한다.
프로필은 `user_profiles` 에 쓰지 않고 `UserContext` 로 주입한다(`service.run_for_context`).

    uv run python -m tests.modules.recommendations.usecase            # 16타입 + 미검사
    uv run python -m tests.modules.recommendations.usecase --runs 24  # 더 많이
    uv run python -m tests.modules.recommendations.usecase --seed 7   # 조합 재현
    uv run python -m tests.modules.recommendations.usecase --report out.md
"""
