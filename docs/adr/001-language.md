# ADR-001: Keep Python (standard library only)

**Status:** Accepted
**Date:** 2026-10-19

## Context
loqctl is a btop-style hardware monitor with firmware limit and fan-curve control for Lenovo LOQ/Legion
laptops. It must install easily on many distros, sample about once per second at low overhead, run
privileged writes that users can audit, and reuse GPL code from LenovoLegionLinux and loq-control,
which is largely Python.

Measured on a LOQ 15IRX9 (i7-13650HX, Python 3.14):

| What | Cost |
|---|---|
| Full Python sample (CPU, memory, battery, RAPL, temps, 9 firmware values) | 8.9 ms CPU per tick (~0.9% of a core at 1 Hz) |
| `nvidia-smi` subprocess per tick | 29 ms CPU per call |
| NVML through `ctypes` (same readings) | 0.06 ms per read |
| CLI startup (`loqctl --version`) | 237 ms |

The dominant cost is spawning `nvidia-smi`, not the language.

## Options considered
- **Python, standard library only.** No rewrite, no build, Python is on every target distro, root-run
  code stays readable, direct reuse of GPL Python code. Costs: no static types, hand-built curses widgets.
- **Rust (ratatui, nvml-wrapper).** Best TUI library, type safety, single binary. Costs: full rewrite,
  per-architecture builds, a large crate dependency tree, LenovoLegionLinux/loq-control code must be
  translated. Saves under 1% of a core.
- **Go (bubbletea).** Single binary and a good TUI library, but no reusable Lenovo code exists in Go.
- **Python with packages (textual, psutil, pynvml).** Nicer UI, but pip installs into a tool run as root
  and PEP 668 makes distro installs painful.

## Decision
Stay on Python 3.10+, standard library only, and:
1. Read NVIDIA telemetry through NVML via `ctypes`, fall back to `nvidia-smi`, and skip GPU queries
   while the GPU is runtime-suspended.
2. Use lazy imports so CLI subcommands do not load curses or the HTTP server (target under 100 ms).
3. Split the single file into a package after the telemetry expansion, keeping a single-file entry point.
4. Recommend a root-owned install path (`/usr/local/bin`) for anyone who runs loqctl with sudo.
   sudo running a user-writable file is a privilege-escalation path in any language.

## Consequences
- Easier: installing anywhere, auditing what runs as root, reusing Python code from other projects.
- Harder: chart and editor widgets are hand-built; no compiler catches type errors, so tests carry that load.
- Revisit if sampling exceeds about 3% CPU after the NVML change, users ask for a single distributable
  binary, or a polkit/D-Bus daemon becomes necessary.
