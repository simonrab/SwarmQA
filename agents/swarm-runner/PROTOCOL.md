# Swarm runner wire protocol, version 1

The swarm runner is an HTTP server hosted inside an XCUITest process on an iOS Simulator or a Mac. It drives one app through `XCUIApplication(bundleIdentifier:)`. The Python client lives in `swarmqa/driver/runner_client.py`. `swarmqa/driver/runner_schema.py` holds the Python types and is the reference for field names; this file and that module must change together.

## Transport

- HTTP/1.1 on `127.0.0.1:<port>`. The port is passed to the runner in the `SWARM_RUNNER_PORT` environment variable. Default `8765`.
- Request and response bodies are JSON (`Content-Type: application/json`, UTF-8), except `GET /screenshot`, which returns `image/png`.
- Every request can carry `X-Swarm-Protocol: 1`. A runner that does not speak that version answers `426` with error code `protocol_mismatch`.
- Requests are handled one at a time. The client never pipelines.

## Coordinates and time

- Points, not pixels. Origin at the top-left of the screen (iOS) or of the main display (macOS). The same space as element frames.
- `ts` is Unix epoch seconds as a float, taken on the device when the tree snapshot was taken.

## Element

```json
{
  "role": "button",
  "label": "Save",
  "identifier": "settings.save",
  "value": null,
  "enabled": true,
  "frame": [16.0, 700.0, 358.0, 44.0],
  "children": []
}
```

`role` is the lower-cased `XCUIElement.ElementType` name without the prefix: `button`, `statictext` becomes `text`, `textfield`, `securetextfield`, `switch`, `cell`, `image`, `window`, `menuitem`, `other`, and so on. `frame` is `[x, y, width, height]` or `null`. `label`, `identifier` and `value` are strings or `null`.

## Query

Wherever a request targets an element, it sends a query. At least one field is set and all set fields must match, using the same rules as `swarmqa.driver.query`: `role` equal ignoring case, `label` a case-insensitive substring, `identifier` and `value` exact. When more than one element matches, the runner acts on the first in tree order.

```json
{"role": "button", "label": "Save", "identifier": null, "value": null}
```

## Endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /health` | — | `{"protocol_version": 1, "runner_version": "…", "platform": "ios" \| "macos", "app_state": "not_running" \| "running" \| "crashed"}` |
| `POST /launch` | `{"bundle_id": "…", "args": [], "env": {}, "terminate_existing": true}` | `{"ok": true}` |
| `POST /terminate` | `{}` | `{"ok": true}` |
| `GET /tree` | — | `{"ts": 0.0, "elements": [Element]}` |
| `POST /observe` | `{"screenshot": "png" \| "jpeg" \| "none", "jpeg_quality": 0.7}` | `{"ts": 0.0, "elements": [Element], "size": [w, h], "scale": 3.0, "screenshot": {"format": "png", "data": "<base64>"} \| null}` |
| `POST /tap` | `{"x": 0.0, "y": 0.0}` or `{"query": Query}` | `{"ok": true}` |
| `POST /type` | `{"text": "…", "query": Query \| null}` | `{"ok": true}` |
| `POST /swipe` | `{"from": [x, y], "to": [x, y], "duration_s": 0.3}` | `{"ok": true}` |
| `POST /key` | `{"keys": ["cmd", "s"]}` | `{"ok": true}` |
| `GET /screenshot` | — | PNG bytes |

- `/observe` takes the tree snapshot and the screenshot back to back in one request. This is the call the agent loop makes every step, and its budget is 300 ms on the fixture app.
- `/type` with a query taps the element first. Without one it types into the focused element.
- `/key` names: `cmd`, `ctrl`, `alt`, `shift`, `return`, `escape`, `tab`, `delete`, `up`, `down`, `left`, `right`, `home` (iOS home button), and single characters.

## Errors

Any failure answers a 4xx or 5xx status with:

```json
{"error": {"code": "not_found", "message": "no element matches {\"label\": \"Save\"}"}}
```

| Code | Status | Meaning |
|---|---|---|
| `bad_request` | 400 | Malformed JSON or missing fields |
| `not_found` | 404 | No element matches the query |
| `not_launched` | 409 | No app has been launched |
| `app_crashed` | 409 | The app is no longer running |
| `timeout` | 504 | The app did not become idle in time |
| `protocol_mismatch` | 426 | Unsupported `X-Swarm-Protocol` |
| `internal` | 500 | Anything else |
