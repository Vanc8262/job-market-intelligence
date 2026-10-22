"""Configuration-driven construction of source-specific extractors."""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from extractors.ashby import AshbyExtractor
from extractors.base_extractor import BaseExtractor
from extractors.greenhouse import GreenhouseExtractor
from extractors.lever import LeverExtractor
from extractors.nextera import NextEraExtractor
from extractors.phenom import PhenomExtractor
from extractors.workday import WorkdayExtractor
from storage.base import BaseStorage
from storage.bronze import BronzeStorage
from utils.checkpoint import CheckpointManager
from utils.config import (
    AshbyTarget,
    Config,
    ExtractorTarget,
    ExtractorTargetsConfig,
    GreenhouseTarget,
    LeverTarget,
    NextEraTarget,
    PhenomTarget,
    SourceAccessPolicy,
    SourcesConfig,
    WorkdayTarget,
)
from utils.logger import log_event


class ExtractorRegistryError(ValueError):
    """Raised when a configured extractor target cannot be constructed."""


class ExtractorRegistry:
    """Instantiate extractor plugins from validated company target configuration."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        targets: ExtractorTargetsConfig | None = None,
        source_config: SourcesConfig | None = None,
        bronze_storage: BaseStorage | None = None,
        checkpoint_manager: CheckpointManager | None = None,
    ) -> None:
        """Initialize shared extractor dependencies and the target catalog."""
        self.config = config or Config()
        self.targets = targets or self.config.extractor_targets
        self.source_config = source_config or self.config.sources
        self.bronze_storage = bronze_storage or BronzeStorage(self.config.settings.paths.bronze_dir)
        self.checkpoint_manager = checkpoint_manager or CheckpointManager(
            self.config.settings.paths.checkpoint_dir
        )
        self._targets_by_name = {target.name.casefold(): target for target in self.targets.targets}
        self._validate_source_references(self.targets.targets)

    @property
    def configured_names(self) -> list[str]:
        """Return all configured target names in declaration order."""
        return [target.name for target in self.targets.targets]

    @property
    def enabled_names(self) -> list[str]:
        """Return enabled target names in declaration order."""
        return [target.name for target in self.targets.targets if target.enabled]

    def create(
        self,
        target_name: str,
        *,
        client: httpx.Client | None = None,
        allow_disabled: bool = False,
    ) -> BaseExtractor:
        """Construct one configured target with shared storage and checkpoints."""
        target = self._targets_by_name.get(target_name.casefold())
        if target is None:
            raise ExtractorRegistryError(f"Unknown extractor target: {target_name}")
        if not target.enabled and not allow_disabled:
            raise ExtractorRegistryError(f"Extractor target is disabled: {target.name}")
        policy = self.access_policy_for(target.name)
        if not policy.allows_automated_collection:
            raise ExtractorRegistryError(
                f"Extractor automation is prohibited for {target.name} under "
                f"access policy {policy.value}"
            )

        if isinstance(target, GreenhouseTarget):
            extractor: BaseExtractor = GreenhouseExtractor(
                target.board_token,
                company_name=target.company_name,
                config=self.config,
                bronze_storage=self.bronze_storage,
                checkpoint_manager=self.checkpoint_manager,
                client=client,
            )
        elif isinstance(target, LeverTarget):
            extractor = LeverExtractor(
                target.site,
                company_name=target.company_name,
                config=self.config,
                bronze_storage=self.bronze_storage,
                checkpoint_manager=self.checkpoint_manager,
                client=client,
            )
        elif isinstance(target, AshbyTarget):
            extractor = AshbyExtractor(
                target.job_board_name,
                company_name=target.company_name,
                include_unlisted=target.include_unlisted,
                config=self.config,
                bronze_storage=self.bronze_storage,
                checkpoint_manager=self.checkpoint_manager,
                client=client,
            )
        elif isinstance(target, WorkdayTarget):
            extractor = WorkdayExtractor(
                target.host,
                target.tenant,
                target.site,
                company_name=target.company_name,
                page_size=target.page_size,
                config=self.config,
                bronze_storage=self.bronze_storage,
                checkpoint_manager=self.checkpoint_manager,
                client=client,
            )
        elif isinstance(target, NextEraTarget):
            extractor = NextEraExtractor(
                target.host,
                target.site,
                company_name=target.company_name,
                config=self.config,
                bronze_storage=self.bronze_storage,
                checkpoint_manager=self.checkpoint_manager,
                client=client,
            )
        elif isinstance(target, PhenomTarget):
            extractor = PhenomExtractor(
                target.host,
                target.tenant,
                target.site,
                locale=target.locale,
                path_prefix=target.path_prefix,
                company_name=target.company_name,
                config=self.config,
                bronze_storage=self.bronze_storage,
                checkpoint_manager=self.checkpoint_manager,
                client=client,
            )
        else:
            raise ExtractorRegistryError(f"Unsupported extractor source: {target.source}")

        log_event(
            "INFO",
            "Instantiated extractor target {target}",
            component="extractor_registry",
            event="extractor_instantiated",
            source=target.source,
            target=target.name,
            access_policy=policy.value,
        )
        return extractor

    def access_policy_for(self, target_name: str) -> SourceAccessPolicy:
        """Return the validated source access policy for one configured target."""
        target = self._targets_by_name.get(target_name.casefold())
        if target is None:
            raise ExtractorRegistryError(f"Unknown extractor target: {target_name}")
        definition = self.source_config.sources.get(target.source)
        if definition is None:
            raise ExtractorRegistryError(
                f"Target {target.name} references unknown source: {target.source}"
            )
        return definition.access_policy

    def create_enabled(self, *, client: httpx.Client | None = None) -> dict[str, BaseExtractor]:
        """Construct every enabled target, keyed by configured target name."""
        return {name: self.create(name, client=client) for name in self.enabled_names}

    def _validate_source_references(self, targets: Iterable[ExtractorTarget]) -> None:
        """Ensure targets reference source plugins enabled at the source level."""
        source_config = self.source_config
        for target in targets:
            definition = source_config.sources.get(target.source)
            if definition is None:
                raise ExtractorRegistryError(
                    f"Target {target.name} references unknown source: {target.source}"
                )
            if target.enabled and (
                not definition.enabled or target.source not in source_config.enabled_sources
            ):
                raise ExtractorRegistryError(
                    f"Target {target.name} references disabled source: {target.source}"
                )
            if target.enabled and not definition.access_policy.allows_automated_collection:
                raise ExtractorRegistryError(
                    f"Target {target.name} cannot use access policy "
                    f"{definition.access_policy.value} for automated extraction"
                )


__all__ = ["ExtractorRegistry", "ExtractorRegistryError"]
