# Contributing to Wiki-Weaver

> **Experimental software.** Wiki-Weaver is shared openly as an experimental exploration.
> See [SUPPORT.md](SUPPORT.md) for the support policy.

We welcome contributions. This guide covers the CLA requirement, code of conduct,
dev environment setup, and how to run the test suite.

## Contributor License Agreement

Most contributions require you to agree to a Contributor License Agreement (CLA)
declaring that you have the right to, and actually do, grant us the rights to use
your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you
need to provide a CLA and decorate the PR appropriately (status check, comment).
Simply follow the instructions provided by the bot. You will only need to do this
once across all repos using Microsoft's CLA.

## Code of Conduct

This project has adopted the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the
[Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or contact
[opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions
or comments.

## Development Setup

Wiki-Weaver runs on top of [Amplifier](https://github.com/microsoft/amplifier).
The ingest pipeline calls into `amplifier_foundation` and `unified_llm`, which `pyproject`
declares as `@main` git deps — so `uv tool install git+...` (and `uv tool upgrade wiki-weaver`)
resolve them — while an installed Amplifier still supplies provider keys and the engine bundle
at runtime.

```bash
# Clone and enter the repo
git clone https://github.com/microsoft/amplifier-app-wiki-weaver.git
cd amplifier-app-wiki-weaver

# Run tests (deterministic subset — no LLM calls, no API key required)
python -m pytest eval/ -q

# Run tests with the Amplifier Python interpreter if installed
/path/to/amplifier/bin/python3 -m pytest eval/ -q
```

The deterministic tests (policy, provenance, ingest-drain, any-text ingest) run without
an API key or Amplifier installation. Tests that invoke the LLM pipeline are automatically
skipped when `amplifier_foundation` is not importable — CI runs the deterministic majority.

## Running the Full Pipeline

To run actual wiki ingest (requires Amplifier + an LLM provider API key). These commands run
from a clone via `python -m wiki_weaver <command>`; installed users invoke the same commands as
`wiki-weaver <command>`:

```bash
# Environment preflight
python -m wiki_weaver doctor

# Create a wiki and ingest a source
python -m wiki_weaver init   runs/my-wiki
cp /path/to/article.md runs/my-wiki/_inbox/
python -m wiki_weaver ingest --wiki runs/my-wiki
```

See [DEMO.md](DEMO.md) for a full demo walkthrough and [docs/](docs/) for design docs.

## Pull Requests

1. Fork the repo and create a branch from `main`.
2. Add or update tests for any new functionality.
3. Run `pytest eval/ -q` and confirm all deterministic tests pass.
4. Submit a pull request — the CLA bot will prompt you if needed.

## Questions

Open an [issue](https://github.com/microsoft/amplifier-app-wiki-weaver/issues) for
bugs, feature requests, or questions.
