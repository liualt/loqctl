# AGENTS.md

## Aim
loqctl is a btop-style hardware monitor and control tool for Lenovo LOQ (and Legion) laptops on Linux.
It is one place to see temperatures, power draw, voltages, clocks, fans and throttling, next to the
firmware power/thermal limits and fan curves that cause them, with safe, verified writes.
It is not a process monitor; btop already does that. Meant to be installed by other people, including
on AMD CPUs and other distros.

## Constraints
- Python standard library only. No third-party dependencies.
- Linux only. Minimum Python 3.10.
- License GPL-3.0-or-later. Code may be reused from GPL-2.0-or-later, GPL-3.0, MIT, BSD or Apache-2.0
  projects. Keep their copyright notices and list the source in the file header and in NOTICE.
  Never reuse GPL-2.0-only code.
- Firmware writes stay on explicit allowlists (`FIELDS`, and the fan-curve files), bounds-checked and
  read-back verified. loqctl makes no raw EC, /dev/port, /dev/mem or ACPI-call writes itself.
  Fan curves go through the LenovoLegionLinux `legion_laptop` kernel module's hwmon files.
- Limits are only editable in `custom` mode. Other modes are firmware-managed and shown read-only.
- Never write `fanN_target` (it can latch the EC in manual mode until a full power cycle).
- Readings the system does not expose show as N/A, never as a guessed or zero value.
- UI colors use the terminal's ANSI palette only.
- Never run firmware writes in tests; use simulated sysfs and mocked subprocesses.
- Never add a passwordless sudoers rule for an editable script path.

## Workflow
- Work in stages. One commit per stage, tests passing first, with a clear message and description.
- Run tests with `python3 -m unittest discover -s tests -v`.
- Working notes live in the gitignored `todo` file.
