#!/bin/bash
set -euo pipefail
cal_cds_args=()
if [[ -n "${CAPLAB_NATIVE_CDS_LIB:-}" ]]; then
    if [[ "$CAPLAB_NATIVE_CDS_LIB" != /* || ! -f "$CAPLAB_NATIVE_CDS_LIB" ]]; then
        printf 'Invalid private cds.lib mapping path.\n' >&2
        exit 2
    fi
    cal_cds_args=(-cdslib "$CAPLAB_NATIVE_CDS_LIB")
fi
if [[ -n "${CAPLAB_NATIVE_ENV_JSON:-}" ]]; then
    cal_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    cal_python=${CAPLAB_PYTHON:-python3}
    cal_virtuoso=${CAPLAB_NATIVE_VIRTUOSO:?Missing invoking Virtuoso executable}
    cal_mode=-nograph
    if [[ "${3:-}" == gui ]]; then cal_mode=; fi
    if [[ -n "$cal_mode" ]]; then
        exec "$cal_python" "$cal_root/caplab/launch_context.py" --exec "$cal_virtuoso" "$cal_mode" -nocdsinit ${cal_cds_args[@]+"${cal_cds_args[@]}"} -restore "$1" -log "$2"
    fi
    exec "$cal_python" "$cal_root/caplab/launch_context.py" --exec "$cal_virtuoso" -nocdsinit ${cal_cds_args[@]+"${cal_cds_args[@]}"} -restore "$1" -log "$2"
fi
# A site environment is optional; all tool and license settings are user-owned.
if [[ -n "${CAPLAB_CADENCE_ENV:-}" ]]; then
    [[ -f "$CAPLAB_CADENCE_ENV" ]] || { printf 'CAPLAB_CADENCE_ENV is not a file.\n' >&2; exit 2; }
    source "$CAPLAB_CADENCE_ENV"
fi
cal_virtuoso=${CAPLAB_NATIVE_VIRTUOSO:-virtuoso}
export CDS_LOAD_ENV=CWD
cal_mode=()
if [[ "${3:-}" != gui ]]; then cal_mode=(-nograph); fi
exec "$cal_virtuoso" ${cal_mode[@]+"${cal_mode[@]}"} -nocdsinit ${cal_cds_args[@]+"${cal_cds_args[@]}"} -restore "$1" -log "$2"
