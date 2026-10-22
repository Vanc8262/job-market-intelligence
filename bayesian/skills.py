"""B1: empirical-Bayes shrinkage of skill shares within role families.

"38% of data science postings ask for Spark" means little when the family has eight
postings. For each top skill, a Beta prior is fitted across all role families by
maximum marginal likelihood (how much families really differ for that skill), and each
family's share becomes a posterior: small families move toward the skill's typical
share, large families keep their own. Every share comes with a credible interval.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from bayesian.beta import JEFFREYS, fit_beta_prior, posterior
from bayesian.records import model_card
from utils.config import BayesianSkillSharesConfig

MODEL_NAME = "skill_shares"
UNCLASSIFIED = "unclassified"


def skill_share_report(
    postings: Iterable[tuple[str | None, set[str]]],
    settings: BayesianSkillSharesConfig,
    *,
    credible_level: float,
) -> dict[str, object]:
    """Return overall and per-family shrunk skill shares.

    ``postings`` yields ``(role_family, skills)`` for each posting analyzed.
    """
    family_totals: Counter[str] = Counter()
    family_skills: dict[str, Counter[str]] = {}
    overall: Counter[str] = Counter()
    total = 0
    for family, skills in postings:
        key = family or UNCLASSIFIED
        total += 1
        family_totals[key] += 1
        family_skills.setdefault(key, Counter()).update(skills)
        overall.update(skills)
    families = sorted(family_totals, key=lambda name: (-family_totals[name], name))
    reported = [
        name for name in families if family_totals[name] >= settings.minimum_family_postings
    ]
    top = [skill for skill, _ in sorted(overall.items(), key=lambda item: (-item[1], item[0]))]
    rows: list[dict[str, object]] = []
    for skill in top[: settings.top_skills]:
        counts = [family_skills[name][skill] for name in families]
        totals = [family_totals[name] for name in families]
        prior = fit_beta_prior(
            counts,
            totals,
            fallback_strength=settings.fallback_prior_strength,
            max_strength=settings.max_prior_strength,
        )
        by_family: dict[str, object] = {}
        for name in reported:
            count = family_skills[name][skill]
            shrunk = posterior(count, family_totals[name], (prior.alpha, prior.beta))
            by_family[name] = {
                "count": count,
                "raw_share": round(count / family_totals[name], 4),
                **shrunk.summary(credible_level),
            }
        overall_posterior = posterior(overall[skill], total, JEFFREYS)
        rows.append(
            {
                "skill": skill,
                "posting_count": overall[skill],
                "share": round(overall[skill] / total, 4) if total else 0.0,
                "interval": overall_posterior.summary(credible_level),
                "prior": prior.summary(),
                "by_family": by_family,
            }
        )
    return {
        "model": model_card(
            MODEL_NAME,
            stage="B1",
            settings=settings,
            replaces="raw skill shares",
            version=settings.version,
        ),
        "credible_level": credible_level,
        "postings": total,
        "families": [{"family": name, "postings": family_totals[name]} for name in reported],
        "skills": rows,
    }


__all__ = ["UNCLASSIFIED", "skill_share_report"]
