"""Complete assertion verdicts projected into the existing source-bound critic mechanics."""

from __future__ import annotations

from functools import partial
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sibyl_core.tasks._evidence_json import canonical, read_json_value
from sibyl_core.tasks.memory_validation import CriticOutput, PreparedMemoryValidation
from sibyl_core.tasks.procedure_evidence import EvidenceRef
from sibyl_core.tasks.procedure_review import ReviewFinding

from . import assertion_critic, critic_pair, fast_critic

VERSION = "explicit-assertion-verdict-v1"
CONTRACTS = ("baseline", "verdict")
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
INSTRUCTIONS = """Output-format override: use the forced VerdictOutput tool below, replacing only
references to returning CriticOutput, findings=[] or a top-level abstention in the original review
instructions. All original evidence, assertion scope, critique rules and advisory cautions still
apply. Assess EVERY entry of assertions exactly once, including /content and every claim record.
Copy each exact claim_path and claim_sha256 from assertion_hashes. Do not omit supported targets,
duplicate targets, merge targets, or invent targets.

Return one verdict for each assertion:
- supported: the original evidence establishes the assertion within its actual stated scope.
  Give a concise rationale and at least one exact original citation key in evidence_refs. Do not
  smuggle a criticism, correction or invented fact into a supported rationale.
- concern: one or more material defects in the exact assertion are warranted by original evidence.
  Return every warranted finding in findings, each using the existing ReviewFinding fields and
  the same target path/hash as this verdict. Use only the five declared basis values. A finding
  must identify an actual defect, not a stronger imagined assertion or an accurate neighbor.
- unable: evidence prevents a useful assessment of this assertion. Give a concise reason. Missing
  proof of an assertion is not proof of its opposite. Do not use unable to hide a warranted concern
  or to explain that an assertion is supported.

Concern and inability may coexist across assertions; preserve both. Never add findings to a
supported or unable verdict. Inspect all original evidence independently of advisory labels.
The complete raw verdicts, including every rationale and reason, are subject to review. A
supported verdict or an empty projected finding list grants no publication permission."""


class _BoundVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    claim_path: _Text
    claim_sha256: _Digest


class SupportedVerdict(_BoundVerdict):
    verdict: Literal["supported"]
    rationale: _Text
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class ConcernVerdict(_BoundVerdict):
    verdict: Literal["concern"]
    findings: list[ReviewFinding] = Field(min_length=1)


class UnableVerdict(_BoundVerdict):
    verdict: Literal["unable"]
    reason: _Text


class VerdictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    verdicts: list[
        Annotated[SupportedVerdict | ConcernVerdict | UnableVerdict, Field(discriminator="verdict")]
    ] = Field(min_length=1)


def critic_request(
    prepared: PreparedMemoryValidation,
    hints: list[dict[str, str]] | None = None,
    *,
    contract: str = "baseline",
) -> dict[str, Any]:
    if contract not in CONTRACTS:
        raise ValueError("unknown critic contract")
    request = assertion_critic.critic_request(prepared, hints, contract="assertion")
    if contract == "verdict":
        request["messages"][0]["content"] = (
            INSTRUCTIONS + "\n\n" + request["messages"][0]["content"]
        )
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "VerdictOutput",
                    "description": "Assess every prepared assertion with an explicit bound verdict.",
                    "parameters": VerdictOutput.model_json_schema(),
                },
            }
        ]
        request["tool_choice"] = {"type": "function", "function": {"name": "VerdictOutput"}}
    return request


def _citations(refs: list[EvidenceRef], payload: dict[str, Any]) -> None:
    identifiers = [ref.evidence_id for ref in refs]
    if len(identifiers) != len(set(identifiers)) or any(
        identifier not in payload["citations"] for identifier in identifiers
    ):
        raise ValueError("invalid verdict evidence references")


def project(output: VerdictOutput, prepared: PreparedMemoryValidation) -> CriticOutput:
    """Reject the complete response on any coverage, identity or citation defect."""
    payload = read_json_value(prepared.payload_json.encode())
    paths = [verdict.claim_path for verdict in output.verdicts]
    if len(paths) != len(set(paths)) or set(paths) != set(payload["assertions"]):
        raise ValueError("verdicts must cover every assertion exactly once")
    findings, unable = [], []
    for verdict in output.verdicts:
        if verdict.claim_sha256 != payload["assertion_hashes"][
            verdict.claim_path
        ] or verdict.claim_sha256 != critic_pair.digest(
            canonical(payload["assertions"][verdict.claim_path])
        ):
            raise ValueError("verdict assertion hash mismatch")
        if isinstance(verdict, SupportedVerdict):
            _citations(verdict.evidence_refs, payload)
        elif isinstance(verdict, ConcernVerdict):
            for finding in verdict.findings:
                if (finding.claim_path, finding.claim_sha256) != (
                    verdict.claim_path,
                    verdict.claim_sha256,
                ):
                    raise ValueError("finding must bind its concern verdict")
                _citations(finding.evidence_refs, payload)
                findings.append(finding)
        else:
            unable.append({"claim_path": verdict.claim_path, "reason": verdict.reason})
    return CriticOutput(findings=findings, abstention_reason=canonical(unable) if unable else None)


def _parse(prepared, body, status, usage):
    output = VerdictOutput.model_validate(
        fast_critic._arguments(body, status, tool_name="VerdictOutput")
    )
    projected = project(output, prepared)
    fast_critic._require_usage(usage)
    return projected, {
        "verdict_output": output.model_dump(mode="json"),
        "projected_output": projected.model_dump(mode="json"),
        "projection_version": VERSION,
    }


async def interpret(entry: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    contract = entry["contract"]
    if contract not in CONTRACTS:
        raise ValueError("unknown critic contract")
    return await fast_critic.interpret(
        entry,
        raw,
        request_builder=partial(critic_request, contract=contract),
        output_parser=_parse if contract == "verdict" else None,
    )
