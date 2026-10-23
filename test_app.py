import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from deal_hunter.app import (ApiError, Ebay, Store, WhatsApp, evaluate, load_rules,
                             scan, window_open)

ROOT = Path(__file__).resolve().parents[1]


class Tests(unittest.TestCase):
    def setUp(self):
        self.rule = load_rules(ROOT / "watchlist.json")[0]
        self.item = json.loads((ROOT / "tests/fixtures/items.json").read_text())[0]
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def notifier(self):
        return WhatsApp({"WHATSAPP_LAST_INBOUND_UTC": datetime.now(timezone.utc).isoformat(),
                         "TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_AUTH_TOKEN": "test",
                         "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
                         "TWILIO_WHATSAPP_TO": "whatsapp:+447000000000"})

    def test_margin_includes_all_costs(self):
        self.assertEqual(evaluate(self.item, self.rule)["margin"], D("25.20"))

    def test_exact_threshold_and_hard_floor(self):
        self.item["price"]["value"] = "105.20"
        self.assertEqual(evaluate(self.item, self.rule)["margin"], D("20.00"))
        self.item["price"]["value"] = "105.21"
        self.assertIsNone(evaluate(self.item, self.rule, D("1")))

    def test_unknown_shipping_currency_country_condition_auction(self):
        changes = [{"shippingOptions": []}, {"price": {"value": "1", "currency": "USD"}},
                   {"itemLocation": {"country": "US"}}, {"conditionId": "7000"},
                   {"buyingOptions": ["AUCTION"]}, {"itemWebUrl": "https://ebay.co.uk.evil.test/x"},
                   {"price": {"value": "NaN", "currency": "GBP"}},
                   {"title": "Nintendo Switch OLED console box only"}]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(evaluate(dict(self.item, **change), self.rule))

    def test_dry_run_does_not_claim_or_send(self):
        with patch.object(WhatsApp, "send") as send:
            self.assertEqual(scan(lambda _: [self.item], [self.rule], self.store, self.notifier()), 1)
            send.assert_not_called()
            self.assertTrue(self.store.claim(self.item["itemId"]))

    def test_duplicate_across_rules_and_scans(self):
        with patch.object(WhatsApp, "send", return_value="SMtest") as send:
            for _ in range(2):
                scan(lambda _: [self.item], [self.rule, self.rule], self.store, self.notifier(), dry_run=False)
            self.assertEqual(send.call_count, 1)

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite3")
            first = Store(path)
            self.assertTrue(first.claim("item"))
            first.close()
            second = Store(path)
            self.assertFalse(second.claim("item"))
            second.close()

    def test_uncertain_send_retains_pending(self):
        with patch.object(WhatsApp, "send", side_effect=ApiError("transport-uncertain")):
            with self.assertRaises(ApiError):
                scan(lambda _: [self.item], [self.rule], self.store, self.notifier(), dry_run=False)
        self.assertFalse(self.store.claim(self.item["itemId"]))

    def test_explicit_rejection_allows_later_retry(self):
        with patch.object(WhatsApp, "send", side_effect=ApiError(429)):
            with self.assertRaises(ApiError):
                scan(lambda _: [self.item], [self.rule], self.store, self.notifier(), dry_run=False)
        self.assertTrue(self.store.claim(self.item["itemId"]))

    def test_session_validation(self):
        now = datetime.now(timezone.utc)
        for value in ("", "garbage", "2026-01-01T12:00:00", (now + timedelta(hours=1)).isoformat(),
                      (now - timedelta(hours=24)).isoformat()):
            self.assertFalse(window_open({"WHATSAPP_LAST_INBOUND_UTC": value}, now))
        self.assertTrue(window_open({"WHATSAPP_LAST_INBOUND_UTC": now.isoformat()}, now))

    def test_expired_session_does_not_claim(self):
        notifier = self.notifier()
        notifier.env["WHATSAPP_LAST_INBOUND_UTC"] = ""
        with patch.object(WhatsApp, "send") as send:
            scan(lambda _: [self.item], [self.rule], self.store, notifier, dry_run=False)
            send.assert_not_called()
        self.assertTrue(self.store.claim(self.item["itemId"]))

    def test_twilio_request(self):
        from urllib.parse import parse_qs
        with patch("deal_hunter.app.request_json", return_value={"sid": "SMtest"}) as request:
            self.assertEqual(self.notifier().send(evaluate(self.item, self.rule)), "SMtest")
            url, headers, body = request.call_args.args
            self.assertTrue(url.endswith("/Messages.json"))
            fields = parse_qs(body.decode())
            self.assertIn("MODELLED", fields["Body"][0])
            self.assertEqual(fields["To"], ["whatsapp:+447000000000"])

    def test_ebay_oauth_pagination_and_marketplace(self):
        responses = [{"access_token": "fake", "expires_in": 7200},
                     {"itemSummaries": [self.item], "next": "ignored"}, {"itemSummaries": []}]
        with patch("deal_hunter.app.request_json", side_effect=responses) as request:
            client = Ebay({"EBAY_CLIENT_ID": "fake", "EBAY_CLIENT_SECRET": "fake"})
            self.assertEqual(len(list(client.search("console", 2))), 1)
            self.assertEqual(request.call_count, 3)
            self.assertEqual(request.call_args.args[1]["X-EBAY-C-MARKETPLACE-ID"], "EBAY_GB")
            self.assertIn("offset=50", request.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
