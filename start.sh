#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Foldspace 蛋白结构工作台"
  echo "启动：./start.sh"
  echo "配置：PROTEIN_STUDIO_PORT=8765 PROTEIN_STUDIO_PYTHON=/path/to/python3.12 ./start.sh"
  exit 0
fi
if [[ $# -gt 0 ]]; then
  echo "未知参数；运行 ./start.sh --help 查看启动方式。" >&2
  exit 2
fi

if [[ -n "${PROTEIN_STUDIO_PYTHON:-}" ]]; then
  studio_python="$PROTEIN_STUDIO_PYTHON"
else
  studio_python=""
  for studio_candidate in "$PWD/.venv/bin/python" "$(command -v python3 || true)" "/opt/homebrew/bin/python3.12" "/usr/local/bin/python3.12" \
    "$HOME/.pyenv/shims/python3" "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"; do
    if [[ -n "$studio_candidate" && -x "$studio_candidate" ]] && \
      "$studio_candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      studio_python="$studio_candidate"
      break
    fi
  done
fi
if [[ -z "$studio_python" ]]; then
  echo "需要 Python 3.10+。安装 Python 后重新运行，或设置 PROTEIN_STUDIO_PYTHON。" >&2
  exit 1
fi
"$studio_python" -c 'import sys; assert sys.version_info >= (3, 10), "请使用 Python 3.10+，推荐 3.12；可通过 PROTEIN_STUDIO_PYTHON 指定解释器。"'
if "$studio_python" - <<'PY'
import json, os, urllib.request
try:
    url = 'http://127.0.0.1:' + os.environ.get('PROTEIN_STUDIO_PORT', '8765') + '/api/health'
    with urllib.request.urlopen(url, timeout=1) as response:
        data = json.load(response)
    if data.get('service') == 'protein-studio':
        print('工作台已在运行：' + url.rsplit('/api/health', 1)[0])
        raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
then
  exit 0
fi
exec "$studio_python" -m backend.server
