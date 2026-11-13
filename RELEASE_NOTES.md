# Tank Scan Lab v1.1.0

Cap Array Lab is renamed Tank Scan Lab and gains a tank-scan mode. Place an N-port inductor, a switched capacitor array and a cross-coupled gm core on the canvas, tick what the canvas contains, and one small-signal run reports f0, LC-only f0, Rp(LC), Rp(LC+loading), terminal gm/gds, negative conductance, startup margin, gm·Rp, Q and per-module loss shares. Without a gm core the same view gives the passive resonance and loss analysis.

Start with `python3 app.py --tank --project examples/ideal_tank.json` (synthetic, no device models) or `bash tank-scan-lab.sh`. Definitions and validation: [docs/TANK_SCAN.md](docs/TANK_SCAN.md).

The capacitor-array C/Q workbench, resonance view, Z11 Studio and workbook formats are unchanged; existing workbooks open as before. Running a new tank scan requires your own Cadence/Spectre environment, models and licenses.
