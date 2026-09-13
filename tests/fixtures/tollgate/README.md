# Tollgate approval streams

The JSON Lines files are the retained `operation_attempts.stdout` values for
Fulcrum external operations 13, 28, 38, and 64. They are preserved verbatim
apart from ensuring one trailing newline. Each stream begins with Tollgate's
authorization response and continues with every changed `--wait` candidate
status through promotion.
