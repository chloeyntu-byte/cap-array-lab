#!/bin/bash
# Tank Scan Lab: open the workbench in tank-scan mode (N-port + cap array + gm startup analysis).
exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/launch.sh" --tank "$@"
