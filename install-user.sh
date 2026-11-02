#!/bin/bash
set -euo pipefail
vp_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
vp_root="${XDG_DATA_HOME:-$HOME/.local/share}/vp-helper"
vp_bin="$HOME/.local/bin"
vp_state="${XDG_STATE_HOME:-$HOME/.local/state}/vp"
vp_selected="$vp_state/submit"

if [[ $EUID -eq 0 ]]; then
    printf '%s\n' 'Run the user installer as your ordinary user, without sudo.' >&2
    exit 1
fi
if ! python3 -c 'import ensurepip, sys; assert sys.version_info >= (3, 12)' 2>/dev/null; then
    printf '%s\n' 'Python 3.12+ with venv/ensurepip is required; no system packages were changed.' >&2
    exit 1
fi
if ! command -v tmux >/dev/null; then
    printf '%s\n' 'tmux is required; install it before running this installer.' >&2
    exit 1
fi
# Only replace links owned by this installer. Keep global programs intact.
for vp_name in vp submit; do
    vp_link="$vp_bin/$vp_name"
    vp_target="$vp_root/current/venv/bin/vp"
    if [[ $vp_name == submit ]]; then vp_target="$vp_selected"; fi
    if [[ -e "$vp_link" || -L "$vp_link" ]]; then
        if [[ ! -L "$vp_link" || $(readlink -- "$vp_link") != "$vp_target" ]]; then
            printf '%s\n' "Preserved existing $vp_link; move it explicitly before installing." >&2
            exit 1
        fi
    fi
done
if [[ -e "$vp_selected" || -L "$vp_selected" ]]; then
    if [[ ! -L "$vp_selected" ]]; then
        printf '%s\n' "Preserved existing $vp_selected; expected a managed submit symlink." >&2
        exit 1
    fi
fi
if [[ -e "$vp_root/current" && ! -L "$vp_root/current" ]]; then
    printf '%s\n' "Preserved existing $vp_root/current; expected a managed release symlink." >&2
    exit 1
fi

mkdir -p -- "$vp_root/releases" "$vp_bin" "$vp_state"
vp_release="$(mktemp -d "$vp_root/releases/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
mkdir -- "$vp_release/app"
cp -a -- "$vp_source/vp_helper" "$vp_source/pyproject.toml" "$vp_source/README.md" "$vp_release/app/"
cp -a -- "$vp_source/third_party" "$vp_release/"
python3 -m venv "$vp_release/venv"
vp_pip_args=(--disable-pip-version-check --no-index --find-links "$vp_source/wheels")
"$vp_release/venv/bin/python" -m pip install "${vp_pip_args[@]}" "$vp_release/app" \
    selectolax lxml robobrowser 'Werkzeug==0.16.1' 'prettytable<4'
# The upstream submission scripts use the system install's absolute shebang.
# Adjust only the installed copies so they import this release's Python code.
"$vp_release/venv/bin/python" - "$vp_release" <<'PY'
from pathlib import Path
import sys
release = Path(sys.argv[1])
for platform in ("qoj", "cf_submit"):
    script = release / "third_party" / platform / "submit"
    text = script.read_text(encoding="utf8")
    script.write_text("#!" + str(release / "venv/bin/python") + "\n" + text.split("\n", 1)[1], encoding="utf8")
    script.chmod(0o755)
PY
"$vp_release/venv/bin/python" -m pip check
"$vp_release/venv/bin/vp" --version

# Switch only after the isolated installation is complete. Old releases remain.
ln -sfn -- "$vp_release" "$vp_root/current"
if [[ ! -e "$vp_selected" && ! -L "$vp_selected" ]]; then
    ln -s -- "$vp_root/current/third_party/qoj/submit" "$vp_selected"
fi
ln -sfn -- "$vp_root/current/venv/bin/vp" "$vp_bin/vp"
ln -sfn -- "$vp_selected" "$vp_bin/submit"
printf '%s\n' "Installed $vp_bin/vp and $vp_bin/submit; keep $vp_bin first in PATH."
printf '%s\n' 'Global vp/submit and existing account, contest and history files were preserved.'
