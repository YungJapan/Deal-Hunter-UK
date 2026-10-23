# Deal Hunter UK V2

Python background scanner using the official eBay UK Browse API and Twilio WhatsApp Sandbox. No Telegram, scraping, automatic buying, deployment or account access. Rebuilt from the requested behaviour: the V1 source was not available in this workspace or the retrieved chat, so this is not a verified migration of its code or watchlist.

## Start here

Requires Python 3.11 or newer. No third-party packages required.

1. Extract the ZIP and open a terminal in the extracted project folder.
2. Run `python -m unittest discover -s tests -v`.
3. Run `python -m deal_hunter.app --demo`. This uses an invented offline listing and sends nothing. Expected margin: GBP 25.20.
4. Review `watchlist.json`. Its resale estimate and fee assumptions are illustrative, **not current valuations or fee advice**. Replace with your own researched values before using live results.
5. Use `.env.example` as a checklist to set environment variables. The program deliberately does not read `.env` automatically. Never commit credentials. For local PowerShell, set individual values with `$env:NAME='value'`; for bash, use `export NAME='value'`.
6. With eBay credentials set and `DRY_RUN=true`, run `python -m deal_hunter.app --once`. This calls eBay but sends nothing and records no duplicates. Omit `--once` for continuous scanning.
7. After configuring WhatsApp below, explicitly set `DRY_RUN=false` to enable sending. Stop with Ctrl+C.

## WhatsApp Sandbox setup

This is a testing integration, not an unattended production WhatsApp service. Activate your Twilio WhatsApp Sandbox yourself, then send `join <your sandbox code>` from the intended recipient phone. Use the sender number shown by your Twilio console, and set `TWILIO_WHATSAPP_FROM` and `TWILIO_WHATSAPP_TO` in `whatsapp:+<international number>` format. Set your Account SID and Auth Token privately.

Send a WhatsApp message to the Sandbox and set `WHATSAPP_LAST_INBOUND_UTC` to that actual inbound time as timezone-aware ISO 8601 UTC, for example `2026-10-23T10:00:00Z` only if that is when you actually sent it. The app pauses free-form alerts after 23 hours 55 minutes, allowing a five-minute safety buffer. Blank, invalid, future or expired timestamps pause sending. This manual timestamp is an operator assertion, not a verified webhook event. Update the process environment and restart when renewing the window. No public webhook is needed or provided.

Twilio requires a 24-hour customer service window for free-form messages. The Sandbox supports only its supplied templates outside that window; these are not appropriate substitutes for deal alerts. The app does not misuse them. Sandbox membership expires after three days; rejoin as needed. Sends are spaced at least 3.1 seconds apart. Twilio may charge for testing. Production use needs a registered sender and approved suitable templates, which this Sandbox edition does not implement.

An API-accepted alert is not proof of delivery. Check Twilio message logs for delivered/failed status. Delivery callbacks are not implemented. Membership expiry or asynchronous delivery failure can occur even when the local time window is valid.

## eBay access and scanning

Supply your eBay production application client ID and secret. The client obtains and caches an application OAuth token using client credentials and the `api_scope` scope. Calls only the production Browse API, with `X-EBAY-C-MARKETPLACE-ID: EBAY_GB`, fixed-price buying options and GB item location. No private accounts are accessed during offline tests. Obtain any required eBay production Buy API approvals and ensure your use, retention and application meet eBay's current agreement and API requirements; this code alone does not establish approval or legal compliance.

Default: one page of up to 50 results per rule every 900 seconds. `MAX_PAGES_PER_RULE` accepts 1–10; `SCAN_INTERVAL_SECONDS` has a 300-second floor. Requests have 30-second timeouts. Errors stop the current cycle and defer to the next interval, avoiding tight retry loops; a 401 invalidates the cached token for the next scan. One-shot provider failures return a nonzero exit status. Monitor your actual application quotas, throttle further if needed, and do not run extra copies. This bounded scan can miss listings and is not exhaustive or real time.

Only explicit GBP prices, known fixed GBP shipping, GB location, allowed condition IDs and matching title terms qualify. Missing shipping is rejected rather than assumed free. The lowest quoted fixed shipping option is used; verify it applies to your address before buying. Auctions, unknown conditions and non-UK links are rejected. Title matching cannot reliably distinguish bundles, misleading listings, variants, counterfeits or damage. Inspect every candidate manually.

## Margin model

`resale * (1 - fee_rate) - item_price - inbound_shipping - outbound_postage - packaging - fixed_fee - risk_reserve`

All money uses Decimal. Candidates must meet `max(20, MIN_MARGIN_GBP)` before display rounding. The £20 floor cannot be lowered by configuration. Fees and estimates are supplied per rule; no sold-price API or scraped comparable sales are claimed. Include your actual business fees, tax/VAT treatment, returns, repairs and other costs in your assumptions/reserves. The result is a modelled estimate, not guaranteed profit. Revise estimates when the market or fees change.

Each rule has a unique `id`, `query`, nonempty `required_terms`, `excluded_terms`, explicit eBay `condition_ids`, `resale_gbp`, `fee_rate` (0–1), `fixed_fee_gbp`, `outbound_postage_gbp`, `packaging_gbp` and `risk_reserve_gbp`. Costs must be finite and nonnegative.

## Persistent duplicates and recovery

SQLite stores only item IDs, state, Twilio message SID and update timestamp. No listing titles, tokens or phone numbers are stored in the database. IDs are globally unique across rules. Records are kept indefinitely by this version to avoid repeat alerts; review this minimal-ID retention strategy against your eBay agreement and operational needs.

An atomic unique insert claims an item as `pending` before sending. Accepted API responses become `accepted`; restarts and redeployments retain both states. Explicit HTTP 4xx rejection releases the claim for a later cycle. Timeouts, transport uncertainty, HTTP 5xx or a crash leave it pending: Twilio might already have accepted the message. This favours avoiding duplicates over guaranteeing every alert. There is no exactly-once delivery guarantee across an external API and SQLite.

For pending records, stop the worker, inspect Twilio logs using the item link/time, and reconcile before resending. With Python's sqlite3 or a SQLite client, inspect `SELECT item_id,state,sid,updated FROM alerts;`. If accepted, update state and SID; only delete a pending row when you have established no send occurred. Do not delete the entire database as a routine restart step. Accepted-but-undelivered messages also need manual review before resetting. Back up using SQLite's backup API while running, or stop the worker before copying the database and any WAL files. Losing the disk or restoring an old backup may cause repeat alerts. For multiple workers, replace this design with a shared durable database/outbox and reconciliation process.

## Render deployment preparation (not deployed)

`render.yaml` describes one paid background worker with a 1 GB persistent disk mounted at `/var/data`; the database path is `/var/data/alerts.sqlite3`. A background worker is intentional: Render cron jobs cannot attach persistent disks. No HTTP port or health endpoint is exposed. Only the mounted path survives redeploys, and disk-backed workers cannot scale to multiple instances.

When you choose to deploy yourself:

1. Back up your existing private `Deal-Hunter-UK` repository and inspect its V1 contents before replacing anything. Put these files at repository root on a review branch. Do not upload `.env` or state files. Existing Telegram credentials can be removed from the new service after you retire V1.
2. Connect that private repository in Render and create a Blueprint from `render.yaml`. Review the paid worker/disk charges before confirming. No repository URL or account identity is embedded here.
3. Supply the prompted credentials and WhatsApp recipient/time values through Render's private environment settings. Review the watchlist and keep `DRY_RUN=true` initially.
4. Inspect build test results and scan logs. The Blueprint build runs offline tests; state is created only at runtime. After your own live dry-run verification, set `DRY_RUN=false` and redeploy.
5. Disable the old V1 worker before enabling V2 alerts. Do not copy a V1 database blindly: its schema and duplicate semantics are unknown. If V1 becomes available, migrate IDs only after reviewing the schema.
6. Renew the Sandbox inbound session and timestamp as described above. Watch for paused-session logs and provider errors. There is no external failure notification channel in this version.

## Sources and validation scope

Official references checked during preparation:

- [eBay Browse API overview](https://www.developer.ebay.com/api-docs/buy/static/api-browse.html)
- [eBay Browse search reference](https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search)
- [Twilio WhatsApp Sandbox and limitations](https://www.twilio.com/docs/whatsapp/sandbox)
- [Render Blueprint specification](https://render.com/docs/blueprint-spec)
- [Render persistent disk limitations](https://render.com/docs/disks)

Tests cover margin arithmetic and hard threshold, rejection filters, dry-run isolation, duplicate persistence, uncertain-send recovery, session expiry, mocked Twilio requests and mocked eBay OAuth/pagination. Local validation uses mocks and offline fixtures only. It does not certify live credentials, eBay approval, delivery, current resale values or a successful Render deployment. No accounts were accessed and no messages were sent during validation.
