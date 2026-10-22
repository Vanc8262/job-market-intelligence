"""Phase 8 Bayesian analytics.

B0 (foundation): convergence diagnostics and gate, Beta-Binomial building blocks, model
cards, and ``config/bayesian.yaml``. B1: Beta-Binomial data-quality evaluation and
empirical-Bayes skill-share shrinkage. Both B1 models are conjugate (closed form), so
they need only SciPy; sampled models (B2 onward) use the optional ``bayes`` extra
(PyMC, nutpie, ArviZ) and must pass ``check_inference_data`` before they are reported.
"""

from bayesian.beta import BetaPosterior, EmpiricalPrior, fit_beta_prior, posterior
from bayesian.diagnostics import (
    DiagnosticsReport,
    DiagnosticThresholds,
    check_draws,
    check_inference_data,
    ess_bulk,
    ess_tail,
    rhat,
)
from bayesian.quality import RateCheck, evaluate_quality
from bayesian.records import model_card
from bayesian.skills import skill_share_report

__all__ = [
    "BetaPosterior",
    "DiagnosticThresholds",
    "DiagnosticsReport",
    "EmpiricalPrior",
    "RateCheck",
    "check_draws",
    "check_inference_data",
    "ess_bulk",
    "ess_tail",
    "evaluate_quality",
    "fit_beta_prior",
    "model_card",
    "posterior",
    "rhat",
    "skill_share_report",
]
