"""Protected receipts distinguish owner omission from physical deletion."""

from dataclasses import asdict

from sibyl_core.services.memory_derivations import observation_from_record
from sibyl_core.services.source_observations import SourceUnavailableError


def omission_receipt(source, binding):
    observations = binding.get("observations")
    if not isinstance(observations, list) or len(observations) != 1:
        raise SourceUnavailableError()
    previous = observation_from_record(observations[0])
    if previous.source != source.observation.source:
        raise SourceUnavailableError()
    return {
        "version": 1,
        "target_id": binding["target_id"],
        "body_sha256": binding["body_sha256"],
        "source": asdict(source.observation),
        "previous_source": observations[0],
        "principal_id": binding["principal_id"],
        "authority_ceiling": binding["authority_ceiling"],
    }


def validate_omission_receipt(binding):
    """Validate an optional protected receipt without granting current authority."""
    receipt = binding.get("operational_omission")
    if receipt is None:
        return None
    if not isinstance(receipt, dict) or binding.get("active") is not False:
        raise ValueError("invalid operational omission receipt")
    if (
        set(receipt)
        != {
            "version",
            "target_id",
            "body_sha256",
            "source",
            "previous_source",
            "principal_id",
            "authority_ceiling",
        }
        or type(receipt.get("version")) is not int
        or receipt.get("version") != 1
    ):
        raise ValueError("invalid operational omission receipt")
    if any(
        receipt.get(key) != binding.get(key)
        for key in ("target_id", "body_sha256", "principal_id", "authority_ceiling")
    ):
        raise ValueError("invalid operational omission receipt")
    if binding.get("observations") != [receipt.get("previous_source")]:
        raise ValueError("invalid operational omission receipt")
    try:
        observed = observation_from_record(receipt["source"])
        previous = observation_from_record(receipt["previous_source"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid operational omission receipt") from exc
    if (
        not observed.durable
        or not previous.durable
        or observed.source != previous.source
        or observed.source.organization_id != binding.get("organization_id")
        or binding.get("target_kind") != "graph_entity"
        or observed.effective_incarnation != previous.effective_incarnation
        or previous.generation > observed.generation
    ):
        raise ValueError("invalid operational omission source")
    return observed


def omission_allows_regeneration(source, binding) -> bool:
    try:
        observed = validate_omission_receipt(binding)
    except ValueError:
        return False
    current = source.observation
    return (
        observed is not None
        and observed.source == current.source
        and observed.effective_incarnation == current.effective_incarnation
        and observed.generation <= current.generation
    )
