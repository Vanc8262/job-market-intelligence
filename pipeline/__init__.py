"""Pipeline orchestration for extraction and downstream processing."""

from pipeline.candidate_collection import (
    CandidateCollectionResult,
    CandidateCollectionRunner,
    CandidateQueryOutcome,
    CandidateTargetOutcome,
)
from pipeline.data_quality import DataQualityGateway, DataQualityResult, QualityMetric
from pipeline.extraction import ExtractionBatchResult, ExtractionRunner, TargetExtractionOutcome
from pipeline.lifecycle import LifecycleResult, PostingLifecycleRunner
from pipeline.manual_import import (
    ManualImportFailure,
    ManualImportProvenance,
    ManualImportResult,
    ManualInputKind,
    ManualOpportunity,
    ManualOpportunityImporter,
)
from pipeline.processing import (
    BronzeToSilverResult,
    BronzeToSilverRunner,
    ProcessingFailure,
    TargetProcessingStats,
)

__all__ = [
    "BronzeToSilverResult",
    "BronzeToSilverRunner",
    "CandidateCollectionResult",
    "CandidateCollectionRunner",
    "CandidateQueryOutcome",
    "CandidateTargetOutcome",
    "DataQualityGateway",
    "DataQualityResult",
    "ExtractionBatchResult",
    "ExtractionRunner",
    "LifecycleResult",
    "ManualImportFailure",
    "ManualImportProvenance",
    "ManualImportResult",
    "ManualInputKind",
    "ManualOpportunity",
    "ManualOpportunityImporter",
    "PostingLifecycleRunner",
    "ProcessingFailure",
    "QualityMetric",
    "TargetExtractionOutcome",
    "TargetProcessingStats",
]
