#!/usr/bin/env python3
"""Rebuild packaged/*.skill and packaged/all-skills.zip from skills/."""
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
OUT = ROOT / "packaged"
NAMES = [
    "perf-verifier",
    "code-verifier",
    "context-saver",
    "repo-map",
    "prompt-clarifier",
]


def pack(name: str) -> Path:
    base = SKILLS / name
    zpath = OUT / f"{name}.skill"
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            arc = f"{name}/{p.relative_to(base).as_posix()}"
            info = zipfile.ZipInfo(arc)
            mode = 0o755 if p.suffix == ".py" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, p.read_bytes())
    names = zipfile.ZipFile(zpath).namelist()
    assert f"{name}/SKILL.md" in names, names
    assert f"{name}/{name}/SKILL.md" not in names, names
    return zpath


def main() -> None:
    OUT.mkdir(exist_ok=True)
    paths = [pack(n) for n in NAMES]
    allz = OUT / "all-skills.zip"
    with zipfile.ZipFile(allz, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, arcname=p.name)
    for p in paths:
        print(p.name, p.stat().st_size)
    print(allz.name, allz.stat().st_size)


if __name__ == "__main__":
    main()
