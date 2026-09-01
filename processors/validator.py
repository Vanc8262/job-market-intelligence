"""Validation helpers for canonical job postings."""

from pydantic import ValidationError

from models.job_posting import JobPosting


class JobPostingValidator:
    """Validate records against the canonical JobPosting schema."""

    def validate(self, payload: JobPosting | dict[str, object]) -> JobPosting:
        """Return a validated JobPosting model or raise ValidationError."""
        if isinstance(payload, JobPosting):
            return payload
        try:
            return JobPosting.model_validate(payload)
        except ValidationError:
            raise
