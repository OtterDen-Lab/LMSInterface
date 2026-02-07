# LMSInterface

Lightweight LMS abstraction focused on Canvas. This repo is primarily used
as a vendored dependency in teaching tools.

## Usage

Set Canvas credentials in `~/.env` (or pass `env_path` to `CanvasInterface`):

- `CANVAS_API_URL`
- `CANVAS_API_KEY`

For production, use:

- `CANVAS_API_URL_prod`
- `CANVAS_API_KEY_prod`

## Tests

Run:

```bash
pytest
```

Tests live in `lms_interface/tests/` so they vendor cleanly.

## Vendoring

Use the shared script to vendor into another project:

```bash
python scripts/vendor_into_project.py /path/to/target --top-level
```
