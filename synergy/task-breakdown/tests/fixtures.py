"""fixtures.py — the workspace trees the tests build.

``make_workspace`` writes exactly what core's meeting worker leaves behind (the cleaned-transcript
envelope + the meeting entity's frontmatter), which is this stage's whole input contract.
"""
from __future__ import annotations

import json
import subprocess

def _note(nid, speaker, text):
    return {"id": nid, "speaker": speaker, "text": text, "pass": 3, "frozen": True}


NOTES = [
    _note("s1", "Priya Raman", "I'll send the revised pricing sheet to Acme by Friday."),
    _note("s2", "Tom Álvarez", "I will book the security review with Dana in two weeks."),
]


def make_workspace(root, subject="u_jane", native="abc", *, notes=NOTES, entity=True, git=False):
    """A workspace tree shaped exactly like the one the meeting worker leaves behind."""
    ws = root / subject
    meeting_dir = ws / "kg" / "entities" / "meeting"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / f"{native}.envelope.json").write_text(
        json.dumps({"notes": notes, "cards": []}, indent=2, sort_keys=True))
    if entity:
        (meeting_dir / f"{native}.md").write_text(
            "---\ntype: meeting\n"
            f"id: {native}\ntitle: Acme renewal\nmeeting_id: {native}\n"
            f"session_uid: {native}\nplatform: google_meet\ndate: 2026-09-01\n---\n\n"
            "## Transcript\n")
    if git:
        for args in (("init", "-q"), ("config", "user.email", "a@b"), ("config", "user.name", "a")):
            subprocess.run(["git", *args], cwd=str(ws), check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(ws), check=True, capture_output=True)
    return ws


