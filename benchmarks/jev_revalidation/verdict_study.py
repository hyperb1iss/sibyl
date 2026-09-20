"""Compare explicit assertion verdicts with the fixed assertion-prefix critic."""

from functools import partial

from . import assertion_study, verdict_critic

VERSION = "jev-verdict-critic-study-v1"
COMPARISONS = {
    "structure_effect": ["baseline_direct", "verdict_direct"],
    "jev_increment": ["verdict_direct", "verdict_live_jev"],
    "structure_with_jev": ["baseline_live_jev", "verdict_live_jev"],
    "baseline_stress": ["baseline_direct", "baseline_misleading"],
    "verdict_stress": ["verdict_direct", "verdict_misleading"],
    "stress_structure_effect": ["baseline_misleading", "verdict_misleading"],
}
PROTOCOL = {
    **assertion_study.PROTOCOL,
    "design": "fixed assertion-prefix CriticOutput versus complete per-assertion VerdictOutput; schema plus instruction intervention crossed with direct, independent live Jev, and misleading hints",
    "comparisons": COMPARISONS,
    "quality": "strict semantic-pass gain for structure effect and Jev increment; no paired new false accept, lost valid concern, unsupported finding, or lost supported accept; review every raw verdict rationale and reason, including unprojected supported verdicts",
    "live_comparison": "live versus live compares combined policies with independent labels; direct versus direct isolates schema plus instruction intervention, not a pure prompt effect",
    "projection": "exact complete assertion coverage and original citations required; only concern verdicts become findings; any unable retains findings and adds abstention; invalid responses fail whole without partial salvage",
    "stress": "compare stress to its own direct contract and verdict stress to baseline stress; no pooling with normal-route metrics",
}
CONFIG = assertion_study.StudyConfig(
    version=VERSION,
    seed=20260924,
    contracts=verdict_critic.CONTRACTS,
    critic_version=verdict_critic.VERSION,
    request_builder=verdict_critic.critic_request,
    interpreter=verdict_critic.interpret,
    comparisons=COMPARISONS,
    protocol=PROTOCOL,
)
ARMS = CONFIG.arms
entries = partial(assertion_study.entries, config=CONFIG)
manifest = partial(assertion_study.manifest, config=CONFIG)
execute = partial(assertion_study.execute, config=CONFIG)
summarize = partial(assertion_study.summarize, config=CONFIG)
run = partial(assertion_study.run, config=CONFIG)

if __name__ == "__main__":
    assertion_study.main(CONFIG)
