"""Versioned bridge contracts and strict dependency-free validation."""
import hashlib
import json
import re
from pathlib import Path


class BridgeError(Exception):
    pass


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 100000}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
COMMIT = {"type": "string", "pattern": "^[a-f0-9]{40,64}$"}
ID = {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,100}$"}


def array(item, minimum=0):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": 10000}


def enum(*values):
    return {"type": "string", "enum": list(values)}


STAT = obj({k: {"type": "integer", "minimum": 0}
            for k in ("size", "mtime_ns", "ctime_ns", "device", "inode")})
SCOPE = obj({"description": TEXT, "allowed_commands": array(TEXT),
             "scratch_write": {"type": "boolean"}})
ARTIFACT_REQUEST = obj({"path": TEXT, "mode": enum("copy", "reference"), "purpose": TEXT})
REQUEST = obj({"schema_version": {"type": "integer", "enum": [1]},
               "language": enum("en"), "prompt": TEXT, "base_commit": COMMIT,
               "final_commit": COMMIT, "scope": SCOPE,
               "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200},
               "artifacts": array(ARTIFACT_REQUEST)})
MANIFEST = obj({"schema_version": {"type": "integer", "enum": [1]},
                "job_id": ID, "session_id": ID, "task_id": TEXT,
                "session_cwd": TEXT, "project": TEXT, "created_at": TEXT,
                "readiness": enum("READY_FOR_REVIEW"), "language": enum("en"),
                "base_commit": COMMIT, "final_commit": COMMIT,
                "base_branch": TEXT, "final_branch": TEXT,
                "prompt": obj({"path": enum("prompt.md"), "sha256": HASH}),
                "review_policy": obj({"path": enum("reviewer.md"), "sha256": HASH}),
                "result_schema": obj({"path": enum("result.schema.json"), "sha256": HASH}),
                "scope": SCOPE, "timeout_seconds": REQUEST["properties"]["timeout_seconds"],
                "artifacts": array(obj({"source": TEXT, "path": TEXT,
                                        "mode": enum("copy", "reference"), "purpose": TEXT,
                                        "sha256": HASH, "stat": STAT}))})
RESULT = obj({"schema_version": {"type": "integer", "enum": [1]},
              "verdict": enum("VERIFIED", "NEEDS_FIXES", "NOT_VERIFIABLE"),
              "summary": TEXT,
              "findings": array(obj({"priority": enum("P0", "P1", "P2", "P3"),
                                      "title": TEXT, "detail": TEXT,
                                      "file": {"type": "string"},
                                      "line": {"type": ["integer", "null"], "minimum": 1}})),
              "checks_performed": array(obj({"check": TEXT, "result": TEXT}), 1),
              "limitations": array(TEXT)})
SECTIONS = ("Objective", "Changed-file purposes", "Reproduction commands",
            "Actual test results", "Limitations", "Acceptance criteria")


def validate(value, schema, where="$"):
    """Validate the JSON Schema subset used by the checked-in contracts."""
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    expected = schema.get("type")
    if expected:
        names = expected if isinstance(expected, list) else [expected]
        if not any(type(value) is types[name] for name in names):
            raise BridgeError(f"{where}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise BridgeError(f"{where}: invalid value")
    if isinstance(value, dict):
        props = schema["properties"]
        if set(schema["required"]) - value.keys() or value.keys() - props.keys():
            raise BridgeError(f"{where}: missing or unknown fields")
        for key, item in value.items():
            validate(item, props[key], f"{where}.{key}")
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            raise BridgeError(f"{where}: invalid item count")
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{where}[{i}]")
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100000):
            raise BridgeError(f"{where}: invalid string length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise BridgeError(f"{where}: invalid format")
    elif type(value) is int:
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise BridgeError(f"{where}: number out of range")


def read_json(path, limit=2_000_000):
    p = Path(path)
    if p.stat().st_size > limit:
        raise BridgeError(f"JSON exceeds {limit} bytes: {p}")
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise BridgeError(f"Duplicate JSON key: {k}")
            d[k] = v
        return d
    return json.loads(p.read_text(), object_pairs_hook=pairs)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def metadata(path):
    s = Path(path).stat()
    return dict(size=s.st_size, mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns,
                device=s.st_dev, inode=s.st_ino)


def validate_prompt(text):
    if not 100 <= len(text.encode()) <= 1_000_000:
        raise BridgeError("Prompt must contain 100..1000000 UTF-8 bytes")
    for heading in SECTIONS:
        m = re.search(r"^## " + re.escape(heading) + r"\s*\n(.*?)(?=^## |\Z)",
                      text, re.M | re.S)
        if not m or not m[1].strip() or re.search(r"\b(TODO|TBD|REPLACE_ME)\b", m[1]):
            raise BridgeError(f"Prompt needs a completed '## {heading}' section")


def validate_result(result):
    validate(result, RESULT)
    if result["verdict"] == "NEEDS_FIXES" and not result["findings"]:
        raise BridgeError("NEEDS_FIXES requires findings")
    if result["verdict"] == "VERIFIED" and result["findings"]:
        raise BridgeError("VERIFIED cannot contain unresolved findings")
    if result["verdict"] == "NOT_VERIFIABLE" and not result["limitations"]:
        raise BridgeError("NOT_VERIFIABLE requires limitations")


if __name__ == "__main__":
    folder = Path(__file__).parent / "schemas"
    folder.mkdir(exist_ok=True)
    for name, schema in (("request", REQUEST), ("manifest", MANIFEST), ("result", RESULT)):
        (folder / f"{name}.schema.json").write_text(json.dumps(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}, indent=2) + "\n")
