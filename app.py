"""Standard-library-only scanner. No HTML scraping or automatic purchases."""
import argparse
import base64
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("deal_hunter")
D = Decimal


def money(value):
    result = D(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Costs and estimates must be finite and nonnegative")
    return result


def load_rules(path):
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rules, list) or not rules:
        raise ValueError("Provide at least one watch rule")
    ids = set()
    for rule in rules:
        if rule["id"] in ids or not rule["query"].strip() or not rule["required_terms"]:
            raise ValueError("Rules need unique IDs, query and required_terms")
        ids.add(rule["id"])
        for key in ("resale_gbp", "outbound_postage_gbp", "packaging_gbp", "fixed_fee_gbp", "risk_reserve_gbp", "fee_rate"):
            rule[key] = money(rule[key])
        if rule["fee_rate"] > 1 or not rule.get("condition_ids"):
            raise ValueError("Set fee_rate between 0 and 1 and explicit condition_ids")
    return rules


def evaluate(item, rule, minimum=D("20")):
    """Reject ambiguous totals; estimates are manually supplied, never sold comps."""
    try:
        title = item["title"].casefold()
        if any(term.casefold() not in title for term in rule["required_terms"]):
            return None
        if any(term.casefold() in title for term in rule.get("excluded_terms", [])):
            return None
        if str(item.get("conditionId")) not in rule["condition_ids"]:
            return None
        if item.get("itemLocation", {}).get("country") != "GB" or "FIXED_PRICE" not in item.get("buyingOptions", []):
            return None
        if item["price"]["currency"] != "GBP":
            return None
        shipping = [money(s["shippingCost"]["value"]) for s in item.get("shippingOptions", [])
                    if s.get("shippingCost", {}).get("currency") == "GBP"
                    and s.get("shippingCostType") == "FIXED"]
        if not shipping:
            return None
        url = item["itemWebUrl"]
        host = (urlparse(url).hostname or "").lower()
        if urlparse(url).scheme != "https" or not (host == "ebay.co.uk" or host.endswith(".ebay.co.uk")):
            return None
        purchase = money(item["price"]["value"]) + min(shipping)
        margin = (rule["resale_gbp"] * (1 - rule["fee_rate"]) - purchase
                  - sum(rule[k] for k in ("outbound_postage_gbp", "packaging_gbp", "fixed_fee_gbp", "risk_reserve_gbp")))
        if margin < max(D("20"), minimum):
            return None
        return {"id": item["itemId"], "title": item["title"][:160], "url": url,
                "purchase": purchase, "margin": margin, "resale": rule["resale_gbp"]}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


class ApiError(Exception):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        super().__init__(f"Provider request failed (HTTP {status}, code {code})")


def request_json(url, headers=None, data=None):
    try:
        with urlopen(Request(url, data=data, headers=headers or {}), timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        # Never log raw responses: they can include recipient details or credentials.
        code = None
        try:
            code = json.loads(exc.read()).get("code")
        except (ValueError, AttributeError):
            pass
        raise ApiError(exc.code, code) from None
    except (URLError, TimeoutError, OSError):
        raise ApiError("transport-uncertain") from None


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class Ebay:
    def __init__(self, env):
        self.env, self.token, self.expiry = env, None, 0

    def headers(self):
        if time.time() >= self.expiry:
            result = request_json("https://api.ebay.com/identity/v1/oauth2/token", {
                "Authorization": basic(self.env["EBAY_CLIENT_ID"], self.env["EBAY_CLIENT_SECRET"]),
                "Content-Type": "application/x-www-form-urlencoded"},
                urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode())
            self.token = result["access_token"]
            self.expiry = time.time() + max(0, int(result["expires_in"]) - 60)
        return {"Authorization": "Bearer " + self.token, "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}

    def search(self, query, pages=1):
        for page in range(pages):
            params = urlencode({"q": query, "limit": 50, "offset": page * 50,
                                "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:GB"})
            try:
                result = request_json("https://api.ebay.com/buy/browse/v1/item_summary/search?" + params, self.headers())
            except ApiError as exc:
                if exc.status == 401:
                    self.expiry = 0
                raise
            yield from result.get("itemSummaries", [])
            if not result.get("next"):
                break


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS alerts (item_id TEXT PRIMARY KEY, state TEXT NOT NULL, sid TEXT, updated REAL NOT NULL)")
        # Pending records survive crashes and require manual reconciliation.
        self.db.commit()

    def claim(self, item_id):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO alerts VALUES (?, 'pending', NULL, ?)",
                                   (item_id, time.time())).rowcount == 1

    def finish(self, item_id, state, sid=None):
        with self.db:
            self.db.execute("UPDATE alerts SET state=?, sid=?, updated=? WHERE item_id=?", (state, sid, time.time(), item_id))

    def release(self, item_id):
        with self.db:
            self.db.execute("DELETE FROM alerts WHERE item_id=? AND state='pending'", (item_id,))

    def close(self):
        self.db.close()


def window_open(env, now=None):
    try:
        stamp = datetime.fromisoformat(env.get("WHATSAPP_LAST_INBOUND_UTC", "").replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        return 0 <= age < 23 * 3600 + 55 * 60
    except ValueError:
        return False


class WhatsApp:
    def __init__(self, env):
        self.env, self.last_send = env, 0

    def send(self, deal):
        if not window_open(self.env):
            raise ValueError("WhatsApp session expired; update confirmed last inbound timestamp")
        time.sleep(max(0, 3.1 - (time.monotonic() - self.last_send)))
        self.last_send = time.monotonic()
        body = (f"Deal Hunter UK | MODELLED estimate\n{deal['title']}\n"
                f"Buy + delivery: GBP {deal['purchase']:.2f}\nAssumed resale: GBP {deal['resale']:.2f}\n"
                f"Modelled margin: GBP {deal['margin']:.2f}\nVerify condition, delivery and costs before buying.\n{deal['url']}")
        result = request_json(f"https://api.twilio.com/2010-04-01/Accounts/{self.env['TWILIO_ACCOUNT_SID']}/Messages.json",
                              {"Authorization": basic(self.env["TWILIO_ACCOUNT_SID"], self.env["TWILIO_AUTH_TOKEN"]),
                               "Content-Type": "application/x-www-form-urlencoded"},
                              urlencode({"From": self.env["TWILIO_WHATSAPP_FROM"], "To": self.env["TWILIO_WHATSAPP_TO"], "Body": body}).encode())
        return result["sid"]


def scan(items, rules, store, notifier, minimum=D("20"), dry_run=True):
    count = 0
    for rule in rules:
        for item in items(rule["query"]):
            deal = evaluate(item, rule, minimum)
            if not deal:
                continue
            if dry_run:
                LOG.info("Dry-run candidate: %s, margin GBP %.2f", deal["id"], deal["margin"])
                count += 1
                continue
            if not window_open(notifier.env):
                LOG.warning("Alerts paused: WhatsApp session needs renewal")
                return count
            if not store.claim(deal["id"]):
                continue
            try:
                sid = notifier.send(deal)
            except ApiError as exc:
                if isinstance(exc.status, int) and 400 <= exc.status < 500:
                    store.release(deal["id"])
                # Transport/5xx ambiguity remains pending; no unsafe automatic resend.
                raise
            except ValueError:
                store.release(deal["id"])
                raise
            store.finish(deal["id"], "accepted", sid)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Offline fixtures; never sends or writes state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = os.environ
    rules = load_rules(env.get("WATCHLIST_PATH", "watchlist.json"))
    minimum = max(D("20"), money(env.get("MIN_MARGIN_GBP", "20")))
    interval = max(300, int(env.get("SCAN_INTERVAL_SECONDS", "900")))
    pages = int(env.get("MAX_PAGES_PER_RULE", "1"))
    if not 1 <= pages <= 10:
        raise ValueError("MAX_PAGES_PER_RULE must be 1..10")
    dry = env.get("DRY_RUN", "true").lower()
    if dry not in ("true", "false"):
        raise ValueError("DRY_RUN must be true or false")
    dry = dry == "true" or args.demo
    if not args.demo:
        for key in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET") + (() if dry else (
                "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO")):
            if not env.get(key):
                raise ValueError(f"Missing {key}")
        if not dry:
            if not re.fullmatch(r"AC[0-9a-fA-F]{32}", env["TWILIO_ACCOUNT_SID"]):
                raise ValueError("Invalid Twilio Account SID")
            for key in ("TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO"):
                if not re.fullmatch(r"whatsapp:\+[1-9][0-9]{7,14}", env[key]):
                    raise ValueError(f"Invalid {key}")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = Store(":memory:" if dry else env.get("DATABASE_PATH", "data/alerts.sqlite3"))
    ebay, notifier = Ebay(env), WhatsApp(env)
    try:
        while not stop.is_set():
            try:
                if args.demo:
                    fixture = json.loads(Path("tests/fixtures/items.json").read_text())
                    source = lambda query: fixture
                else:
                    source = lambda query: ebay.search(query, pages)
                count = scan(source, rules, store, notifier, minimum, dry)
                LOG.info("Scan complete: %d candidates/accepted alerts", count)
            except ApiError as exc:
                LOG.error("%s; waiting until next scan; check provider quotas/session", exc)
                if args.once or args.demo:
                    raise SystemExit(1) from None
            if args.once or args.demo:
                break
            stop.wait(interval)
    finally:
        store.close()


if __name__ == "__main__":
    main()
