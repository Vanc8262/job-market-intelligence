"""Extractor interfaces and future source-specific extractor plugins."""

from extractors.ashby import AshbyExtractor
from extractors.base_extractor import BaseExtractor, ExtractionResult, SearchRequest
from extractors.greenhouse import GreenhouseExtractor
from extractors.handshake_edu import HandshakeAuthorizationError, HandshakeEduExtractor
from extractors.jobicy import JobicyExtractor, JobicyPollDeferredError
from extractors.lever import LeverExtractor
from extractors.nextera import NextEraExtractor
from extractors.phenom import PhenomExtractor
from extractors.registry import ExtractorRegistry, ExtractorRegistryError
from extractors.smartrecruiters import SmartRecruitersExtractor
from extractors.usajobs import USAJobsAuthorizationError, USAJobsExtractor
from extractors.workable import WorkableExtractor
from extractors.workday import WorkdayExtractor

__all__ = [
    "AshbyExtractor",
    "BaseExtractor",
    "ExtractionResult",
    "GreenhouseExtractor",
    "HandshakeAuthorizationError",
    "HandshakeEduExtractor",
    "JobicyExtractor",
    "JobicyPollDeferredError",
    "LeverExtractor",
    "NextEraExtractor",
    "PhenomExtractor",
    "ExtractorRegistry",
    "ExtractorRegistryError",
    "SearchRequest",
    "SmartRecruitersExtractor",
    "USAJobsAuthorizationError",
    "USAJobsExtractor",
    "WorkableExtractor",
    "WorkdayExtractor",
]
