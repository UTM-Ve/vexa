# tests — offline, no provider, no meeting

`test_breakdown.py` proves the two things that must never be guessed (owner resolution · due-date
resolution) as pure tables, plus prompt composition and parsing. `test_workspace.py` covers reading
a finished meeting out of a workspace tree and writing its artifacts back. `test_service.py` drives
the whole runner over a temp workspace root with a scripted model reply.
