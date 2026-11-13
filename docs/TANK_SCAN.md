# Tank scan

Start with `bash tank-scan-lab.sh` (or `python3 app.py --tank`). The same mode is available as **Measurement results → Measurement mode → Tank scan** in the regular workbench.

## Workflow

1. Place the tank on the canvas: an N-port inductor (**RF devices / SNP**) or ideal inductors, the capacitor-array cell driven by a register, and optionally a cross-coupled gm cell (drains on P/N, gates cross-connected inside the cell). Bias it as in normal operation, for example VDD through the inductor centre tap.
2. Under **Canvas contains**, tick *Inductor / N-port*, *Cap array* and/or *gm core*. With *gm core* ticked the active analysis runs; without it, the passive resonance and loss analysis runs.
3. Check the roles under **Roles & options**. They are auto-detected (N-port or inductor → inductor, register-driven cell → cap array, any other cell → gm core) and can be changed. Changing roles or ticks re-analyzes saved data only.
4. **Run tank scan.** With a gm core, a low reference frequency (**gm reference freq.**, default 10 MHz) is added below the sweep.
5. **Narrow sweep to f0** sets a 201-point linear sweep around the resolved f0 values; run again for bracket-level accuracy. A warning appears when the f0 bracket exceeds 1 %.

On the results tab the library and settings panels hide automatically; **Show setup panels** keeps them visible.

## Method

Every enabled instance is measured through 0 V probes on all of its terminals. Two independent AC excitations (P/N differential and P-to-ground) are combined into a floating 1 A P-N drive. By Tellegen's theorem the module admittances `Y_k = 2·conj(S_k)/|V_d|²` add up to the port admittance; the analysis checks that residual (the **module sum check**).

The gm core's own 2×2 admittance on the P/N nets is identified from the two excitations: `gm = Re(Y12+Y21)/2` and `gds = Re(Y11+Y22)/2`, per side, at the core terminals. The negative conductance follows the usual gm-cancellation tank scan: the low-frequency terminal gm is cancelled under the actual voltages at f0 (`G_neg = gm/2` for a symmetric pair). Everything else the core absorbs, including the RF roll-off of gm, counts as core loading. The alternative split with the cross terms evaluated at f0 is reported in separate columns; both give the same net Re(Y).

| Quantity | Definition |
|---|---|
| f0 | First Im(Z)=0 of the complete circuit including the core (startup frequency) |
| f0 LC only | Zero of the inductor + array susceptance (estimate, core capacitance removed) |
| Rp(LC) | 1 / (G_inductor + G_array) at f0 |
| Rp(LC+loading) | 1 / G_loss, G_loss = G_LC + other loads + core loading |
| Startup margin | G_neg / G_loss; > 1 starts (≈ gm·Rp/2 for a symmetric pair) |
| gm·Rp | gm at the reference frequency × Rp(LC+loading) |
| Q, C_eff, L_eff | From the susceptance slope at resonance, C_eff = ½·dB/dω |

Selecting a row lists each module (or coupled-winding group) with its loss conductance and share of G_loss.

## Validation

- `tests/test_tank_scan.py` checks an analytic network: f0, G_neg = gm/2, core loading = gds/2, both Rp values, margin, gm·Rp, LC-only f0 and loss shares agree to 1e-5. A second network with frequency-dependent gm checks the cancellation definition. The bundled example is analyzed in the same test.
- During development the analysis was compared on a native SP run against an independent netlist-level gm-cancellation simulation (AC-only controlled sources, pure floating drive). Rp(LC+loading), G_neg and margin agreed within 0.02 %. The terminal gm/gds differed from the transistor's intrinsic operating-point values by the expected series-resistance degeneration. That run used the developer's own models and is not part of this repository.

## Limitations

- Values are small-signal at the DC operating point, i.e. the startup condition. Self-biased cores whose DC current differs from steady state need HB/transient for swing and steady-state gain.
- Values at f0 are linear interpolations between the two bracketing samples.
- An N-port whose data start above DC is extrapolated by the simulator for the DC bias and the low reference point.
