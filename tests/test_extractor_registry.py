"""Tests for configuration-driven extractor construction."""

from pathlib import Path

import httpx
from pydantic import ValidationError

from extractors import (
    AshbyExtractor,
    ExtractorRegistry,
    ExtractorRegistryError,
    GreenhouseExtractor,
    LeverExtractor,
    NextEraExtractor,
    PhenomExtractor,
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
    }
    assert [target.name for target in targets if target.enabled] == [
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


def test_registry_instantiates_every_configured_extractor(tmp_path: Path) -> None:
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
    assert extractors["acme-ashby"].include_unlisted is True
    assert extractors["acme-workday"].page_size == 10
    assert extractors["acme-nextera"].site == "acme-energy"
    assert extractors["acme-phenom"].tenant == "ACMEUS"
    assert all(
        extractor.bronze_storage is registry.bronze_storage
        for extractor in extractors.values()
    )
    assert all(
        extractor.checkpoint_manager is registry.checkpoint_manager
        for extractor in extractors.values()
    )


def test_checkpoint_names_are_scoped_to_company_sites(tmp_path: Path) -> None:
    registry = build_registry(tmp_path, configured_targets())

    extractors = registry.create_enabled()
    checkpoint_names = [extractor.checkpoint_name for extractor in extractors.values()]

    assert len(checkpoint_names) == len(set(checkpoint_names))
    assert "greenhouse-acme-extractor" in checkpoint_names
    assert "workday-acme/External-extractor" in checkpoint_names
    assert "nextera-acme-energy-extractor" in checkpoint_names
    assert "phenom-acme-extractor" in checkpoint_names
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
