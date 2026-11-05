# loqctl working notes (gitignored)

## Status (2026-10-30)
- main is at 92dc1bf. The old 0.3-0.4 work plan (items 0-4), the fan-curve editor, #32 fan panel and the
  UI overhaul (#86, #87, #89, #106) are all merged. Docs, completions, NO_COLOR, Makefile install,
  doctor, battery readings, GPU mode (read-only) and idle-cost fixes are in too.
- Open issues: #56, #82, #90, #101, #102 (all PR-backed, held for hardware) and #65 (low).
- Rules: tests pass first, one PR per issue, never push to main.

## Held for the user (reviewer PASS, needs hardware)
- PR 84 systemd unit (#56)
- PR 85 floors skip unchanged loaded points (#82)
- PR 92 named profiles, CLI (#90)
- PR 103 battery charging mode (#101)
- PR 104 Fn lock / camera / Win key / LED toggles (#102)
- Blocked until verified: TUI controls for battery, toggles, profiles; automatic AC/battery profile switching.
- USER ACTION: first real fan-curve write test (custom mode, small change, check readback).
- USER ACTION (unconfirmed it was done): `softdep legion_laptop pre: lenovo_wmi_gamezone` in
  /etc/modprobe.d/legion_laptop.conf and reboot, then confirm limits read (legion_wmi otherwise binds
  the gamezone GUID 887B54E3 first, so no platform profile and lenovo-wmi-other gives EINVAL).

## Decisions taken for the user (veto list: .claude/questions.md)
- Battery line order: percent+status, time, health, limit. No GPU mode switching. fan_fullspeed skipped.
- POST /v1/fancurve keeps 200 on partial failure (TUI reads per-point status).
- Fan percent steps are 2 % (1 % reads back the same); fixing #15 would allow 1 %.

## Hardware checks still pending
- Fan curve writes (reads, unit and EC temps verified 2026-10-20). Risk: EC3 reads one table and
  writes another, so readback may not match a write.
- `write_curve` writes temps before speeds, so a partial failure leaves a mixed curve (no atomic path).
- The 9 Stage 6 allowlist keys (PL3, PL4, cross-load, APU, GPU base, CPU boost): this firmware exposes only the original 9.
- AMD paths (k10temp, zenpower, RAPL on Zen): simulated sysfs only.
- GPU runtime-suspend skip: not seen live.
- NVML may keep the dGPU awake. Run loqctl ~2 min on battery with no GPU apps, then
  `cat /sys/bus/pci/devices/*/power/runtime_status` for the NVIDIA device. If never "suspended",
  call `nvmlShutdown` after each read.
- Stage 10 sudo capdata fetch and cache: needs a real password sudo run by the user.

## Leftovers
- Makefile uninstall removes the lib dir when the bin link is someone else's.
- Old worktrees agent-a465... and agent-a848... hold uncommitted edits (ask before removing).
- Stash "fit_parts overflow fix": drop (PR 83 merged).
- #65 open, low (fan-curve re-apply after resume tried once).

## Later (ask the user first)
- Version bump + tag (outward-facing).
- Rate-limit the per-hwmon name read from #133.
- More review passes on fancurve.py and curvestore write logic (fixes need hardware).
- udev rule so a group can write firmware attributes without sudo.
- Native keyboard RGB by porting LegionAura's HID protocol (MIT) over hidraw.
- Re-apply limits after resume (needs a service).
- kb_cli: `status()` reloads config from disk after apply, so JSON may show old settings (low priority).
- Skill candidates: new-issue helper (CONTRIBUTING.md sections, up to 5 per batch); ui-grid-test helper
  (render Dashboard.draw with mocked curses to a char grid with color attrs), file as an issue.

## Project reference
**What it is**: btop-style monitor and control for Lenovo LOQ/Legion on Linux. Shows limits and fan
curves next to the readings they drive. No process list. Python 3.10+, stdlib only.

**Hard rules** (full list in AGENTS.md):
- No raw EC, /dev/port, /dev/mem or acpi_call writes. Never write `fanN_target`.
- Real per-mode limits come from the debugfs capdata table (root), cached in
  ~/.cache/loqctl/mode-limits.json. Custom mode shows live sysfs values.
- Reuse only GPL-2.0-or-later, GPL-3.0, MIT, BSD, Apache-2.0 code. Never GPL-2.0-only.

**Fan curve facts** (LLL source; layout checked on hardware 2026-10-20):
- hwmon name `legion_hwmon` (number changes); files `pwm{1,2}_auto_pointN_pwm` (0-255),
  `pwm{1,2,3}_auto_pointN_temp` and `_temp_hyst`; accel/decel read-only for us.
- `fancurve_speed_unit` on /sys/devices/platform/legion (rpm|percent|level); this machine reports `rpm`.
  Each store rewrites the whole curve.
- 10 points; pwm3 has temp+temp_hyst only; also `auto_points_size`, `fancurve_defaults_powermode`,
  temp1-3, fan1/2_input/max, fan1/2_target (never write).
- Edits only in custom mode. The firmware resets the curve on mode change/reboot, so `serve`
  re-applies `active.json`.

**Environment**: LOQ 15IRX9 (83DV), BIOS NECN44WW, i7-13650HX, CachyOS, fish (Claude's Bash is zsh),
Python 3.14.8 only (check 3.10 syntax with `ast.parse(feature_version=(3, 10))`), kernel 7.2.x,
LLL dkms 0.1.3. sudo needs a password.

## Related projects (reuse sources, check licenses first)
- johnfanv2/LenovoLegionLinux: kernel module + Python CLI/GUI. Fan-curve source.
- whatanoobfaker/loq-control, JonathanRamosNascimento/lenovo-legion-control, VitaminDB/linux-legion,
  arabcian/centurion, pjt222/fancontrol.

## Pipeline lessons
- Keep the orchestrator context thin: specs in files, no code reading by it.
- Haiku reviewers cut reviewer cost ~95%; implementers stay on Sonnet.
