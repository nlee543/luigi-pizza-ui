# Luigi’s Pizza UI

A Flask + Jinja customer and staff ordering interface for Luigi’s Pizza API. No JavaScript framework, Node build, CDN, external font, or frontend package installation is needed. Vanilla JavaScript adds order-status polling, local timestamps, and submit-button feedback. All core forms work without JavaScript.

## Run locally (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:API_BASE_URL = 'http://127.0.0.1:8000'
$env:API_CSRF_PROTECTION = '1'
.\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:5000. Keep the API running on port 8000. The UI contacts the API from Python, so browser CORS configuration is not needed. Public menu pages display a clear availability message if the API is offline; there is no fallback mock menu in the application.

## Implemented flows

- Public menu: pizza sizes and current prices, topping selection, drinks, quantity controls, and basket editing.
- Customers: registration, login, logout, required temporary-password changes, password changes, checkout, order history, and order details.
- Tracking: actual API `status`, refreshed every 15 seconds while the tab is visible, with completed orders stopping polling. Pickup and delivery share a branching fulfillment stage. Without JavaScript, the status is visible and a refresh link is provided.
- Staff/admin: protected kitchen dashboard, paginated customer search, customer selection, ordering through `POST /staff/orders`, and status updates through `PATCH /orders/{id}/status`.
- Accessible labels, keyboard focus, skip navigation, responsive CSS, error messages, and preserved baskets after API errors.

Staff sign in with the same API login endpoint; the API role determines access. Staff can build a basket, find/select a customer, and submit for that customer. The API remains authoritative for customer ownership, staff authorization, availability, and accepted status changes.

## Authentication and CSRF

The browser receives an opaque `luigi_ui` session ID. API `luigi_session` cookies and `csrf_token` stay in the server-side session store under ignored `instance/sessions/`; they are not placed in browser local storage or Flask’s client-side signed cookie. Passwords are sent directly to the API and never stored by this UI. Login rotates the UI session ID and form token; logout clears local session data even if the API is unavailable.

Every browser mutation requires a per-session form CSRF token. API requests send `X-CSRF-Protection` (configurable, default `1`) and, once signed in, the API-returned `X-CSRF-Token`. Protected pages revalidate the user with `/auth/me`, including role, active state, and mandatory password-change state. API redirects are not followed. Responses include a self-only content security policy, frame protection, and no-store caching for pages.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Trusted API origin; keep this server-controlled. |
| `API_CSRF_PROTECTION` | `1` | Login/registration preflight-protection header value; confirmed as `1` for this API. |
| `SECRET_KEY` | Random per process | Set a stable high-entropy deployment secret. |
| `COOKIE_SECURE` | Off | Set to `1` behind HTTPS. |

The filesystem session store is suitable for this local single-host implementation. Before deployment, use an appropriately protected shared session store for multiple hosts, HTTPS, a stable secret, API-side login rate limiting, and a production WSGI server (for example Waitress on Windows). Do not expose Flask’s development server publicly. Exclude `instance/` from backups or sharing unless handled as sensitive session data; OneDrive users should place production session storage outside a synchronized directory. Sessions expire after eight hours.

## API boundaries and assumptions

The checked-in `openapi.json` is the local API contract inspected during implementation.

- The API has no fulfillment-selection field, payment endpoint, tax/fee breakdown, order total, or price snapshot. The UI does not submit invented fields or collect payment. Basket amounts are **current menu subtotals**, displayed in dollars as an initial assumption. Historical orders show contents, not reconstructed historical totals.
- Toppings have names and IDs but no prices. No topping surcharge is invented.
- Status values are `order_received`, `preparing`, `out_for_delivery`, `ready_for_pickup`, and `completed`. No delivery estimate or intermediate baking status is fabricated.
- Staff menu/inventory administration, customer profile editing, and staff-assisted customer registration are not part of this initial ordering-focused UI.
- A basket token and disabled submit button prevent ordinary repeat submissions, but the API has no documented idempotency key. Concurrent requests or a timeout after the API accepted an order cannot be made exactly-once by this UI. After an ambiguous submission failure, check order history before resubmitting. API idempotency is recommended before production use.
- The login/registration CSRF header value is confirmed as `1`. Live login/logout and protected-page access were verified with customer and admin test accounts; a distinct staff account has not been tested.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Tests mock API responses and verify payloads, role gates, CSRF protection, session rotation and confidentiality, registration/password/logout flows, basket validation, failure handling, and tracker output. The live public API menu and browser basket flow were also checked. Live integration checks through the Flask test client verified customer/admin login and logout, order-history pages, admin kitchen/customer-search access, and rejection of customer access to staff pages (403). Neither account required a password change. With explicit authorization for the non-production API, 53 live checks passed on October 24, 2026: customer and admin-assisted checkout, persisted order ownership/contents/creator, duplicate-submit rejection, pickup and delivery status transitions, tracker endpoint updates, cross-customer order/detail polling denial, registration, password changes, and old-password rejection. Completed test orders #102 and #103 remain available for inspection. Temporary test customer #23 was deactivated; supplied account passwords were unchanged, and test sessions were logged out. These checks exercised Flask routes with a real API; automatic browser timer behavior was not part of this integration run. Credentials are not stored in project files.

The opt-in live test runner is `tests/live_check.py`; it is not collected by pytest. It reads a JSON object from stdin with `admin` and `customer` entries, each containing `email` and `password`. Invoke from the repository root using `.\.venv\Scripts\python.exe -c "import runpy; runpy.run_path('tests/live_check.py', run_name='__main__')"`. Run only against an explicitly authorized non-production API: each run creates two orders and a temporary customer, then deactivates that temporary customer on success. Credentials are never included in its reports.
