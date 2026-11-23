"""Stack parameters of an environment (infra/params/<env>.json) with named model profiles.

A params file may say {"ModelProfile": "target"}; the profile's values (writer and judge model
ids, effort, thinking, prices) come from infra/models.json, and keys set in the params file
override them. Used by scripts/deploy.py (deployments) and the eval runner (CI evaluates the
configuration that will be deployed), so both always agree.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_profiles(path: str | Path) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {name: {k: str(v) for k, v in values.items() if not k.startswith("_")}
            for name, values in data.items() if not name.startswith("_") and isinstance(values, dict)}


def load_stack_params(path: str | Path, profiles_path: str | Path | None = None,
                      model_profile: str | None = None) -> dict[str, str]:
    """Read a params file and expand its model profile (or `model_profile`, when given)."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    profile = model_profile or data.get("ModelProfile")
    out: dict[str, str] = {}
    if profile:
        profiles_path = Path(profiles_path) if profiles_path else path.resolve().parent.parent / "models.json"
        profiles = load_profiles(profiles_path)
        if profile not in profiles:
            raise ValueError(f"unknown model profile {profile!r} in {path}; known: {', '.join(sorted(profiles))}")
        out.update(profiles[profile])
    explicit = {k: str(v) for k, v in data.items() if not k.startswith("_") and k != "ModelProfile"}
    if model_profile:  # an explicit profile choice also wins over model keys in the params file
        explicit = {k: v for k, v in explicit.items() if k not in out}
    out.update(explicit)
    return out
