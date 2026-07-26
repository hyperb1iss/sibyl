"""Final comparison tables for Stage 1, evaluated against the pre-registered rules."""

from __future__ import annotations

import json

from paths import OUT


def main() -> None:
    stage1 = json.loads((OUT / "stage1_report.json").read_text())
    print("PRIMARY RULE (literal, k=10 items)  and  SECONDARY RULE (135,000-char budget)")
    print("=" * 96)
    for domain, data in stage1.items():
        n = data["measurable"]
        print(f"\n### {domain}  (measurable questions n={n})")
        header = f"{'ranker':7s} {'arm':12s} {'@1':>7s} {'@5':>7s} {'@10':>7s} {'@20':>7s} {'@50':>7s} {'@100':>7s} {'@25k':>7s} {'@50k':>7s} {'@135k':>7s}"
        print(header)
        for ranker in ("bm25", "dense", "rrf"):
            for arm in ("FAT", "SLICE", "SLICE_GOAL"):
                block = data["arms"][arm][ranker]
                k = block["recall_at_k"]
                b = block["recall_at_char_budget"]
                print(
                    f"{ranker:7s} {arm:12s} {k['1']:7.3f} {k['5']:7.3f} {k['10']:7.3f} "
                    f"{k['20']:7.3f} {k['50']:7.3f} {k['100']:7.3f} "
                    f"{b['25000']:7.3f} {b['50000']:7.3f} {b['135000']:7.3f}"
                )
        print()
        for ranker in ("bm25", "dense", "rrf"):
            fat10 = data["arms"]["FAT"][ranker]["recall_at_k"]["10"]
            sl10 = data["arms"]["SLICE"][ranker]["recall_at_k"]["10"]
            fatb = data["arms"]["FAT"][ranker]["recall_at_char_budget"]["135000"]
            slb = data["arms"]["SLICE"][ranker]["recall_at_char_budget"]["135000"]
            sgb = data["arms"]["SLICE_GOAL"][ranker]["recall_at_char_budget"]["135000"]
            print(
                f"  {ranker:6s} PRIMARY slice@10 {sl10:.3f} vs fat@10 {fat10:.3f} -> "
                f"{'CONFIRMED' if sl10 < fat10 else 'refuted'} | "
                f"SECONDARY slice@135k {slb:.3f} vs fat@10 {fat10:.3f} -> "
                f"{'CONFIRMED' if slb < fat10 else 'refuted'} | "
                f"budget-matched fat@135k {fatb:.3f} | goal-carry@135k {sgb:.3f}"
            )
        # median ranks
        print()
        for ranker in ("bm25", "dense", "rrf"):
            row = " ".join(
                f"{arm}={data['arms'][arm][ranker]['median_first_gold_rank']:.0f}"
                for arm in ("FAT", "SLICE", "SLICE_GOAL")
            )
            print(f"  median first-gold rank [{ranker}]: {row}")


if __name__ == "__main__":
    main()
