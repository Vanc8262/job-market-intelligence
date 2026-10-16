"""Extractor interface and source-specific extractor plugins."""

from extractors.ashby import AshbyExtractor
from extractors.base_extractor import BaseExtractor, ExtractionResult, SearchRequest
from extractors.greenhouse import GreenhouseExtractor
from extractors.lever import LeverExtractor
from extractors.nextera import NextEraExtractor
from extractors.phenom import PhenomExtractor
from extractors.registry import ExtractorRegistry, ExtractorRegistryError
from extractors.workday import WorkdayExtractor

__all__ = [
    "AshbyExtractor",
    "BaseExtractor",
    "ExtractionResult",
    "ExtractorRegistry",
    "ExtractorRegistryError",
    "GreenhouseExtractor",
    "LeverExtractor",
    "NextEraExtractor",
    "PhenomExtractor",
    "SearchRequest",
    "WorkdayExtractor",
]
