# Changelog

## v1.1.0

- Rename the product to Tank Scan Lab; the C/Q workbench, resonance view and Z11 Studio are unchanged.
- Add the Tank scan mode: N-port + capacitor array + cross-coupled gm startup analysis (f0, Rp(LC), Rp(LC+loading), terminal gm/gds, G_neg, startup margin, gm·Rp, Q, per-module loss shares). Canvas-content selection switches between the passive and active analysis.
- Add `tank-scan-lab.sh`, `--tank`, an optional low gm reference frequency, and a synthetic tank example (`examples/ideal_tank.json`, `python -m caplab.tank_example`).
- Tank view: KPI tiles, resizable plot / table / loss areas, collapsible roles and auto-hidden side panels on the results tab.
- Factor the floating-drive reconstruction in `loss_native` into a reusable function (behaviour unchanged).

## v1.0.0

- Prepare a clean MIT-licensed source release with bilingual documentation.
- Apply the RF-FIELDWORK cap theme, shared headers, primary actions, status chips, progress bars and dark plots.
- Bundle Z11 Studio and preserve the original numerical, workbook and native integration contracts.
- Replace site-specific defaults with user environment configuration.
- Add a reproducible synthetic eight-code capacitor/LC workbook and two-port Touchstone example.
- Add offline replay, compatibility, numerical and UI smoke checks.
