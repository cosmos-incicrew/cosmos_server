---
status: accepted
---

# 인프라를 별도 저장소에서 관리한다

GCP 기반 인프라와 셀프 호스팅 Langfuse 설정은 별도 `cosmos_infra` 저장소에서
관리한다. 소규모 팀에서는 애플리케이션 저장소에 함께 두는 편이 단순하지만, Cosmos
API와 관측 시스템은 권한, 배포 주기, 데이터 수명주기가 다르므로 `cosmos_server`에는
애플리케이션 이미지와 CI/CD를, `cosmos_infra`에는 Terraform과 Langfuse·VM 부트스트랩
설정을 두는 경계를 선택한다.

## Consequences

- 인프라 변경과 애플리케이션 변경을 독립적으로 검토하고 권한을 제한할 수 있다.
- `cosmos_server`의 배포 워크플로는 커밋 SHA 이미지의 빌드와 API VM 배포만 담당한다.
- 두 저장소 사이의 이미지 이름, 환경 이름, 배포 권한 계약을 문서화해야 한다.
