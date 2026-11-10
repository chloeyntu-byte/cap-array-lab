# User-provided native environment

No native tools, model files, library mappings or licenses ship with this repository. Begin with a working, licensed Cadence project owned by you.

Standalone configuration example (replace placeholders):

```bash
export CAPLAB_WORKSPACE=/path/to/workspace
export CAPLAB_LIBRARY=MY_CAP_LIBRARY
export CAPLAB_NATIVE_VIRTUOSO=/path/to/virtuoso
export CAPLAB_CADENCE_ENV=/path/to/trusted-site-environment.sh
export CAPLAB_PYTHON=python3
bash launch.sh
```

`CAPLAB_CADENCE_ENV` is executable shell configuration, so only select a file you trust. It is optional when the native tool is already configured in the inherited environment. The tool does not install or repair licenses. License variables such as `CDS_LIC_FILE` and `LM_LICENSE_FILE` are inherited or supplied by your environment script. The explicit native-environment snapshot supplied by Maestro takes precedence over a standalone site script.

`CAPLAB_NATIVE_CDS_LIB` may point to an absolute existing mapping file. The application otherwise uses the selected workspace's mapping. Use **Load library** and **Advanced** to select your library, reference cell, view and simulation settings. A new workbook is empty; `CAP_DEMO` is only a placeholder library name.

## Maestro menu

From a user-owned project initialization file, load the installed repository's `load.il` (replace the path):

```lisp
load("/path/to/cap-array-lab/load.il")
```

The loader resolves `launch.sh` relative to itself and registers Cap Array Lab for supported Maestro/ADE windows. Each click captures that window's source context and native environment in a private temporary file. The `--context` application argument consumes this generated context; do not hand-author it from unrelated sessions. The desktop Python environment is isolated from the original native environment.

Native runs create private testbench/setup copies and retain run histories under `.work/` and `runs/`. Save and inspect results before removing runtime folders. User workbooks retain their original schema and may contain local paths; they are not automatically anonymized.

Z11 Studio is bundled under `caplab/z11/`. Native launch uses an isolated runtime copy and a source-bound broker; offline launch uses the same copy mechanism. The standalone `z11_launch` helper saves state next to the bundled Z11 code. Do not include those state folders in redistributions.

This release was tested with synthetic input, data-only export plans and fake executable launch checks. No licensed EDA process or native menu integration was exercised.
