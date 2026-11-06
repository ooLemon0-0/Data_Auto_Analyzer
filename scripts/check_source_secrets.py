"""Check the exact Git index (or working sources) before publishing.

Local config is only read in memory to detect accidental credential copies.
No secret values are printed. Release binaries belong in GitHub Releases.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SECRET_KEY = re.compile(r"password|passwd|secret|access_token|private_key", re.I)


def credentials(value, path=""):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            field = f"{path}.{key}"
            if "selectors" not in path and isinstance(item, str) and len(item) >= 6 and SECRET_KEY.search(key):
                found.add(item)
            found.update(credentials(item, field))
    elif isinstance(value, list):
        for item in value:
            found.update(credentials(item, path))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    secrets = set()
    private = ROOT / "config/config.json"
    if private.exists():
        secrets = credentials(json.loads(private.read_text(encoding="utf-8")))
    command = ["git", "ls-files", "-z"]
    if not args.staged:
        command += ["--cached", "--others", "--exclude-standard"]
    names = set(subprocess.check_output(command, cwd=ROOT).decode().split("\0")) - {""}
    problems = []
    token = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)")
    for name in sorted(names):
        if name.startswith(("release/", "build/", "dist/", "runtime/")) or name == "config/config.json" or Path(name).name.startswith(".env"):
            problems.append(f"{name}: private / generated file must not enter source Git")
            continue
        try:
            content = (subprocess.check_output(["git", "show", ":" + name], cwd=ROOT) if args.staged else (ROOT / name).read_bytes()).decode("utf-8")
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if any(secret in line for secret in secrets) or token.search(line):
                problems.append(f"{name}:{number}: credential match (value omitted)")
    for problem in problems:
        print(problem)
    print(f"Source secret check: {len(names)} files, {len(problems)} problems")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
