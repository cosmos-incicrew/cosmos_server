"""추천 파이프라인 end-to-end 데모 러너 (로컬 확인용).

라우터·HTTP·JWT 없이 서비스 진입점 `create_recommendations(user_id)` 를 직접 불러
①~⑦ 전 단계를 실행하고 결과를 출력한다. 실 Supabase·실 Gemini 를 쓰며 읽기 전용이다
(추천은 DB 에 쓰지 않는다). ③ 검색은 gemini-embedding-001 벡터 검색(match_rec_* RPC)이다.

    uv run python scripts/demo_recommendations.py            # 프로필 목록에서 자동 선택
    uv run python scripts/demo_recommendations.py <user_id>  # 특정 유저로 실행

VSCode 에서는 실행/디버그 패널의 "추천 데모 실행" 구성으로 F5 로 돌릴 수 있다
(.vscode/launch.json). 주의: 운영 DB 를 가리키는 .env 로는 돌리지 말 것.
"""

import asyncio
import sys
import time
from pathlib import Path

# scripts/ 밖(루트)에서 app 을 import 하려면 루트를 경로에 넣는다 (dev_token.py 와 동일).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.supabase import get_supabase  # noqa: E402
from app.modules.recommendations.service import create_recommendations  # noqa: E402


async def _pick_user_id() -> str | None:
    """인자가 없을 때 프로필 목록을 보여주고 온보딩 완료된 첫 유저를 고른다."""
    client = await get_supabase()
    res = await (
        client.table("user_profiles")
        .select("user_id, age, gender, skin_concerns, bsti_type")
        .limit(10)
        .execute()
    )
    profiles = res.data or []
    if not profiles:
        print("user_profiles 가 비어 있다. 온보딩된 유저가 필요하다.")
        return None

    print("--- 사용 가능한 프로필 ---")
    chosen: str | None = None
    for p in profiles:
        ready = bool(p.get("age")) and bool(p.get("skin_concerns"))  # 없으면 409
        mark = "✓" if ready else "✗(온보딩 미완)"
        print(
            f"  {mark} {p['user_id']}  age={p.get('age')} "
            f"concerns={p.get('skin_concerns')} bsti={p.get('bsti_type')}"
        )
        if ready and chosen is None:
            chosen = p["user_id"]
    print()
    return chosen


async def run(user_id: str) -> None:
    print(f"user_id = {user_id}\n요청 중... (실 Gemini 호출 — 십수 초 걸릴 수 있다)\n")
    t0 = time.monotonic()
    resp = await create_recommendations(user_id)
    dt = time.monotonic() - t0

    data = resp.model_dump()
    print("=" * 70)
    print(
        f"전체 소요: {dt:.1f}s   STATUS: {data.get('status')}   "
        f"mode={data.get('retrieval_mode')}"
    )
    print(f"advisory: {data.get('advisory')}")
    print("=" * 70)
    ans = data.get("answer")
    if ans:
        print("① 원인 분석\n" + ans["cause_analysis"])
        print("\n② 추천 성분과 근거\n" + ans["recommendation"])
        print("\n③ 사용법·관리법\n" + ans["usage_guide"])
    else:
        print("(근거 부족 — answer 없음)")
    print("\n--- 📂 근거: 유사 케이스", len(data.get("cases", [])),
          "· 성분", len(data.get("ingredients", [])), "---")
    for c in data.get("cases", []):
        rec = ", ".join(c.get("recommended_ingredients", [])[:3])
        print(
            f"  [케이스] {c['target_concern']} · {c['gender']} {c['age']}세 "
            f"{c['skin_type']} · 유사도 {c['similarity']} · 추천성분: {rec}"
        )
    for i in data.get("ingredients", []):
        line = f"  [성분] {i['name_kor']} ({i['inci']}) · 유사도 {i['similarity']}"
        if i.get("safety_note"):
            line += f" · 주의: {i['safety_note']}"
        for w in i.get("warnings", []):
            line += f" · ⚠{w['type']}"
        print(line)

    products = data.get("products", [])
    print(f"\n--- 🛒 추천 제품 {len(products)}개 (추천 성분 함유) ---")
    for p in products:
        matched = ", ".join(p.get("matched_ingredients", []))
        brand = p.get("brand") or "브랜드미상"
        cat = f" · {p['main_category']}" if p.get("main_category") else ""
        print(f"  [{brand}] {p['product_name']}{cat}")
        print(f"      매칭 성분: {matched}")
        if p.get("product_url"):
            print(f"      {p['product_url']}")

    print("\n사용 프로필:", data.get("user_profile"))


async def main() -> None:
    user_id = sys.argv[1] if len(sys.argv) > 1 else await _pick_user_id()
    if not user_id:
        print("실행할 user_id 를 정하지 못했다. 인자로 넘기거나 프로필을 먼저 만든다.")
        raise SystemExit(1)
    await run(user_id)


if __name__ == "__main__":
    asyncio.run(main())
