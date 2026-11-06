"""Version 2 immutable round input contract; reuses the version 1 review result."""
from pathlib import Path
from contracts import BridgeError, COMMIT, HASH, ID, TEXT, obj, validate

FILE = obj({"sha256": HASH, "size": {"type": "integer", "minimum": 0, "maximum": 8388608}})
ROUND = obj({"schema_version": {"type": "integer", "enum": [2]},
    "conversation_id": ID, "round": {"type": "integer", "minimum": 1, "maximum": 10},
    "claude_session_id": ID, "message": TEXT, "head": {**COMMIT, "type": ["string", "null"]},
    "branch": {**TEXT, "type": ["string", "null"]}, "created_at": TEXT,
    "files": {"type": "object", "minProperties": 1, "maxProperties": 2048,
              "additionalProperties": {"anyOf": [FILE, {"type": "null"}]}}})


def validate_round(request):
    # The shared validator deliberately supports only the smaller fixed-object subset.
    envelope = {k: v for k, v in request.items() if k != "files"}
    validate(envelope, obj({k: v for k, v in ROUND["properties"].items() if k != "files"}))
    files = request.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 2048:
        raise BridgeError("A round requires 1..2048 file records")
    for name, meta in files.items():
        p = Path(name)
        if not p.parts or p.is_absolute() or ".." in p.parts or str(p) != name:
            raise BridgeError("Round file paths must be normalized project-relative paths")
        if meta is not None:
            validate(meta, FILE)
