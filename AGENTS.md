# Repository Guidelines

## Project Structure & Module Organization

This repository is a fork of the original ARPA Emilia-Romagna Weather integration by davidecavestro, adapted for ARPAE realtime weather and forecast data. Integration code lives in `custom_components/arpa_emilia_romagna_weather/`:

- `__init__.py` registers YAML configuration and platform setup.
- `coordinator.py` contains the data update coordinator and API fetching logic.
- `const.py` holds domain, endpoint URLs, and sensor definitions.
- `sensor.py` defines exposed Home Assistant sensor entities.
- `weather.py` defines the weather forecast entity.
- `config_flow.py` manages the Home Assistant config flow.
- `manifest.json` and `hacs.json` hold integration metadata.

Tests live in `tests/` (to be added). User-facing setup notes are in `README.md`.

## Build, Test, and Development Commands

Create a local Python environment before installing dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite with:

```bash
pytest -q
```

Run a focused test file while iterating:

```bash
pytest tests/test_api.py
```

Run clean verification with Podman:

```bash
podman run --rm -v "$PWD:/workspace:Z" -w /workspace registry.access.redhat.com/ubi9/python-314 bash -lc 'python -m venv /tmp/venv && source /tmp/venv/bin/activate && pip install -e ".[dev]" >/tmp/pip.log && pytest -q && python -m compileall -q custom_components tests'
```

For manual Home Assistant testing, copy the integration into a Home Assistant config directory:

```bash
cp -R custom_components/arpa_emilia_romagna_weather /config/custom_components/arpa_emilia_romagna_weather
```

Then configure the `arpa_emilia_romagna_weather:` YAML block as shown in `README.md` or migrate to the new `arpae_weather:` block.

## Coding Style & Naming Conventions

Use Python 3.14 syntax and standard Home Assistant async patterns. Keep indentation at four spaces. Use `snake_case` for functions, variables, and test names; use `PascalCase` for classes; keep constants uppercase in `const.py`. Prefer typed dataclasses or explicit parser helpers when shaping ARPAE payloads, and keep network access separate from pure parsing logic so tests stay fast.

## Testing Guidelines

Tests use `pytest`. Name tests `test_<behavior>` and keep fixtures or builders local when they are only used by one file, as in `make_bulletin()`. Add parser tests for malformed, missing, or alternate ARPAE fields before changing parsing behavior. Run `pytest` before opening a PR.

## Commit & Pull Request Guidelines

Use concise imperative commit subjects, for example `Add alert severity parser tests`. Pull requests should describe the behavior change, list validation commands, and call out Home Assistant configuration or entity changes. Include screenshots only when sensor presentation or dashboard examples change.

## Security & Configuration Tips

Do not commit Home Assistant secrets, tokens, or private `/config` files. Keep `manifest.json` metadata aligned with `pyproject.toml` version changes, and avoid adding runtime dependencies unless they are required by Home Assistant or the integration.

## External Data Use

The default `scan_interval` is 2 hours. Keep polling conservative and do not add bulk extraction behavior. ARPAE bulletin and realtime data come from:

- Forecast / bulletin endpoint: `https://apps.arpae.it/REST/meteo_bollettini/?sort=-_id&max_results=1`
- Air quality station endpoint: `https://apps.arpae.it/REST/qa_stazioni`
- Realtime observation endpoint: `https://dati-simc.arpae.it/opendata/osservati/meteo/realtime/realtime.jsonl`
- Alert endpoint: `https://allertameteo.regione.emilia-romagna.it/o/get-stato-allerta`

ARPAE bulletin data is daily/slot-based; do not add hourly or nowcast forecast entities unless the source data actually supports that behavior. Refer to the actual ARPAE REST endpoints documented in the gist at `https://gist.github.com/frazei/0eaac760d20b2278bb0d074a39884d82` when adapting API calls.

## Proof-Based Endpoint Rule (No Guessing)

Only use endpoints that are directly observable with proof (successful HTTP response or documented reference). Do not invent URLs based on patterns. Before committing endpoint changes, provide a verifiable call result (e.g., `curl -I` or `fetch`) confirming the endpoint responds and returns the expected format.

When guessing is necessary (e.g., adapting from a different region), state it explicitly as unverified, then verify with a real call before any claim. Never present unverified endpoints as truthful in code or documentation.

Verified observable endpoints at time of writing:
- `https://apps.arpae.it/REST/meteo_bollettini/?sort=-_id&max_results=1` (forecast bulletin; observable via beelzetron repo and ARPAE dataset pages)
- `https://apps.arpae.it/REST/qa_stazioni` (air quality stations; observable via gist and dataset pages)
- `https://dati-simc.arpae.it/opendata/osservati/meteo/realtime/realtime.jsonl` (realtime JSONL; observable via open-data portal resource page)
- `https://allertameteo.regione.emilia-romagna.it/o/get-stato-allerta` (alert state; observable via developer docs page)

Not observable / unverified:
- `https://api.arpae.it/REST/v1` (transport error / unreachable) - do not use.
