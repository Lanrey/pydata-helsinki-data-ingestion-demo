# Contributing

Thanks for contributing.

## Setup

```bash
git clone <your-repo-url>
cd data-ingestion-pydata-helenski-demo
uv sync --project python
```

## Run locally

```bash
PYTHONPATH=python/src uv run --project python agentctl --help
PYTHONPATH=python/src uv run --project python python -m agent_backend.session_mcp_server --help
PYTHONPATH=python/src uv run --project python python -m agent_backend.bridge_mcp_server --help
```

## Style

- Keep changes focused and minimal.
- Prefer clear error messages.
- Update docs with behavior changes.

## Testing

```bash
PYTHONPATH=python/src uv run --project python python -m unittest discover -s python/src -p 'test*.py'
```
