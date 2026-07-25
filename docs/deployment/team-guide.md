# 개발 백엔드 배포·사용 가이드

이 문서는 Cosmos 팀원이 개발 API를 사용하고, 백엔드 변경을 안전하게 배포하기 위한
실무 안내서입니다. 인프라를 처음 만드는 절차나 VM 사양을 바꾸는 방법은
[`cosmos_infra`](https://github.com/cosmos-incicrew/cosmos_infra)의 README를
참고하세요.

## 먼저 알아둘 것

| 항목 | 개발 환경 |
|---|---|
| API | `https://api.35-255-31-62.nip.io` |
| Swagger | `https://api.35-255-31-62.nip.io/docs` |
| ReDoc | `https://api.35-255-31-62.nip.io/redoc` |
| OpenAPI JSON | `https://api.35-255-31-62.nip.io/openapi.json` |
| Langfuse | `https://langfuse.35-188-208-32.nip.io` |
| Flutter Web | `https://cosmos-incicrew.vercel.app` |
| 배포 대상 | GCE `cosmos-api-dev`, `us-central1-a` |

이 주소는 팀 내부 개발·테스트용입니다. 고정 IP를 사용하므로 노트북이나 휴대폰의
접속 네트워크가 바뀌어도 주소는 유지됩니다.

API 문서에는 Caddy Basic Auth가 적용되어 있습니다. 사용자명은 `cosmos`이고,
비밀번호는 팀의 안전한 비밀 공유 채널에서 받습니다. 이 인증은 문서 노출을 막기 위한
것이며, 실제 API의 사용자 인증과는 별개입니다.

브라우저 CORS는 정식 Vercel 주소와 로컬 Flutter Web 주소
`http://localhost:3000`만 허용합니다. 새 프론트 도메인을 추가할 때는 백엔드의
`CORS_ALLOWED_ORIGINS` 설정과 테스트를 함께 변경합니다.

## 가장 자주 쓰는 흐름

일반적인 백엔드 변경은 다음 순서로 배포됩니다.

1. 기능 브랜치에서 코드를 수정하고 로컬 검증을 실행합니다.
2. `main`을 대상으로 PR을 엽니다.
3. GitHub Actions의 `Quality gate`가 모두 통과한 것을 확인합니다.
4. 리뷰 후 PR을 `main`에 머지합니다.
5. `Backend CI`가 같은 품질 검증을 다시 실행하고 커밋 SHA 이미지를 게시합니다.
6. CI가 성공하면 `Deploy development`가 최신 `main` 이미지를 자동 배포합니다.
7. 아래 명령으로 외부 주소까지 정상인지 확인합니다.

팀원은 평소에 GCP 콘솔이나 VM에 직접 접속할 필요가 없습니다. `main`에 직접
커밋하거나 로컬에서 VM으로 수동 배포하지 않습니다.

## PR을 열기 전

저장소 루트에서 다음 검증을 모두 통과시킵니다.

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy
uv run lint-imports
docker build --tag cosmos-server:local .
```

DB 변경이 포함되어 있다면 PR 설명에서 별도로 표시합니다. 현재 자동 배포는
FastAPI 컨테이너만 교체하며 `supabase/migrations/`의 migration은 실행하지 않습니다.
백엔드 코드가 새 스키마를 먼저 요구하면 API 배포 직후 장애가 날 수 있으므로,
DB 적용 순서와 담당자를 PR에서 먼저 합의해야 합니다.

## 머지하면 어떤 일이 일어나는가

`.github/workflows/ci.yml`은 `main`에 들어간 정확한 커밋으로 Docker 이미지를
만듭니다. 이미지는 Artifact Registry에 커밋 SHA로 올라갑니다.

CI가 성공하면 `.github/workflows/deploy-development.yml`이 해당 이미지를 변경되지 않는
digest 형식으로 API VM에 자동 배포합니다. 여러 커밋이 빠르게 들어와도 실제 배포는
한 번에 하나만 실행하며, 이미 최신 `main`이 존재하면 오래된 자동 배포는 건너뜁니다.

`development` Environment에는 required reviewer를 두지 않습니다. `main` 보호와 CI
통과가 자동 배포 조건이며, 사람이 명시적으로 개입할 때는 아래의 `Run workflow`를
사용합니다. required reviewer를 다시 설정하면 자동 배포가 승인 대기 상태에 머물 수
있습니다.

VM의 배포 스크립트는 다음 작업을 수행합니다.

1. Artifact Registry에서 새 이미지를 받습니다.
2. Secret Manager의 최신 값을 런타임 환경변수로 만듭니다.
3. API 컨테이너를 새 이미지로 교체합니다.
4. `/health/ready`가 정상인지 기다립니다.
5. 준비 상태가 실패하면 직전 정상 이미지로 되돌립니다.

이미지에는 Supabase service-role 키, Kakao Admin 키, Langfuse secret 키가 들어가지
않습니다. GitHub Actions에도 이 값을 등록하지 않습니다.

## 자동 배포를 다시 실행하거나 이전 버전으로 복구하기

자동 배포가 누락되었거나 같은 버전을 다시 배포해야 할 때는 GitHub의
`Actions → Deploy development → Run workflow`를 사용합니다.

- `revision`을 비우면 현재 `main` 최신 커밋을 배포합니다.
- 특정 커밋을 배포하려면 `main`에 포함된 전체 commit SHA를 입력합니다.
- Artifact Registry에 해당 SHA 이미지가 없거나 `main` 이력에 없는 커밋이면 배포하지
  않고 실패합니다.

자동 배포와 수동 배포는 같은 스크립트와 직렬화 규칙을 사용합니다. 실행 중인 배포를
강제로 취소하지 않으며, 동시에 VM을 변경하지 않습니다. 일반 팀원은 VM에 직접
접속하거나 이미지 digest를 복사할 필요가 없습니다.

## 배포가 끝난 뒤 확인하기

```bash
API_URL=https://api.35-255-31-62.nip.io

curl --fail "$API_URL/health"
curl --fail "$API_URL/health/ready"
```

정상 응답은 각각 다음과 같습니다.

```json
{"status":"ok"}
{"status":"ready"}
```

- `/health` 실패: Caddy, VM 또는 API 컨테이너가 기동하지 않은 상태일 수 있습니다.
- `/health`는 성공하고 `/health/ready`만 실패: Supabase 연결이나 런타임 Secret을
  우선 확인합니다.
- 둘 다 성공하지만 특정 API만 실패: Swagger에서 요청 형식과 인증 여부를 확인하고,
  GitHub Actions 및 컨테이너 로그를 확인합니다.

## API 문서와 실제 API 인증

Swagger에 들어갈 때 나타나는 Basic Auth 창에는 팀 문서 계정을 사용합니다. Swagger
오른쪽 위의 `Authorize`에서 입력하는 값은 Supabase access token입니다. 서로 다른
인증이므로 혼동하지 않도록 주의합니다.

인증이 필요한 API는 다음과 같이 호출합니다.

```bash
API_URL=https://api.35-255-31-62.nip.io
SUPABASE_ACCESS_TOKEN='로그인 후 받은 access token'

curl --fail --get "$API_URL/api/v1/products/search" \
  --data-urlencode 'q=크림' \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN"
```

토큰을 저장소, 이슈, PR, 메신저 일반 채널에 붙여 넣지 않습니다. 현재 서버가 제공하는
경로와 스키마는 Swagger 또는 `/openapi.json`을 최종 기준으로 삼습니다.

## 배포 실패를 확인하는 순서

1. GitHub의 `Actions → Backend CI`에서 실패한 job과 step을 확인합니다.
2. `Quality gate` 실패라면 코드나 테스트를 수정한 새 PR을 만듭니다.
3. 이미지 게시까지 성공했다면 `Actions → Deploy development`에서 자동 배포 실행을
   확인합니다.
4. 자동 실행이 누락되었으면 `Run workflow`에서 `revision`을 비워 최신 `main`을
   명시적으로 다시 배포합니다.
5. 배포 step이 실패했어도 외부 `/health`와 `/health/ready`를 확인합니다. readiness
   실패 시 스크립트가 직전 이미지로 복구했을 수 있습니다.
6. 복구 후에도 API가 비정상이면 인프라 담당자가 IAP로 VM 로그를 확인합니다.

인프라 담당자가 확인할 때 사용하는 명령은 다음과 같습니다.

```bash
gcloud compute ssh cosmos-api-dev \
  --project=kt-tech-up-01 \
  --zone=us-central1-a \
  --tunnel-through-iap

sudo docker compose \
  --env-file /opt/cosmos/release.env \
  -f /opt/cosmos/compose.yml ps

sudo docker compose \
  --env-file /opt/cosmos/release.env \
  -f /opt/cosmos/compose.yml logs --tail=200 api
```

수동 workflow에서도 커밋 SHA를 실제 이미지 digest로 변환해 배포합니다. 이전 커밋
복구는 자동 복구가 실패했거나 최신 변경 자체에 문제가 있다고 확인된 경우에만
사용합니다.

## 개발 환경이 꺼져 있을 때

비용 절감을 위해 VM을 수동으로 정지할 수 있습니다. 두 health endpoint가 모두
응답하지 않는다면 먼저 `cosmos_infra`의 `Development environment control`
워크플로에서 상태를 확인합니다.

- `status`: 현재 상태만 확인
- `start`: Langfuse가 준비된 뒤 API를 시작
- `stop`: API를 먼저 정지한 뒤 Langfuse를 정지

일반 팀원이 VM을 직접 시작하거나 정지할 필요는 없습니다. 필요한 경우 인프라
담당자에게 워크플로 실행을 요청합니다.

## Langfuse에서 trace 확인하기

Langfuse 주소에서 개인 팀 계정으로 로그인한 뒤 프로젝트의 Traces 메뉴를 확인합니다.
개발 서버는 `development` 환경과 배포 이미지 digest를 trace에 기록합니다.

Langfuse 장애는 API 요청 자체를 실패시키지 않도록 구성되어 있습니다. 추천 응답은
정상인데 trace만 보이지 않는다면 API를 다시 배포하기 전에 Langfuse health와 프로젝트
키 설정을 먼저 확인합니다.
