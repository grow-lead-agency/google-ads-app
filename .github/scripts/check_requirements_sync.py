"""Drift guard: requirements*.txt (upstream, čte je setup.sh) vs pyproject.toml (uv.lock).

GrowLead fork tooling (GRO-1302). Upstream mění jen requirements*.txt; když merge z upstreamu
posune rozsah závislosti, tenhle check v CI selže a řekne, co dorovnat v pyproject.toml
(potom `uv lock` a commit locku).

Exit 0 = shodné, exit 1 = drift (vypsaný na stderr).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]


def _parse_lines(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # -r / -e / --index-url se nekontroluje
            continue
        req = Requirement(line)
        key = canonicalize_name(req.name) + (f"[{','.join(sorted(req.extras))}]" if req.extras else "")
        marker = f"; {req.marker}" if req.marker else ""
        out[key] = f"{req.specifier}{marker}"
    return out


def _requirements_file(name: str) -> dict[str, str]:
    path = ROOT / name
    if not path.exists():
        return {}
    return _parse_lines(path.read_text(encoding="utf-8").splitlines())


def _diff(label: str, txt: dict[str, str], toml: dict[str, str]) -> list[str]:
    problems = []
    for key in sorted(set(txt) | set(toml)):
        if key not in toml:
            problems.append(f"{label}: '{key}{txt[key]}' je v .txt, chybí v pyproject.toml")
        elif key not in txt:
            problems.append(f"{label}: '{key}{toml[key]}' je v pyproject.toml, chybí v .txt")
        elif txt[key] != toml[key]:
            problems.append(f"{label}: '{key}' .txt '{txt[key]}' != pyproject '{toml[key]}'")
    return problems


def main() -> int:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = _parse_lines(data["project"].get("dependencies", []))
    dev_all = _parse_lines(data.get("dependency-groups", {}).get("dev", []))

    problems = _diff("runtime (requirements.txt)", _requirements_file("requirements.txt"), runtime)

    # requirements-dev.txt: každá jeho položka musí být v dev group se stejným rozsahem.
    # Dev group smí mít navíc nástroje GrowLead (ruff, pip-audit), ty upstream nezná.
    dev_txt = _requirements_file("requirements-dev.txt")
    problems += _diff(
        "dev (requirements-dev.txt)", dev_txt, {k: v for k, v in dev_all.items() if k in dev_txt}
    )

    if problems:
        print("requirements*.txt a pyproject.toml se rozjely:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Oprava: dorovnej pyproject.toml, pak `uv lock` a commit uv.lock.", file=sys.stderr)
        return 1
    print(f"OK: {len(runtime)} runtime + {len(dev_txt)} dev závislostí sedí s requirements*.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
