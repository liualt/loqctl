# AGENTS.md

## Aim
loqctl is a btop-style hardware monitor and control tool for Lenovo LOQ (and Legion) laptops on Linux.
It is one place to see temperatures, power draw, voltages, clocks, fans and throttling, next to the
firmware power/thermal limits and fan curves that cause them, with safe, verified writes.
It is not a process monitor; btop already does that. Meant to be installed by other people, including
on AMD CPUs and other distros.

## Hard rules (protect the hardware and the license)
- Never write `fanN_target` (it can latch the EC in manual mode until a full power cycle).
- loqctl makes no raw EC, /dev/port, /dev/mem or ACPI-call writes itself; fan curves go through
  the `legion_laptop` hwmon files.
- Never run firmware writes in tests; use simulated sysfs and mocked subprocesses.
- Never reuse GPL-2.0-only code (incompatible with GPL-3.0).

## Preferences (break one only when the benefit clearly outweighs the cost; say why in the commit)
- Standard library only (no third-party dependencies); Linux only; Python 3.10+. License
  GPL-3.0-or-later. Code may be reused from GPL-2.0-or-later, GPL-3.0, MIT, BSD or Apache-2.0
  projects; keep their copyright notices and credit the source in the file header and NOTICE.
- Firmware writes stay on explicit allowlists (`FIELDS` and the fan-curve files), bounds-checked and
  read-back verified.
- Limits are editable only in `custom` mode; other modes are firmware-managed and shown read-only.
- Missing readings show as N/A, not a guessed or zero value. UI colors use the terminal's ANSI palette only.
- Avoid passwordless sudoers rules for an editable script path.
- Redundant tests may be deleted to keep the suite lean; list each cut and why.

## Workflow
- Follow `CONTRIBUTING.md`. Every change starts from a GitHub issue and lands through a pull request
  that closes it. Never commit or push directly to `main`, except small docs or housekeeping
  changes that touch no code or tests (`loqctl/`, `loqctl.py`, `tests/`).
- One branch and one PR per issue, named `<type>/<issue>-<slug>` (for example `fix/2-null-curve-point`).
- Work in stages; a stage is one PR. Tests pass before every commit, with a clear message and
  description. PRs are squash-merged, so the PR title and description become the commit on `main`.
- Issue bodies use the bug report sections in `CONTRIBUTING.md` (Mozilla bug writing guidelines).
  Commit subjects and PR titles use Conventional Commits, and PR bodies follow the PR template.
- Run the tests the change affects (`python3 -m unittest tests.test_x 2>&1 | tail -3`); the full suite
  (`python3 -m unittest discover -s tests -v`) runs in CI and before a release.
- Working notes live in the gitignored `todo.md` file.

## Code review
- For any code review (including `/code-review` and `/review`), read `REVIEW.md` at the repo root
  before reviewing, and follow it for severity, checks, exclusions and output.
