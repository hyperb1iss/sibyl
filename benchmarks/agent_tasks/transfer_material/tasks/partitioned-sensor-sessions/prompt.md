Repair independent sensor sessions. Input {lateness,gap,samples:[{sensor,tick}]}. Each sensor has its own maximum accepted tick and watermark maximum-lateness. Reject strictly older ticks, accept equality. Sort accepted ticks per sensor, join consecutive gaps <=gap, duplicates count. Return {sessions:[{sensor,first,last,n}],rejected}. Unlike a global collector, another sensor's clock cannot make this sensor late.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
