# Browser Acceptance

The committed Playwright suite runs the built React UI against the real local
Python API. The runner creates a temporary configuration and isolated analytics
and live-evidence stores, binds an ephemeral loopback port, and rejects outbound
Python socket connections. Browser requests must stay on that exact origin.
No venue credentials or live gates are needed. Do not point the suite at a
production service; its alert and wallet cases intentionally change local state.

After installing the repository's hash-locked Python test dependencies:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
cd frontend
npx playwright install --with-deps chromium
cd ..
python scripts/verify_browser_workflows.py
```

On Windows, an installed Edge may be used without downloading Chromium:

```powershell
$env:MARKET_SENTINEL_BROWSER_TEST_CHANNEL = "msedge"
python scripts/verify_browser_workflows.py
```

The runner accepts `--node` for a Node executable outside PATH and forwards
Playwright selectors such as `--project=desktop-dark` and `--grep=wallet`.
It fails if prerequisites are absent, any test fails, or the backend attempts
outbound traffic. CI runs this gate before publishing the frontend artifact.

Four projects exercise desktop (1440x1000) and mobile-width (390x844) layouts,
each with the saved light and dark themes. Tests cover all eight navigation
views, keyboard activation, tab order, accessible control names/current-page
state, actual palette application, reload and browser back/forward navigation,
alert/wallet create-edit-toggle-delete and persistence, rejected deletion,
deleting the currently edited record, failed-save form preservation, and invalid
paper/analytics input. Only the save-failure case injects an HTTP error; normal
CRUD and validation use the real backend. The tests do not silently retry.

Screenshots and failure traces are written below `frontend/test-results`; the
HTML report is below `frontend/playwright-report`. Both are ignored by Git and
excluded from source distributions. CI retains them as
`browser-workflow-evidence` for 14 days, including when a test fails.

These tests do not establish physical mobile-device acceptance, all browser
engines, native Tkinter DPI/packaging behavior, every adapter's controls, actual
venue data correctness, or real-money execution. They complement, rather than
replace, the existing component tests, Live Safety report smoke and mobile CDP
smoke matrix.
