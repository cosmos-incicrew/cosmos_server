"""AI Hub Validation(held-out) → 추천 평가 데이터셋 JSON.

정답 성분은 `load_recommendations_data.extract_recommended` 를 그대로 재사용한다 —
rec_cases 적재와 **같은 규칙**으로 뽑아야 인덱스(Training)와 평가(Validation)의
성분 표기가 어긋나지 않는다. 정답은 영문 INCI 가 아니라 한글 성분명이다(answer 의
대문자 INCI 추출은 65% 케이스가 0개라 폐기 — 상담문이 성분을 한글로 서술한다).

Validation 은 인덱스 적재 경로(Training/02.라벨링데이터)에 포함되지 않아 held-out 이다.
"""

from __future__ import annotations

import argparse
import json
import random
import zipfile
from pathlib import Path
from typing import Any

from scripts.load_recommendations_data import (
    build_name_matcher,
    extract_recommended,
    read_ingredients,
)

DEFAULT_SRC = Path.home() / "mkim/실무/data/03.스킨케어 성분-효능 추천 데이터/3.개방데이터"
VAL_SUBDIR = "2.데이터(NIA)/Validation/02.라벨링데이터"
DEFAULT_OUTPUT = Path("evaluation/recommendations/datasets/held-out-v1.0.0.json")
DEFAULT_SEED = 20260724
DEFAULT_SAMPLE = 100

# Validation target_concern(한글, 슬래시 표기) → 파이프라인 고민 enum
# (constants.CONCERN_SEARCH_KEYWORDS 키).
CONCERN_MAP: dict[str, str] = {
    "모공": "pores",
    "미백(색소침착/기미/칙칙함)": "brightening",
    "주름": "wrinkles",
    "여드름/뾰루지": "acne",
    "붉어짐(홍조)": "redness",
    "과각질/악건성": "dryness",
    "피부처짐/탄력저하": "sagging",
    "민감성(트러블/자극감)": "sensitivity",
}


def _load_validation_cases(src: Path) -> list[dict[str, Any]]:
    val_dir = src / VAL_SUBDIR
    zips = sorted(val_dir.glob("*.zip"))
    if not zips:
        raise SystemExit(f"Validation zip 없음: {val_dir}")
    cases: list[dict[str, Any]] = []
    for zp in zips:
        with zipfile.ZipFile(zp) as zf:
            for name in zf.namelist():
                if not name.endswith(".jsonl"):
                    continue
                for line in zf.read(name).decode("utf-8").splitlines():
                    if line.strip():
                        cases.append(json.loads(line))
    return cases


def build_dataset(src: Path, seed: int, sample: int) -> dict[str, Any]:
    matcher = build_name_matcher(read_ingredients(src))
    raw = _load_validation_cases(src)
    built: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in raw:
        info, meta = rec.get("info", {}), rec.get("meta", {})
        concern_kor = info.get("target_concern", "")
        concern = CONCERN_MAP.get(concern_kor)
        case_id = info.get("id")
        if not (concern and case_id and info.get("answer")) or case_id in seen:
            continue
        gold = extract_recommended(matcher, info["answer"], rec.get("chain_of_thought", []))
        if not gold:  # 정답 0개는 recall 분모가 없어 제외 (전체의 1.7%)
            continue
        seen.add(case_id)
        built.append(
            {
                "case_id": case_id,
                "age": meta.get("age") or None,
                "gender": meta.get("gender") or None,
                "concern": concern,
                "target_concern_kor": concern_kor,
                "gold": gold,
            }
        )
    random.Random(seed).shuffle(built)
    return {
        "dataset_version": DEFAULT_OUTPUT.stem.split("-")[-1],
        "dataset_kind": "recommendations-recall",
        "sampling_seed": seed,
        "source": "AI Hub 71886 Validation (held-out)",
        "cases": built[:sample],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="추천 평가 데이터셋 생성 (Validation held-out)")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dataset = build_dataset(args.src, args.seed, args.sample)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(dataset['cases'])}건 → {args.output}")


if __name__ == "__main__":
    main()
