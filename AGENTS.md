# AGENTS.md

## Aim
loqctl is a terminal dashboard and CLI for Lenovo LOQ laptops on Linux: live CPU/GPU/memory/battery
telemetry next to firmware power and thermal limits, with safe, verified writes. Meant to be installed
by other people, including on AMD CPUs and other distros.

## Constraints
- Python standard library only. No third-party dependencies.
- Linux only. Minimum Python 3.8.
- Firmware writes stay on the explicit `FIELDS` allowlist, bounds-checked and read-back verified. No raw EC writes.
- Limits are only editable in `custom` mode. Other modes are firmware-managed and shown read-only.
- UI colors use the terminal's ANSI palette only.
- Never run firmware writes in tests; use simulated sysfs and mocked subprocesses.
- Never add a passwordless sudoers rule for an editable script path.

## Workflow
- Work in stages. One commit per stage, tests passing first, with a clear message and description.
- Run tests with `python3 -m unittest discover -s tests -v`.
- Working notes live in the gitignored `todo` file.
