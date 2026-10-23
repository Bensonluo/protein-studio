#!/usr/bin/env bash
# Linux installation recipe matching the official AF3 v3.0.4 Dockerfile.
# Does not fetch model weights or the sequence databases, and does not start jobs.
set -euo pipefail

af3_prefix=/opt/af3
hmmer_prefix=/opt/hmmer
af3_python=3.12
af3_commit=85c4d20505fd5cef05eac22b534d4e793971ae69
hmmer_sha=ca70d94fd0cf271bd7063423aabb116d42de533117343a9b27a65c17ff06fbf3
usage() {
  cat <<'HELP'
Usage: install_af3_runtime.sh [--prefix /opt/af3] [--hmmer-prefix /opt/hmmer] [--python 3.12]
Run on the target Linux host after installing build dependencies and uv==0.9.24.
Requires writable installation parents; never overwrites an existing checkout.
Downloads official source/dependencies and CCD data, not weights or sequence DBs.
HELP
}
while (($#)); do
  case "$1" in
    --prefix) af3_prefix="$2"; shift 2;;
    --hmmer-prefix) hmmer_prefix="$2"; shift 2;;
    --python) af3_python="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) usage >&2; exit 2;;
  esac
done
[[ "$(uname -s)" == Linux ]] || { echo 'Run this installer on the target Linux CPU/GPU host.' >&2; exit 1; }
[[ "$af3_prefix" == /* && "$hmmer_prefix" == /* ]] || { echo 'Installation paths must be absolute.' >&2; exit 1; }
[[ ! -e "$af3_prefix" && ! -e "$hmmer_prefix" ]] || { echo 'An installation path exists. Inspect/reuse it; this installer will not replace it.' >&2; exit 1; }
for af3_tool in git uv wget sha256sum tar patch gcc g++ make; do
  command -v "$af3_tool" >/dev/null || { echo "Missing build tool: $af3_tool" >&2; exit 1; }
done
[[ "$(uv --version)" == 'uv 0.9.24'* ]] || { echo 'Use uv==0.9.24, as pinned by the official v3.0.4 build recipe.' >&2; exit 1; }
git clone --branch v3.0.4 --depth 1 https://github.com/google-deepmind/alphafold3.git "$af3_prefix"
[[ "$(git -C "$af3_prefix" rev-parse HEAD)" == "$af3_commit" ]] || { echo 'AF3 tag does not match the pinned official commit.' >&2; exit 1; }
af3_build_dir="$(mktemp -d)"
trap 'rm -rf "$af3_build_dir"' EXIT
wget -O "$af3_build_dir/hmmer-3.4.tar.gz" http://eddylab.org/software/hmmer/hmmer-3.4.tar.gz
(
  cd "$af3_build_dir"
  echo "$hmmer_sha  hmmer-3.4.tar.gz" | sha256sum --check
  tar -xzf hmmer-3.4.tar.gz
  patch -p0 < "$af3_prefix/docker/jackhmmer_seq_limit.patch"
  cd hmmer-3.4
  ./configure --prefix="$hmmer_prefix"
  make -j "$(getconf _NPROCESSORS_ONLN)"
  make install
  (cd easel && make install)
)
export UV_PROJECT_ENVIRONMENT="$af3_prefix/venv"
export UV_LINK_MODE=copy
export UV_COMPILE_BYTECODE=1
uv venv "$UV_PROJECT_ENVIRONMENT" --python "$af3_python"
(
  cd "$af3_prefix"
  uv sync --frozen --all-groups --no-editable
  uv run --frozen build_data
)
"$af3_prefix/venv/bin/python" - "$af3_prefix" "$hmmer_prefix" <<'PY'
import datetime,hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
report={'installedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'api':'v3.0.4',
        'sourceCommit':'85c4d20505fd5cef05eac22b534d4e793971ae69',
        'sourceScriptSha256':hashlib.sha256((root/'run_alphafold.py').read_bytes()).hexdigest(),
        'lockSha256':hashlib.sha256((root/'uv.lock').read_bytes()).hexdigest(),
        'python':sys.version,'hmmerPrefix':sys.argv[2],
        'weightsInstalledByThisScript':False,'sequenceDatabasesInstalledByThisScript':False,
        'gpuInferenceValidated':False}
(root/'foldspace-runtime-install.json').write_text(json.dumps(report,indent=2)+'\n')
print('Runtime built. Run database/GPU probes before starting real inference.')
PY
