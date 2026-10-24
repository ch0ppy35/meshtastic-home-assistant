# Contribution guidelines

Contributing to this project should be as easy and transparent as possible, whether it's:

- Reporting a bug
- Discussing the current state of the code
- Submitting a fix
- Proposing new features

## Github is used for everything

Github is used to host code, to track issues and feature requests, as well as accept pull requests.

Pull requests are the best way to propose changes to the codebase.

1. Fork the repo and create your branch from `main`.
2. If you've changed something, update the documentation.
3. Make sure your code lints (using `scripts/lint`).
4. Test you contribution.
5. Issue that pull request!

## Any contributions you make will be under the MIT Software License

In short, when you submit code changes, your submissions are understood to be under the same [MIT License](http://choosealicense.com/licenses/mit/) that covers the project. Feel free to contact the maintainers if that's a concern.

## Report bugs using Github's [issues](../../issues)

GitHub issues are used to track public bugs.
Report a bug by [opening a new issue](../../issues/new/choose); it's that easy!

## Write bug reports with detail, background, and sample code

**Great Bug Reports** tend to have:

- A quick summary and/or background
- Steps to reproduce
  - Be specific!
  - Give sample code if you can.
- What you expected would happen
- What actually happens
- Notes (possibly including why you think this might be happening, or stuff you tried that didn't work)

People *love* thorough bug reports. I'm not even kidding.

## Use a Consistent Coding Style

Use [Ruff](https://docs.astral.sh/ruff/) to make sure the code follows the style.

## Test your code modification

### Automated tests

The test suite needs no Meshtastic hardware and no running Home Assistant. Home Assistant is
started in-process by
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component),
and the radio is stood in for. Sockets are blocked for the whole suite, so a test cannot reach
a real device even by accident — see `tests/test_hermetic.py`.

Work in a virtual environment; installing Home Assistant into a system Python tends to end
badly. Python 3.12 matches the `target-version` in `.ruff.toml`.

```shell
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements.test.txt -r requirements.protobuf.txt
.venv/bin/pytest
```

`tests/test_send_end_to_end.py` covers the outbound path the way a dashboard drives it, by
calling the real services and reading the result back out of the recorder through the logbook.
When changing how messages are sent or logged, prefer extending that file over testing against
a device: a check that needs a node on the desk is one nobody else can run.

### Manual testing

This custom component is based on [integration_blueprint template](https://github.com/ludeeus/integration_blueprint).

It comes with development environment in a container, easy to launch
if you use Visual Studio Code. With this container you will have a stand alone
Home Assistant instance running and already configured with the included
[`configuration.yaml`](./config/configuration.yaml)
file.

Use it for the things automated tests cannot judge — how something looks and feels in the UI,
and whether real firmware behaves as expected. It is not where correctness gets established.

## License

By contributing, you agree that your contributions will be licensed under its MIT License.
