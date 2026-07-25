from pathlib import Path

WORKFLOWS = Path(".github/workflows")


def read_workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_ci_publishes_main_image_without_waiting_for_deployment_approval() -> None:
    workflow = read_workflow("ci.yml")

    assert "name: Backend CI" in workflow
    assert "docker push" in workflow
    assert "environment: development" not in workflow
    assert "Deploy and verify" not in workflow


def test_deployment_supports_automatic_and_explicit_runs() -> None:
    workflow = read_workflow("deploy-development.yml")

    assert "workflow_run:" in workflow
    assert 'workflows: ["Backend CI"]' in workflow
    assert "workflow_dispatch:" in workflow
    assert "revision:" in workflow


def test_deployment_serializes_changes_and_skips_stale_automatic_runs() -> None:
    workflow = read_workflow("deploy-development.yml")

    assert "group: cosmos-api-development" in workflow
    assert "cancel-in-progress: false" in workflow
    assert 'should_deploy="false"' in workflow
    assert "outdated automatic deployment" in workflow


def test_manual_revision_is_restricted_to_a_published_main_commit() -> None:
    workflow = read_workflow("deploy-development.yml")

    assert "git merge-base --is-ancestor" in workflow
    assert "gcloud artifacts docker images describe" in workflow
    assert "/opt/cosmos/bin/deploy-api" in workflow
