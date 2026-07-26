# Intraday Trading Agent

AI-assisted intraday trading agent for NSE/BSE — FastAPI service.

See `docs/ARCHITECTURE_AUDIT.md` and `docs/PHASE_AUDIT.md` for the current state of the codebase.

## Development setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[research]" --group dev
copy .env.example .env
```

Fill in broker credentials in `.env`, then run the test suite:

```
pytest
```
