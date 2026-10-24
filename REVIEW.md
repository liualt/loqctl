# Review instructions

These rules apply to every code review of loqctl, including the local `/code-review` command.
They add to the hard rules in `AGENTS.md`. Breaking an `AGENTS.md` hard rule is always Important.

## Severity

Mark a finding **Important** only if it would cause one of these:

- **Broken behavior**: wrong logic, a crash, a race, a write that reports success without
  taking effect, or a stale or wrong reading shown as current.
- **Hardware risk**: a firmware write outside the allowlist (`FIELDS` or the fan-curve
  `FILE_MAP`), a write that is not bounds-checked or read back, any write to `fanN_target`,
  or a raw EC, `/dev/port`, `/dev/mem` or ACPI-call write.
- **Fake data**: a missing reading shown as `0`, a guessed value or a stale cached value
  instead of N/A.
- **Privilege or data exposure**: escalation through sudo, the Unix-socket API or
  `SO_PEERCRED` checks, or user paths or command output leaking into logs or error messages.
- **Blocked rollback**: a change the user cannot undo from loqctl, such as a curve or mode
  that can't be restored or state that only clears after a power cycle.
- **License breakage**: GPL-2.0-only code, or reused code without credit in the file header
  and `NOTICE`.

Style, naming, formatting and refactoring suggestions are **Nit** at most.
Mark problems the change didn't introduce as **Pre-existing**.

## Cap the nits

Report at most five Nits per review. Choose the ones that most affect readability or upkeep.

## Evidence

- Every finding cites `file:line`. Base behavior claims on the code you read, not on names.
- A data-source claim names both the source (sysfs or debugfs path, NVML call or command) and
  the line that reads it.
- If a claim depends on real hardware or a kernel driver version you couldn't check, say so
  and mark it unverified. Don't state it as fact.

## Always check

### Data provenance: is each reading real?

For each value the UI or API shows (temperatures, power, voltages, clocks, fan RPM,
throttling, limits, mode), trace:

1. **Where it's read from**: the sysfs or debugfs path, hwmon `name`, NVML function,
   `nvidia-smi` field, `/proc` file or MSR.
2. **Which driver or controller provides it**: for example the EC through `legion_laptop`,
   `lenovo-wmi-other`/`lenovo-wmi-gamezone` WMI, `intel-rapl` powercap, `coretemp`/`k10temp`/
   `zenpower`, `amdgpu`, `nvme`, NVML, `power_supply` or `cpufreq`.
3. **Whether it returns real data on a real machine**: flag any value that could be a
   constant, a placeholder, a firmware default that ignores the current mode (see the stock-mode
   note in `capdata.py`), a cached value that never refreshes, the wrong unit or scale, or a
   reading from the wrong device when several hwmon devices share a name.
4. **AMD and other distros**: flag Intel-only or distro-specific paths that have no fallback.
   loqctl is meant to install on AMD LOQ/Legion machines and on other distros.

### Third-party drivers, tools and reused code

- loqctl depends on the LenovoLegionLinux `legion_laptop` module, the mainline
  `lenovo-wmi-*` drivers, NVML/`nvidia-smi` and LegionAura. Check that every call matches
  how those projects actually behave: file names, units, value ranges and error codes. Flag
  assumptions that aren't backed by a citation in a comment or in `docs/adr/`.
- Flag conflicts between drivers, such as `legion_wmi` claiming the gamezone WMI GUID, and
  check that the user sees a clear explanation instead of a bare `EINVAL`.
- For code or algorithms taken from another repo, check the license (never GPL-2.0-only),
  the credit in the file header and `NOTICE`, and whether we reimplemented something the
  standard library already provides.

### Fan control UX

The fans use a third-party driver (`legion_laptop`), and the curve editor exposes the raw
firmware model: up/down temperatures, hysteresis, per-point speeds and accel/decel. That is
much harder to use than Lenovo Vantage, where the temperature points are fixed and the user
only sets the fan % at each point. Flag changes that make fan editing harder, and point out
where a Vantage-style mode (fixed temperatures, edit % only, sensible defaults, one-key reset
to the firmware curve) would remove a step or a way to make a mistake. Also check that the
unit shown (RPM, level or %) matches the curve's real scale.

### First-run UX and UI

- Missing drivers, missing permissions or an unsupported model produce a clear message with a
  next step, not an empty panel, a traceback or a misleading "no driver" label.
- Read-only states (non-`custom` modes, missing `legion_laptop`) are shown as read-only. They
  never look editable and then fail.
- After any write, the UI shows "applied" only once read-back has confirmed it, and the panel
  shows the new value.
- Colors use the ANSI palette only. Layout stays usable at 80×24.

### Tests

- New firmware-writing paths have a test against simulated sysfs. No test writes real
  firmware or calls real `sudo`.
- Cutting a redundant test is allowed when the review lists it with a reason.

## Scope

- On a diff, apply the checks above to the code the diff touches. Report wider problems found
  along the way as Pre-existing.
- On a path or whole-package target (for example `/code-review loqctl/`), run all the
  "Always check" sections across that path.

## Do not report

- Gitignored or local-only files: `todo`, `reports/`, `research_notes/`, `__pycache__/`.
- Agent tooling and specs under `.claude/`, and binary assets such as `preview.png`.
- `LICENSE` text.
- Third-party packages: loqctl is standard-library only, so a suggestion to add one is not a
  finding.

## Output

- Post every finding in the chat: severity, `file:line`, a one-sentence summary, a concrete
  failure scenario, and for data-provenance findings the source and driver. Don't shorten
  findings into a summary-only list.
- Also write the same review to `reports/review-<YYYY-MM-DD>-<branch>.md`. Start the file
  with the scope (target, commits, effort level), whether the tests passed, and which findings
  are unverified. Then list the findings, most severe first. `reports/` is gitignored.
- If the review reports through the `ReportFindings` tool, still write the markdown file.
