"""Validate generated release notes against the public claim gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.trust.doc_claim_gate import (
    build_doc_claim_receipt,
    load_claim_docs,
    validate_doc_claim_receipt,
)

RELEASE_NOTES_SURFACE = "generated-release-notes.md"


def validate_release_notes_claims(notes: str) -> tuple[dict[str, Any], list[str]]:
    """Scan release notes together with the claim-gated public corpus."""
    docs = load_claim_docs()
    docs[RELEASE_NOTES_SURFACE] = notes
    receipt = build_doc_claim_receipt(docs)
    receipt["fixture"] = "generated-release-notes-claim-gate-v1"
    return receipt, validate_doc_claim_receipt(receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate generated release notes through the public claim gate."
    )
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)

    notes = args.notes.read_text(encoding="utf-8")
    receipt, failures = validate_release_notes_claims(notes)
    receipt["status"] = "FAIL" if failures else "PASS"
    receipt["failures"] = failures
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        f"{json.dumps(receipt, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )

    if failures:
        for failure in failures:
            sys.stderr.write(f"release notes claim gate: {failure}\n")
        return 1
    sys.stdout.write(f"Release notes claim gate passed: {args.receipt}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
