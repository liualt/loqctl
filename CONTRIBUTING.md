# Contributing to loqctl

loqctl writes to laptop firmware. These rules keep that safe and keep `main` working.
Every change, including the maintainer's, follows this process.

## The short version

1. Open an issue for the problem or feature.
2. Make a branch from an up-to-date `main` for that issue.
3. Make the change with tests, then open a pull request that closes the issue.
4. Review, fix and wait for the checks, then squash-merge.

Nobody commits or pushes directly to `main`, with one exception: the maintainer may push small
docs or housekeeping changes (typos, wording, process files like this one) straight to `main`.
Anything that touches `loqctl/`, `loqctl.py` or `tests/` always goes through an issue and a PR.

## 1. Issues

Every problem gets an issue before it gets code: bugs, review findings, features, docs and
clean-ups. One issue covers one problem. If a review turns up ten findings, open ten issues.

Bug reports follow the [Mozilla bug writing guidelines](https://bugzilla.mozilla.org/page.cgi?id=bug-writing.html),
and the bug report form in `.github/ISSUE_TEMPLATE` asks for the same sections.

The title is under 60 characters, names the feature and the symptom, and describes the problem
rather than the fix. "Null fan curve point is written to firmware as 'None'" is a good title.
"fancurve bug" and "Add a None check" are not.

The body has these sections, in this order:

| Section | Contents |
| --- | --- |
| Summary | One or two sentences on what is wrong. |
| Steps to reproduce | The smallest set of steps, or the simulated-sysfs setup, that shows the bug. |
| Actual results | What loqctl did, with errors pasted as text. |
| Expected results | What it should have done. |
| Environment | Laptop model, BIOS, CPU, distro, kernel, loqctl version, and the `legion_laptop` version if fans are involved. |
| Details | `file:line` references, a possible fix, the REVIEW.md severity, and what still needs real hardware to confirm. |

Feature requests use the feature request form: the problem, the proposed behavior, the data
source or driver, and alternatives.

Labels:

| Label | Use |
| --- | --- |
| `bug`, `enhancement`, `documentation`, `performance` | The kind of issue |
| `important` | Important under the REVIEW.md severity rules |
| `needs hardware` | Needs a real LOQ or Legion machine to confirm or test |
| `good first issue`, `help wanted` | Open to new contributors |

Work on `important` issues first, hardware-risk ones (firmware writes, `fanN_target`, rollback)
before the rest.

Close an issue without a PR only with a comment saying why (duplicate, invalid, won't fix).

## 2. Branches

Branch from the latest `main`. Name the branch `<type>/<issue>-<short-slug>`:

| Type | Use |
| --- | --- |
| `fix/` | Bug fixes |
| `feat/` | New features |
| `docs/` | Documentation only |
| `refactor/` | No behavior change |
| `test/` | Tests only |
| `chore/` | Tooling, repo housekeeping |

For example: `fix/2-null-curve-point`.

```sh
git switch main && git pull --ff-only
git switch -c fix/2-null-curve-point
```

One branch per issue. Don't mix unrelated fixes into one branch.

## 3. Making the change

- Read `AGENTS.md` first. Its hard rules can't be broken: no `fanN_target` writes, no raw
  EC, `/dev/port`, `/dev/mem` or ACPI-call writes, no real firmware writes in tests, and no
  GPL-2.0-only code.
- Standard library only, Linux only, Python 3.10+.
- Firmware writes stay on the allowlists (`FIELDS` and the fan-curve files), are bounds-checked
  and are read back to verify.
- Missing readings show N/A. UI colors use the ANSI palette only.
- Every fix gets a test that fails without the fix. Firmware-writing paths are tested against
  simulated sysfs with mocked subprocesses.
- Run the full suite before every commit:

  ```sh
  python3 -m unittest discover -s tests -v
  ```

- If the change is user-visible, add a line to the README changelog and bump `VERSION` in
  `loqctl/__init__.py` (patch for fixes, minor for features).
- Reused code: check the license, keep the copyright notice, and credit the source in the file
  header and in `NOTICE`.

## 4. Commits

Commits on a branch can be small. They are squashed when the PR merges, so the PR title and
description are what end up on `main`.

Commit subjects and PR titles follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<scope>): <description>

<body>

<footers>
```

- The type is `fix`, `feat`, `docs`, `refactor`, `perf`, `test`, `build`, `ci` or `chore`. A
  `fix` is a patch release and a `feat` is a minor release.
- The scope is the module the change is about: `fancurve`, `curvestore`, `hardware`, `sensors`,
  `api`, `ui`, `keyboard`, `cli` or `capdata`. Leave it out for changes across many modules.
- The description is lower case, imperative, has no full stop, and keeps the whole subject under
  about 72 characters. Add the version in brackets when the change bumps it:
  `fix(fancurve): reject null curve points (0.4.2)`.
- A `!` after the scope, or a `BREAKING CHANGE:` footer, marks a change that breaks the CLI, the
  HTTP API or saved files.
- The body explains what was wrong, why, what changed per module, and what the tests cover.
- Footers reference issues: `Fixes #2`, or `Refs #3` for related work that doesn't close it.

## 5. Pull requests

Open the PR against `main` once the tests pass. The PR template in `.github` has the sections.

- The title is a Conventional Commits subject. It becomes the commit subject on `main`.
- The description starts with a GitHub [closing keyword](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue),
  such as `Fixes #2`, so merging closes the issue. Use one keyword per issue.
- Then come Summary (what was wrong and why), Changes (per module), Testing (the unittest run
  and any real hardware, with model and BIOS), and Notes (anything unverified, any `AGENTS.md`
  preference broken and why, any test removed and why).
- Keep PRs small: one issue, one stage. Split large work into several issues and PRs.
- Open a draft PR to share work in progress.

```sh
git push -u origin fix/2-null-curve-point
gh pr create --base main --title "fix(fancurve): reject null curve points (0.4.2)"
```

## 6. Review

- Every PR is reviewed against `REVIEW.md` before it merges. Important findings block the
  merge. Nits are optional.
- Fix review findings with new commits on the same branch. Don't force-push once review has
  started unless you're asked to.
- Findings that are real but outside the PR's scope become new issues, not extra changes in
  this PR.

## 7. Merging

- Squash-merge only, once review is done and the tests pass. The commit on `main` uses the PR
  title and description.
- Delete the branch after merging.
- Check that the issue closed. If it didn't, close it with a link to the PR.

```sh
gh pr merge --squash --delete-branch
```

## Reporting security problems

For anything that could let a user gain root through the API socket or sudo path, or damage
hardware, open a GitHub security advisory (Security tab, "Report a vulnerability") instead of
a public issue.

## License

Contributions are licensed GPL-3.0-or-later, the same as the project.
