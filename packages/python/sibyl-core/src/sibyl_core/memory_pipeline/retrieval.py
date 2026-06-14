"""Retrieval contracts shared by memory pipeline stages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CandidateSourceFailure:
    source: str
    error_type: str

    def as_metadata(self) -> dict[str, str]:
        return {
            "source": self.source,
            "error_type": self.error_type,
        }


@dataclass(frozen=True, slots=True)
class CandidateSourceResult[CandidateT]:
    source: str
    candidates: tuple[CandidateT, ...] = ()
    failure: CandidateSourceFailure | None = None

    @classmethod
    def success(
        cls,
        source: str,
        candidates: Sequence[CandidateT],
    ) -> CandidateSourceResult[CandidateT]:
        return cls(source=source, candidates=tuple(candidates))

    @classmethod
    def failed(
        cls,
        source: str,
        error_type: str,
    ) -> CandidateSourceResult[CandidateT]:
        return cls(
            source=source,
            failure=CandidateSourceFailure(source=source, error_type=error_type),
        )

    @property
    def degraded(self) -> bool:
        return self.failure is not None
