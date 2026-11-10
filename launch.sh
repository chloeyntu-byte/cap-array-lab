#!/bin/bash
set -euo pipefail
cal_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unset LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH TCL_LIBRARY TK_LIBRARY QT_PLUGIN_PATH
export PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
cal_python=${CAPLAB_PYTHON:-python3}
exec "$cal_python" "$cal_root/app.py" "$@"
