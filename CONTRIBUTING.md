# Contributing to Kronos

Thanks for your interest in Kronos. Contributions of every kind are
welcome: bug reports, documentation fixes, new constraint checks,
policy ideas, and operational feedback from real clouds.

## Reporting issues

Open an issue at https://github.com/kronos-openstack/kronos/issues.
For bugs, include the Kronos version, the OpenStack release, and -
when planning behaves unexpectedly - a `kronos-record` snapshot if
you can share one (scrub hostnames if needed). A snapshot lets us
replay your exact planning cycle offline with `kronos-replay`.

## Development setup

Python 3.12+ is required.

```bash
git clone https://github.com/kronos-openstack/kronos.git
cd kronos
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Before you submit

All four gates must pass; CI runs the same commands:

```bash
pytest tests/
ruff check kronos/ tests/
mypy kronos/
pyright kronos/ tests/
```

New code needs type hints on public APIs and tests for new behavior.
Follow the conventions of the module you are editing: the project
uses oslo.config, oslo.log, and oslo.messaging throughout, and
ASCII-only punctuation in code, comments, and documentation. If you
change configuration options, regenerate the config reference:

```bash
oslo-config-generator --config-file etc/oslo-config-generator/kronos.conf
```

## Sign your work (DCO)

Every commit must carry a Developer Certificate of Origin sign-off:

```bash
git commit -s
```

This appends a `Signed-off-by:` line with your name and email,
certifying that you have the right to submit the change under the
project license (see https://developercertificate.org/). CI checks
every commit in a pull request and fails on missing sign-offs.

## Pull requests

- Keep each PR to one logical change.
- Describe the operational motivation, not just the code change.
- The project targets OpenStack ecosystem conventions; expect review
  feedback aimed at keeping it that way.

## License

By contributing you agree that your contributions are licensed under
the Apache License 2.0, the same license as the project.
