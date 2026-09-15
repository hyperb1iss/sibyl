Repair telemetry sessions. Input {lag,break_after,arrivals:[[device,time]]}. Before each event use global maximum previously accepted time minus lag as watermark; reject strictly older, accept equality. Accepted events alone advance maximum; negative times allowed. Group accepted events by device in timestamp order, consecutive gap <= break_after joins. Return {ranges:[[device,start,end,count]],dropped}, ranges sorted device/start.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
