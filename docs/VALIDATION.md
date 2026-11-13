# Release validation

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
bash -n launch.sh cadence-child.sh caplab/z11/z11_launch
bash launch.sh --help
```

For Tk coverage, set `DISPLAY` to the private display assigned to your session. Do not use another user's desktop. Full Z11 window coverage also requires the optional NumPy/Matplotlib environment. This release used a private 1600×1000 Xvnc display and checked 1280×800 layouts.

The original 128 tests passed before packaging changes. The release suite has 140 tests: under Python 3.6, 139 pass and one optional Z11 GUI test skips because Matplotlib is not installed in that interpreter; under the modern Z11 environment, all 140 pass. The added coverage includes closed-form capacitance/Q and LC resonance checks, inline-workbook integrity, Touchstone metadata, visible Tk table rows, replay cancellation/restart, launch argument checks using a fake native executable, and Z11 nodal results versus an independent closed-form solution.

A read-only comparison of the original and bundled Z11 on a generic 101-frequency example produced identical complex results, parameter labels, optimization score and serialized schematic plan. Native EDA tools were not run. The full source-tree pre/post manifest comparison found no content, size or modification-time changes in either source tree.

The theme module is copied verbatim. Product-side font fallback handles Tk builds that return family names in lower case; the kit itself is unchanged. C/Q Canvas plots use the kit palette; Z11 uses the shared Matplotlib theme.

## v1.1.0 tank scan

`tests/test_tank_scan.py` adds analytic checks of the tank-scan analysis (f0, negative conductance, core loading, Rp(LC), Rp(LC+loading), margin, gm·Rp, LC-only f0, loss shares; a frequency-dependent gm for the cancellation definition), role/mode selection, review edge cases (reference point outside the resonance search, f0 on the first sweep sample, exact zero crossings), the low reference frequency and the bundled `examples/ideal_tank.json`. The full suite has 151 tests; under Python 3.6 with a private display, 150 pass and one optional Z11 GUI test skips. `python3 -m caplab.tank_example` regenerates the tank example byte-for-byte numerically (`scripts/compare_numeric_tree.py`). Native tank scans were cross-checked during development (see docs/TANK_SCAN.md) and are not part of this repository.

Screenshots:

- `screenshots/01-empty.png`: empty workbench and Load example.
- `screenshots/02-example.png`: synthetic circuit canvas.
- `screenshots/03-running.png`: live synthetic replay and progress.
- `screenshots/04-cq-results.png`: per-code C/Q and decomposition.
- `screenshots/05-z11-results.png`: saved LC Z11 magnitude/phase.
- `screenshots/06-compact.png`: 1280×800 controls and footer.
- `screenshots/07-z11-studio.png`: bundled standalone Z11 example.

The core plot area occupies more than 60% of the main horizontal pane at 1600×1000. The footer and measurement controls remain mapped inside a 1280×800 window. Large settings lists retain their scrollbars. Empty, running and completed states were inspected in the real GUI.

Native library/PDK compatibility, licensed simulation and real Maestro menu callbacks remain user-environment validation items. The Z11 GUI combination of Python 3.6 / Tk 8.5 / compatible Matplotlib was not available here. Core UI tests passed on Python 3.6.8 / Tk 8.5.13; the full GUI suite passed on Python 3.11.15 / Tk 8.6.13. Z11 numerical paths also passed on Python 3.6. No package or system component was installed.
