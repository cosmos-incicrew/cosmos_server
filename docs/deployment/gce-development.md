# GCE 개발 배포 설계

## 목적과 범위

팀원이 휴대폰과 외부 네트워크에서 Cosmos API를 테스트하고, 별도 GCE VM에서
Langfuse 셀프 호스팅을 경험하기 위한 개발 환경이다. 일반 사용자 공개 환경이나
고가용성 환경은 이 문서의 범위가 아니다.

Supabase 스키마 변경과 Flutter 클라이언트 배포는 자동 배포 범위에 포함하지 않는다.

## 확정된 원칙

- GCP 프로젝트는 `kt-tech-up-01`, 개발 환경 기본 리전은 `us-central1`이다.
- Cosmos API와 Langfuse는 장애·배포·데이터 경계를 분리한 별도 VM에서 실행한다.
- 비용과 성능은 낮은 사양에서 시작해 측정 후 수직 확장한다.
- API 인프라와 Langfuse 인프라는 별도 `cosmos_infra` 저장소의 Terraform으로 관리한다.
- `main` PR 머지 후 API 이미지를 자동 배포하고 readiness 실패 시 직전 이미지로 복구한다.
- 개발 환경은 기본적으로 24시간 가동하되 필요할 때 수동 워크플로로 중지·시작한다.
- Supabase 마이그레이션은 자동 실행하지 않는다.

관련 결정:

- [ADR 0001: 애플리케이션과 관측 시스템을 별도 호스트로 운영한다](../adr/0001-separate-application-and-observability-hosts.md)
- [ADR 0002: 인프라를 별도 저장소에서 관리한다](../adr/0002-separate-infrastructure-repository.md)
- [ADR 0003: Trace에서 개인 프로필 데이터를 제외한다](../adr/0003-minimize-personal-data-in-traces.md)

## 토폴로지

| 리소스 | 초기 사양 | 책임 |
|---|---|---|
| `cosmos-api-dev` | `e2-medium`, 30GB balanced persistent disk | Caddy, FastAPI |
| `cosmos-langfuse-dev` | `e2-highmem-2`, 30GB boot + 100GB balanced data disk | Caddy, Langfuse Web/Worker, PostgreSQL, ClickHouse, Redis, MinIO |
| Artifact Registry | `us-central1` Docker 저장소 | 커밋 SHA 기반 API 이미지 |
| Secret Manager | 비밀별 IAM 부여 | Supabase, Langfuse, Kakao 및 서비스 설정 |

`e2-highmem-2`는 낮은 trace 양을 전제로 CPU 비용을 줄인 초기값이며 Langfuse 공식
권장 CPU보다 낮다. CPU 병목이나 처리 지연이 확인되면 `e2-standard-4` 이상으로
높인다. 사양은 Terraform 변수로 변경 가능하게 둔다.

개발 단계에서는 비용을 우선해 `us-central1`을 사용한다. 한국 사용자 대상 공개 전에는
실측 p50/p95 응답시간을 기준으로 API VM의 서울 `asia-northeast3` 이전을 재검토한다.

## 외부 주소와 네트워크

- API: `https://api.<API_STATIC_IP>.nip.io`
- Langfuse: `https://langfuse.<LANGFUSE_STATIC_IP>.nip.io`
- 두 주소는 개발 전용 임시 주소이며 공개 출시 전에 소유 도메인으로 교체한다.
- Caddy만 외부 `80/443` 포트를 수신하고 인증서를 자동 관리한다.
- 공개 SSH 포트는 두지 않는다. IAP TCP forwarding 경로에서만 SSH를 허용한다.
- Langfuse의 PostgreSQL, ClickHouse, Redis, MinIO 관리 포트는 외부에 공개하지 않는다.
- API VM은 VPC 내부 주소로 Langfuse ingestion endpoint에 접근한다.
- `/health`와 `/health/ready`는 공개하고 Swagger/OpenAPI 경로는 Caddy 인증으로
  개발팀에만 공개한다.

팀원의 접속 IP는 허용 목록에 사용하지 않는다. API 접근 통제는 Supabase JWT,
Langfuse UI 접근 통제는 Langfuse 계정으로 수행한다.

## IAM과 비밀 관리

GitHub Actions는 서비스 계정 JSON 키 대신 저장소와 브랜치 조건으로 제한한 Workload
Identity Federation을 사용한다.

VM에는 서비스별 전용 서비스 계정을 부여한다.

- API VM: Artifact Registry 읽기, 필요한 Secret Manager secret 읽기, Vertex AI 호출,
  로그·메트릭 쓰기
- Langfuse VM: 필요한 Secret Manager secret 읽기, 스냅샷/로그·메트릭에 필요한 권한
- GitHub 배포 주체: Artifact Registry 쓰기와 API VM 배포에 필요한 최소 권한
- Terraform 주체: 인프라 변경 전용 권한

API VM의 Gemini 호출은 VM 서비스 계정의 Application Default Credentials를 사용한다.
운영 환경에 `GOOGLE_APPLICATION_CREDENTIALS` 파일을 배포하지 않는다. Supabase
service-role 키와 Langfuse secret 키는 클라이언트나 Docker 이미지에 포함하지 않는다.

## API CI/CD

Pull request CI:

1. 잠금 파일 기반 의존성 설치
2. `LANGFUSE_TRACING_ENABLED=false`로 테스트 실행
3. Ruff lint
4. mypy
5. import-linter
6. Docker 이미지 빌드 검증

`main` 머지 후 CD:

1. 동일 품질 게이트 재확인
2. Git 커밋 SHA로 Docker 이미지 빌드
3. Artifact Registry에 immutable SHA tag와 digest 게시
4. 현재 실행 digest를 rollback 대상으로 기록
5. API VM에 새 digest 배포
6. `/health`와 `/health/ready` 확인
7. 실패 시 직전 digest로 복구하고 워크플로 실패 처리

GitHub Actions에 Supabase DB 비밀번호나 service-role 키를 제공하지 않으며 DB
마이그레이션은 실행하지 않는다.

## Langfuse 운영

- 공식 Langfuse v3 Docker Compose 구조를 기반으로 버전을 고정한다.
- Langfuse 업데이트는 API 배포와 분리한 수동 승인 워크플로로 실행한다.
- 최초 관리자, 조직, 프로젝트와 API 키는 headless initialization으로 생성한다.
- 팀원은 개별 이메일/비밀번호 계정을 사용한다.
- 초기 가입 기간 이후 `AUTH_DISABLE_SIGNUP=true`로 신규 가입을 닫는다.
- SMTP와 SSO는 초기 범위에서 제외한다.
- trace 보존 기간은 30일이다. Langfuse OSS에는 내장 retention 기능이 없으므로
  Public API 기반 일일 정리 작업으로 적용한다.

API 프로세스는 Langfuse 장애 때문에 사용자 요청을 실패시키지 않는다. FastAPI 종료
시 Langfuse SDK를 shutdown해 대기 중인 trace를 전송하고, CI에서는 trace 전송을
비활성화한다.

Trace에는 정제된 프롬프트와 모델 응답을 저장할 수 있지만 사용자 ID, 나이, 성별,
임신·수유 여부와 보유 제품은 저장하지 않는다. 실제 사용자 ID 대신 요청 단위 trace
ID를 사용한다.

## 백업과 복구

- Langfuse 데이터는 별도 100GB persistent disk에 저장한다.
- 데이터 디스크를 매일 스냅샷하고 7일간 보관한다.
- 개발 단계 복구 목표는 최대 24시간의 trace 유실 허용이다.
- VM과 설정은 Terraform, startup 구성, Secret Manager로 재생성한다.
- 공개 운영 전에는 PostgreSQL, ClickHouse, 오브젝트 스토리지별 백업과 복구 훈련으로
  강화한다.

## 수동 시작과 중지

GitHub Actions의 수동 워크플로에서 전체 개발 환경을 시작하거나 중지한다.

- 시작: Langfuse VM → Langfuse readiness 확인 → API VM → API readiness 확인
- 중지: API VM → Langfuse SDK 종료 대기 → Langfuse VM

VM을 중지하면 컴퓨팅 비용은 멈추지만 persistent disk와 예약 고정 IP 비용은 계속
발생한다.

## 관측과 비용 조정

- Ops Agent가 애플리케이션·컨테이너 로그와 호스트 CPU, 메모리, 디스크 메트릭을
  Cloud Logging·Monitoring으로 전송한다.
- 알림 수신 채널이 확정되면 디스크 70%, 메모리 80%, 반복적인 readiness 실패를
  초기 경보 기준으로 추가한다. 초기 배포 범위에서는 대시보드로 확인한다.
- 한 달간 사용량을 측정한 뒤 VM 사양, 디스크 크기, 스냅샷 보존과 가동 시간을
  조정한다.

## 초기 범위에서 제외

- Managed Instance Group과 다중 VM 고가용성
- 외부 HTTPS Load Balancer와 Cloud Armor
- 정식 도메인
- Supabase 마이그레이션 자동화
- Langfuse SSO, SMTP, 고가용성, 서비스별 논리 백업
- Flutter 클라이언트 배포 및 Supabase 액세스 토큰 주입
