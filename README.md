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

## Parallel Uploads

Question uploads support multi-threading to speed up large quizzes. The default
behavior uses a small worker pool and limits in-flight requests.

Defaults:

- `max_workers = 4`
- `max_in_flight = 8`

If any worker receives a Canvas `429`, all workers respect a shared backoff
window before continuing.

You can override these via `CanvasCourse.create_question(...)` or by calling
the lower-level `_upload_question_payloads(...)`.

## Vendoring

Use the shared script to vendor into another project:

```bash
python scripts/vendor_into_project.py /path/to/target --top-level
```
