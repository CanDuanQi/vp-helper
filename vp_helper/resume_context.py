"""Find a saved VP without preparing a contest or starting participation."""
from __future__ import annotations

import math
from pathlib import Path

from .models import Contest, VPError
from .parsing import contest_url
from .storage import read_json, state_directory


def resume_state() -> tuple[Path, dict]:
    config_path = None
    config = context = None
    directory = Path.cwd().resolve()
    for parent in (directory, *directory.parents):
        candidate = parent / "contest.json"
        if candidate.exists():
            config_path = candidate
            config = read_json(candidate)
            context = config.get("vp_context") if isinstance(config, dict) else None
            if not isinstance(context, dict):
                raise VPError("This contest.json has no saved VP context; no new VP was started.")
            break
    if config_path is None:
        context = read_json(state_directory() / "current.json")
    if not isinstance(context, dict) or not isinstance(context.get("state_path"), str):
        raise VPError("No saved VP was found. Run vp resume in the original contest directory.")
    path = Path(context["state_path"]).expanduser().resolve()
    if path.parent != state_directory().resolve():
        raise VPError("The saved VP context is outside this user's VP state directory.")
    if not path.is_file():
        raise VPError("The saved VP record is missing; no new VP was started.")
    state = read_json(path)
    try:
        contest = Contest.from_dict(state["contest"])
        platform, ident, canonical = contest_url(state["url"])
        started = state["start_time"]
        valid = (isinstance(started, (int, float)) and not isinstance(started, bool)
                 and math.isfinite(started) and started > 0
                 and state["tmux_session"] == f"vp-{platform}-{ident}-{int(started)}"
                 and contest.url == canonical and contest.platform == platform and contest.contest_id == ident
                 and isinstance(state["handle"], str) and bool(state["handle"])
                 and Path(state["directory"]).is_dir())
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise VPError("The saved VP record is invalid or its contest directory is missing; no new VP was started.")
    if config_path is not None and (
            Path(state["directory"]).resolve() != config_path.parent.resolve()
            or any(context.get(key) != state[key] for key in ("url", "handle", "start_time"))
            or config.get("handle", state["handle"]) != state["handle"]):
        raise VPError("This contest.json does not match its saved VP record; no new VP was started.")
    return path, state
