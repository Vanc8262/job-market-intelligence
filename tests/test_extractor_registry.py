"""Tests for configuration-driven extractor construction."""

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from extractors import (
    AshbyExtractor,
    ExtractorRegistry,
    ExtractorRegistryError,
    GreenhouseExtractor,
    HandshakeAuthorizationError,
    JobicyExtractor,
    LeverExtractor,
    NextEraExtractor,
    PhenomExtractor,
    SmartRecruitersExtractor,
    USAJobsAuthorizationError,
    USAJobsExtractor,
    WorkableExtractor,
    WorkdayExtractor,
)
from storage import BronzeStorage
from utils.checkpoint import CheckpointManager
from utils.config import Config, ExtractorTargetsConfig, SourcesConfig


def configured_targets() -> ExtractorTargetsConfig:
    """Return one enabled target for every supported extractor plugin."""
    return ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "acme-greenhouse",
                    "source": "greenhouse",
                    "company_name": "Acme, Inc.",
                    "board_token": "acme",
                },
                {
                    "name": "acme-lever",
                    "source": "lever",
                    "company_name": "Acme, Inc.",
                    "site": "acme",
                },
                {
                    "name": "acme-ashby",
                    "source": "ashby",
                    "company_name": "Acme, Inc.",
                    "job_board_name": "Acme",
                    "include_unlisted": True,
                },
                {
                    "name": "acme-workday",
                    "source": "workday",
                    "company_name": "Acme, Inc.",
                    "host": "https://acme.wd5.myworkdayjobs.com",
                    "tenant": "acme",
                    "site": "External",
                    "page_size": 10,
                },
                {
                    "name": "acme-nextera",
                    "source": "nextera",
                    "company_name": "Acme Energy",
                    "host": "https://jobs.example-energy.com",
                    "site": "acme-energy",
                },
                {
                    "name": "acme-phenom",
                    "source": "phenom",
                    "company_name": "Acme, Inc.",
                    "host": "https://careers.acme.example",
                    "tenant": "ACMEUS",
                    "site": "acme",
                    "locale": "en_us",
                    "path_prefix": "/us/en",
                },
                {
                    "name": "acme-smartrecruiters",
                    "source": "smartrecruiters",
                    "company_name": "Acme, Inc.",
                    "company_identifier": "acme",
                    "page_size": 50,
                },
                {
                    "name": "acme-workable",
                    "source": "workable",
                    "company_name": "Acme, Inc.",
                    "account_subdomain": "acme",
                },
                {
                    "name": "jobicy-test",
                    "source": "jobicy",
                    "count": 100,
                    "geo": "usa",
                    "cache_ttl_minutes": 120,
                },
                {
                    "name": "federal-usajobs",
                    "source": "usajobs",
                    "api_key_env": "TEST_USAJOBS_API_KEY",
                    "user_agent_email_env": "TEST_USAJOBS_USER_AGENT_EMAIL",
                    "results_per_page": 50,
                    "max_pages": 2,
                },
            ]
        }
    )


def build_registry(tmp_path: Path, targets: ExtractorTargetsConfig) -> ExtractorRegistry:
    """Build a registry with isolated filesystem dependencies."""
    return ExtractorRegistry(
        targets=targets,
        bronze_storage=BronzeStorage(tmp_path / "bronze"),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )


def test_default_configuration_documents_all_target_shapes() -> None:
    targets = Config().extractor_targets.targets

    assert {target.source for target in targets} == {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "nextera",
        "phenom",
        "usajobs",
        "jobicy",
    }
    assert [target.name for target in targets if target.enabled] == [
        "jobicy-remote-us",
        "federal-usajobs",
        "palantir-lever",
        "modmed-workday",
        "nextera-energy",
        "chewy-phenom",
        "cloudflare-greenhouse",
        "datadog-greenhouse",
        "ramp-ashby",
        "workday-careers",
        "chime-greenhouse",
        "spotify-lever",
        "owner-ashby",
    ]
    targets_by_name = {target.name: target.model_dump() for target in targets}
    assert targets_by_name["jobicy-remote-us"]["enabled"] is True
    assert targets_by_name["jobicy-remote-us"]["count"] == 200
    assert targets_by_name["jobicy-remote-us"]["geo"] == "usa"
    assert targets_by_name["jobicy-remote-us"]["cache_ttl_minutes"] == 60
    assert targets_by_name["federal-usajobs"]["enabled"] is True
    assert targets_by_name["federal-usajobs"]["results_per_page"] == 100
    assert targets_by_name["federal-usajobs"]["max_pages"] == 3
    assert targets_by_name["palantir-lever"]["site"] == "palantir"
    assert (
        targets_by_name["modmed-workday"]["host"]
        == "https://modmed.wd501.myworkdayjobs.com"
    )
    assert targets_by_name["modmed-workday"]["tenant"] == "modmed"
    assert targets_by_name["modmed-workday"]["site"] == "ModMed12"
    assert targets_by_name["nextera-energy"]["host"] == "https://jobs.nexteraenergy.com"
    assert targets_by_name["nextera-energy"]["site"] == "nextera-energy"
    assert targets_by_name["chewy-phenom"]["tenant"] == "CHINUS"
    assert targets_by_name["chewy-phenom"]["site"] == "chewy"
    assert targets_by_name["cloudflare-greenhouse"]["board_token"] == "cloudflare"
    assert targets_by_name["datadog-greenhouse"]["board_token"] == "datadog"
    assert targets_by_name["ramp-ashby"]["job_board_name"] == "ramp"

    registry = ExtractorRegistry()
    assert registry.access_policy_for("palantir-lever").value == "public_api"
    assert registry.access_policy_for("chewy-phenom").value == "public_feed"


def test_registry_instantiates_every_configured_extractor(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("TEST_USAJOBS_USER_AGENT_EMAIL", "registered@example.com")
    registry = build_registry(tmp_path, configured_targets())
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(AssertionError("unexpected network request"))
        )
    )

    extractors = registry.create_enabled(client=client)

    assert isinstance(extractors["acme-greenhouse"], GreenhouseExtractor)
    assert isinstance(extractors["acme-lever"], LeverExtractor)
    assert isinstance(extractors["acme-ashby"], AshbyExtractor)
    assert isinstance(extractors["acme-workday"], WorkdayExtractor)
    assert isinstance(extractors["acme-nextera"], NextEraExtractor)
    assert isinstance(extractors["acme-phenom"], PhenomExtractor)
    assert isinstance(extractors["acme-smartrecruiters"], SmartRecruitersExtractor)
    assert isinstance(extractors["acme-workable"], WorkableExtractor)
    assert isinstance(extractors["jobicy-test"], JobicyExtractor)
    assert isinstance(extractors["federal-usajobs"], USAJobsExtractor)
    assert extractors["acme-ashby"].include_unlisted is True
    assert extractors["acme-workday"].page_size == 10
    assert extractors["acme-nextera"].site == "acme-energy"
    assert extractors["acme-phenom"].tenant == "ACMEUS"
    assert extractors["acme-smartrecruiters"].company_identifier == "acme"
    assert extractors["acme-smartrecruiters"].page_size == 50
    assert extractors["acme-workable"].account_subdomain == "acme"
    assert extractors["jobicy-test"].count == 100
    assert extractors["jobicy-test"].geo == "usa"
    assert extractors["jobicy-test"].cache_ttl == timedelta(minutes=120)
    assert extractors["federal-usajobs"].results_per_page == 50
    assert extractors["federal-usajobs"].max_pages == 2
    assert all(
        extractor.bronze_storage is registry.bronze_storage
        for extractor in extractors.values()
    )
    assert all(
        extractor.checkpoint_manager is registry.checkpoint_manager
        for extractor in extractors.values()
    )


def test_checkpoint_names_are_scoped_to_company_sites(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("TEST_USAJOBS_USER_AGENT_EMAIL", "registered@example.com")
    registry = build_registry(tmp_path, configured_targets())

    extractors = registry.create_enabled()
    checkpoint_names = [extractor.checkpoint_name for extractor in extractors.values()]

    assert len(checkpoint_names) == len(set(checkpoint_names))
    assert "greenhouse-acme-extractor" in checkpoint_names
    assert "workday-acme/External-extractor" in checkpoint_names
    assert "nextera-acme-energy-extractor" in checkpoint_names
    assert "phenom-acme-extractor" in checkpoint_names
    assert "smartrecruiters-acme-extractor" in checkpoint_names
    assert "workable-acme-extractor" in checkpoint_names
    assert "jobicy-jobicy-test-extractor" in checkpoint_names
    assert "usajobs-public-extractor" in checkpoint_names
    for extractor in extractors.values():
        close = getattr(extractor, "close", None)
        if callable(close):
            close()


def test_registry_rejects_disabled_and_unknown_targets(tmp_path: Path) -> None:
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "disabled-lever",
                    "source": "lever",
                    "enabled": False,
                    "site": "acme",
                }
            ]
        }
    )
    registry = build_registry(tmp_path, targets)

    try:
        registry.create("disabled-lever")
    except ExtractorRegistryError as exc:
        assert "disabled" in str(exc).casefold()
    else:
        raise AssertionError("disabled targets must not be created implicitly")

    extractor = registry.create("disabled-lever", allow_disabled=True)
    assert isinstance(extractor, LeverExtractor)
    extractor.close()

    try:
        registry.create("missing")
    except ExtractorRegistryError as exc:
        assert "unknown" in str(exc).casefold()
    else:
        raise AssertionError("unknown target names must fail fast")


def test_registry_never_automates_a_disallowed_source_policy(tmp_path: Path) -> None:
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "manual-greenhouse",
                    "source": "greenhouse",
                    "enabled": False,
                    "board_token": "manual",
                }
            ]
        }
    )
    source_config = SourcesConfig.model_validate(
        {
            "enabled_sources": [],
            "sources": {
                "greenhouse": {
                    "enabled": False,
                    "type": "ats",
                    "access_policy": "manual_import",
                    "base_url": "https://example.com/jobs",
                    "api_base_url": "https://example.com/api",
                    "rate_limit_per_minute": 1,
                }
            },
        }
    )
    registry = ExtractorRegistry(
        targets=targets,
        source_config=source_config,
        bronze_storage=BronzeStorage(tmp_path / "bronze"),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )

    try:
        registry.create("manual-greenhouse", allow_disabled=True)
    except ExtractorRegistryError as exc:
        assert "automation is prohibited" in str(exc).casefold()
        assert "manual_import" in str(exc)
    else:
        raise AssertionError("allow_disabled must not bypass source access policy")


def test_handshake_partner_target_requires_provisioned_environment_key(
    tmp_path: Path, monkeypatch
) -> None:
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "authorized-handshake",
                    "source": "handshake",
                    "institution_scope": "example-university",
                    "api_key_env": "TEST_HANDSHAKE_EDU_API_KEY",
                    "authorized_use_confirmed": True,
                }
            ]
        }
    )
    source_config = SourcesConfig.model_validate(
        {
            "enabled_sources": ["handshake"],
            "sources": {
                "handshake": {
                    "enabled": True,
                    "type": "job_board",
                    "access_policy": "partner_api",
                    "base_url": "https://joinhandshake.com",
                    "api_base_url": "https://edu-api.joinhandshake.com/v1",
                    "rate_limit_per_minute": 30,
                }
            },
        }
    )
    monkeypatch.delenv("TEST_HANDSHAKE_EDU_API_KEY", raising=False)
    registry = ExtractorRegistry(
        targets=targets,
        source_config=source_config,
        bronze_storage=BronzeStorage(tmp_path / "bronze"),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )

    try:
        registry.create("authorized-handshake")
    except HandshakeAuthorizationError as exc:
        assert "TEST_HANDSHAKE_EDU_API_KEY" in str(exc)
    else:
        raise AssertionError("Handshake targets must fail without a provisioned API key")


def test_handshake_partner_target_requires_authorized_use_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "unconfirmed-handshake",
                    "source": "handshake",
                    "institution_scope": "example-university",
                    "api_key_env": "TEST_HANDSHAKE_EDU_API_KEY",
                }
            ]
        }
    )
    source_config = SourcesConfig.model_validate(
        {
            "enabled_sources": ["handshake"],
            "sources": {
                "handshake": {
                    "enabled": True,
                    "type": "job_board",
                    "access_policy": "partner_api",
                    "base_url": "https://joinhandshake.com",
                    "api_base_url": "https://edu-api.joinhandshake.com/v1",
                    "rate_limit_per_minute": 30,
                }
            },
        }
    )
    monkeypatch.setenv("TEST_HANDSHAKE_EDU_API_KEY", "support-enabled-key")
    registry = ExtractorRegistry(
        targets=targets,
        source_config=source_config,
        bronze_storage=BronzeStorage(tmp_path / "bronze"),
        checkpoint_manager=CheckpointManager(tmp_path / "checkpoints"),
    )

    with pytest.raises(HandshakeAuthorizationError, match="authorized_use_confirmed"):
        registry.create("unconfirmed-handshake")


def test_usajobs_target_requires_key_and_registered_email(tmp_path: Path, monkeypatch) -> None:
    targets = ExtractorTargetsConfig.model_validate(
        {
            "targets": [
                {
                    "name": "federal-usajobs",
                    "source": "usajobs",
                    "api_key_env": "TEST_USAJOBS_API_KEY",
                    "user_agent_email_env": "TEST_USAJOBS_USER_AGENT_EMAIL",
                }
            ]
        }
    )
    monkeypatch.delenv("TEST_USAJOBS_API_KEY", raising=False)
    monkeypatch.delenv("TEST_USAJOBS_USER_AGENT_EMAIL", raising=False)
    registry = build_registry(tmp_path, targets)

    try:
        registry.create("federal-usajobs")
    except USAJobsAuthorizationError as exc:
        assert "TEST_USAJOBS_API_KEY" in str(exc)
    else:
        raise AssertionError("USAJOBS targets must fail without an environment key")

    monkeypatch.setenv("TEST_USAJOBS_API_KEY", "test-key")
    try:
        registry.create("federal-usajobs")
    except USAJobsAuthorizationError as exc:
        assert "TEST_USAJOBS_USER_AGENT_EMAIL" in str(exc)
    else:
        raise AssertionError("USAJOBS targets must require the registration email")


def test_target_names_must_be_unique_case_insensitively() -> None:
    try:
        ExtractorTargetsConfig.model_validate(
            {
                "targets": [
                    {"name": "Acme", "source": "lever", "site": "acme"},
                    {"name": "acme", "source": "ashby", "job_board_name": "acme"},
                ]
            }
        )
    except ValidationError as exc:
        assert "unique" in str(exc).casefold()
    else:
        raise AssertionError("target names must be unique")
