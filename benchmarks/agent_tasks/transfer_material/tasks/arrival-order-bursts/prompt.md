Repair arrival-order burst grouping. Input {gap,ticks:[integers]}. Never reorder or reject ticks. A new tick joins the current burst only when its difference from the previous arrival is between0 and gap inclusive; a backwards jump starts a new burst. Return {bursts:[[ticks_in_arrival_order],...]}. There is no watermark.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
