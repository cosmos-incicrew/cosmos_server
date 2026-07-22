from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from app.common.skin_concerns import CONCERN_CODES

MAX_NICKNAME_LENGTH = 20
# 만 나이 상한. 오타(999)나 생년 입력을 걸러내는 용도.
MAX_AGE = 120

Gender = Literal["female", "male", "other"]

# BSTI 16타입 = 4축 조합. O/D(유수분) S/R(민감) P/N(색소) W/T(주름).
# DB의 user_profiles_bsti_type_check 제약과 같은 규칙 — 한쪽만 고치면 500이 난다.
BSTI_TYPE_PATTERN = r"^[OD][SR][PN][WT]$"


class ProfileUpsertRequest(BaseModel):
    """온보딩 프로필 저장 요청 (명세서 B-2). 수정도 같은 엔드포인트로 전체 덮어쓴다."""

    # 온보딩 화면이 닉네임 입력을 강제하지 않는다. 필수로 두면 나이·고민만 채운
    # 사용자가 저장에 실패해 온보딩을 영영 못 끝낸다 — 추천에 닉네임은 안 쓴다.
    nickname: Annotated[str, Field(min_length=1, max_length=MAX_NICKNAME_LENGTH)] | None = None
    age: Annotated[int, Field(ge=1, le=MAX_AGE)] | None = None
    gender: Gender | None = None
    skin_concerns: list[str] = Field(default_factory=list)
    is_pregnant: bool | None = None
    is_nursing: bool | None = None
    # BSTI 는 검사 시점이 프로필 저장과 달라, 다른 항목처럼 덮어쓰지 않는다.
    # null(미포함)이면 저장 시 **기존 값을 유지**한다 — repository.upsert_profile 참고.
    # 닉네임만 고치는 마이페이지 저장에 BSTI 가 지워지면 안 되기 때문이다.
    bsti_type: Annotated[str, Field(pattern=BSTI_TYPE_PATTERN)] | None = None

    @field_validator("skin_concerns")
    @classmethod
    def _known_codes_only(cls, value: list[str]) -> list[str]:
        unknown = [code for code in value if code not in CONCERN_CODES]
        if unknown:
            raise ValueError(f"알 수 없는 피부 고민 코드: {', '.join(unknown)}")
        return list(dict.fromkeys(value))


class ProfileResponse(BaseModel):
    user_id: str
    nickname: str | None
    age: int | None
    gender: Gender | None
    skin_concerns: list[str]
    is_pregnant: bool | None
    is_nursing: bool | None
    bsti_type: str | None = None
    created_at: str | None
    updated_at: str | None
