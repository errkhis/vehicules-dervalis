import json
import os
import sys
import unittest
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle_bot.config import Config, ConfigError, ROOT
from vehicle_bot.matching import Matcher, normalize
from vehicle_bot.models import Details, Notice
from vehicle_bot.portal import Portal, PortalError, parse_details, parse_listing, soup_of
from vehicle_bot.service import CASABLANCA, run, scan_start
from vehicle_bot.storage import MESSAGE_VERSION, Store
from vehicle_bot.telegram import DeliveryError, Telegram, format_message
from api.check import app, authorized, safe_database_message

TODAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 12, tzinfo=CASABLANCA)


def notice(key="org:1", title="Acquisition de véhicules"):
    return Notice(key, title, TODAY.isoformat(), "08/10/2026 11:30",
                  "https://www.marchespublics.gov.ma/?page=entreprise.EntrepriseDetailConsultation&refConsultation=1&orgAcronyme=org")


def listing_html(ref=1, procedure="AOS", published="25/09/2026", pages=1):
    return f'''<form action=""><input name="PRADO_PAGESTATE" value="state">
    <span id="ctl0_CONTENU_PAGE_resultSearch_nombreElement">{pages}</span>
    <span id="ctl0_CONTENU_PAGE_resultSearch_nombrePageTop">{pages}</span>
    <table class="table-results"><tr><th>Header</th></tr><tr>
    <td></td><td>{procedure} ... Fournitures {published}</td>
    <td><div id="row_panelBlocObjet">Objet : Achat...<div id="row_infosBullesObjet">Achat de véhicules &amp; ambulances</div></div>
    Acheteur public : Service des véhicules</td><td>Rabat</td><td>08/10/2026<br>11:30</td>
    <td><a href="?page=entreprise.EntrepriseDetailConsultation&amp;refConsultation={ref}&amp;orgAcronyme=org">Détail</a></td>
    </tr></table></form>'''


def detail_html(value="-", include_docs=True, fields=""):
    docs = f"Prospectus, notices ou autres documents : {value} Réunion : -" if include_docs else ""
    return f'''<div>Procédure : Appel d'offres ouvert simplifié | Sur offre de prix</div>
    <div>Estimation (en Dhs TTC) * : 385 000,00</div>
    <span id="summary_dateHeureLimiteRemisePlis">08/10/2026 11:30</span>
    <div>{fields} Date et heure limite : 08/10/2026 11:30 {docs}</div>'''


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.matcher = Matcher.from_file(ROOT / "keywords.json")

    def test_accents_case_plurals_typos_and_arabic(self):
        for title in ["VÉHICULES", "vehicules", "ve\u0301hicule", "achat d’un forgon",
                      "Location de PICK-UP", "Pièces pour camions", "Entretien du parc automobile",
                      "اقتناء سيارات الإسعاف", "transport scolaire"]:
            with self.subTest(title=title):
                self.assertTrue(self.matcher.matches(title))

    def test_whole_words_avoid_false_matches(self):
        for title in ["Fourniture de buses", "Entretien des arbustes", "Achat de mobilier", "motopompes"]:
            self.assertFalse(self.matcher.matches(title))

    def test_exclusions_and_normalization(self):
        self.assertEqual(normalize("  VÉHICULE—ÉLECTRIQUE "), "vehicule electrique")
        self.assertFalse(Matcher(["voiture"], ["location"]).matches("LOCATION de voiture"))


class ConfigTests(unittest.TestCase):
    def test_missing_settings_error_names_only_missing_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                Config.from_env()
        self.assertEqual(
            str(ctx.exception),
            "Missing Vercel setting: DATABASE_URL or POSTGRES_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CRON_SECRET",
        )

    def test_uses_supabase_postgres_url_when_database_url_is_not_set(self):
        values = {
            "POSTGRES_URL": "postgresql://example",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_CHAT_ID": "chat",
            "CRON_SECRET": "more-than-sixteen-characters",
        }
        with patch.dict(os.environ, values, clear=True), patch("vehicle_bot.config.load_env"):
            self.assertEqual(Config.from_env().database_url, "postgresql://example")


class ParserTests(unittest.TestCase):
    def test_title_uses_full_tooltip_without_buyer_or_duplicate(self):
        items, total, rows = parse_listing(soup_of(listing_html()), TODAY, TODAY)
        self.assertEqual((total, rows), (1, 1))
        self.assertEqual(items[0].title, "Achat de véhicules & ambulances")
        self.assertEqual(items[0].deadline, "08/10/2026 11:30")
        self.assertEqual(items[0].key, "org:1")

    def test_exact_day_and_aos_filter(self):
        self.assertEqual(parse_listing(soup_of(listing_html(published="26/09/2026")), TODAY, TODAY)[0], [])
        with self.assertRaises(PortalError):
            parse_listing(soup_of(listing_html(procedure="AOO")), TODAY, TODAY)

    def test_error_page_is_not_zero_results(self):
        with self.assertRaises(PortalError):
            parse_listing(soup_of("<h1>Service unavailable</h1>"), TODAY, TODAY)
        empty = '<div id="ctl0_CONTENU_PAGE_resultSearch_panelNoElementFound">Aucun résultat</div>'
        self.assertEqual(parse_listing(soup_of(empty), TODAY, TODAY), ([], 0, 0))

    def test_documents_yes_no_unknown(self):
        for value, expected in [("-", False), ("—", False), ("Non", False),
                                ("Néant", False), ("Déposer les fiches techniques", True)]:
            with self.subTest(value=value):
                details = parse_details(soup_of(detail_html(value)), notice())
                self.assertIs(details.documents, expected)
                self.assertEqual(details.estimation, "385 000,00")
        self.assertIsNone(parse_details(soup_of(detail_html(include_docs=False)), notice()).documents)

    def test_caution_value_stops_before_following_location(self):
        fields = "Caution provisoire : 500,00 Lieu d'exécution : Rabat"
        details = parse_details(soup_of(detail_html(fields=fields)), notice())
        self.assertEqual(details.caution, "500,00")
        self.assertEqual(details.location, "Rabat")

    def test_caution_stops_before_qualifications_and_agreements(self):
        fields = "Caution provisoire : 20 000,00 MAD Qualifications : - Agréments : -"
        details = parse_details(soup_of(detail_html(fields=fields)), notice())
        self.assertEqual(details.caution, "20 000,00 MAD")

    def test_location_value_stops_before_following_caution(self):
        fields = "Lieu d’exécution : Salé Caution provisoire : 600,00"
        details = parse_details(soup_of(detail_html(fields=fields)), notice())
        self.assertEqual(details.location, "Salé")
        self.assertEqual(details.caution, "600,00")

    def test_missing_empty_and_dash_caution_and_location_are_none(self):
        cases = [
            "",
            "Caution provisoire : Lieu d'exécution :",
            "Caution provisoire : - Lieu d'exécution : -",
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                details = parse_details(soup_of(detail_html(fields=fields)), notice())
                self.assertIsNone(details.caution)
                self.assertIsNone(details.location)

    def test_details_three_argument_call_remains_compatible(self):
        details = Details("385 000,00", "08/10/2026 11:30", False)
        self.assertIsNone(details.caution)
        self.assertIsNone(details.location)

    def test_mixed_encoding_preserves_vehicle_words(self):
        raw = "<div>véhicules سيارة</div>".encode("utf-8") + b"<p>caf\xe9</p>"
        self.assertEqual(soup_of(raw).get_text(" "), "véhicules سيارة café")
        self.assertIn("véhicule", soup_of("<p>véhicule</p>".encode("cp1252")).get_text())

    def test_detail_error_not_aos(self):
        with self.assertRaises(PortalError):
            parse_details(soup_of("<h1>Access denied</h1>"), notice())

    def test_pagination_and_repeated_page_detection(self):
        search = soup_of('<form><select name="ctl0$CONTENU_PAGE$AdvancedSearch$procedureType"></select></form>')
        portal = Portal()
        try:
            with patch.object(portal, "_request", return_value=search), patch.object(portal, "_submit", side_effect=[
                    soup_of(listing_html(1, pages=2)), soup_of(listing_html(2, pages=2))]) as submit:
                self.assertEqual(len(portal.listing(TODAY, TODAY)), 2)
                self.assertEqual(submit.call_args.args[1]["ctl0$CONTENU_PAGE$resultSearch$numPageTop"], "2")
            with patch.object(portal, "_request", return_value=search), patch.object(portal, "_submit", return_value=soup_of(listing_html(1, pages=2))):
                with self.assertRaisesRegex(PortalError, "repeated"):
                    portal.listing(TODAY, TODAY)
        finally:
            portal.close()


class MemoryStore:
    """Durable state across service invocations, without a live database."""
    def __init__(self):
        self.rows = {}
        self.previous = None
        self.busy = False

    def acquire(self):
        return not self.busy

    def last_scan(self):
        return self.previous

    def remember(self, notices, matcher, scan_date):
        new = matches = 0
        for n in notices:
            if n.key not in self.rows:
                match = bool(matcher.matches(n.title))
                self.rows[n.key] = {"notice": n, "status": "pending" if match else "skipped", "message": None}
                new += 1
                matches += match
        self.previous = scan_date
        return {"new_notices": new, "new_matches": matches}

    def pending(self, limit):
        return [(r["notice"], r["message"]) for r in self.rows.values() if r["status"] == "pending"][:limit]

    def cache_message(self, key, message):
        self.rows[key]["message"] = message

    def sending(self, key):
        self.rows[key]["status"] = "sending"

    def sent(self, key, message_id):
        self.rows[key]["status"] = "sent"

    def failure(self, key, reason, uncertain=False, retry_seconds=600):
        self.rows[key]["status"] = "uncertain" if uncertain else "pending"

    def counts(self):
        return {status: sum(r["status"] == status for r in self.rows.values())
                for status in ["pending", "sent", "skipped", "uncertain"]}


class StorageCacheTests(unittest.TestCase):
    def test_stale_message_is_regenerated_and_cache_gets_current_version(self):
        stale_row = {
            "notice": asdict(notice()),
            "message": "old cached text",
            "message_version": MESSAGE_VERSION - 1,
        }
        store = Store.__new__(Store)
        store.chat_id = "test-chat"
        store.conn = Mock()

        def fake_execute(query, params):
            if "SELECT notice" in query:
                self.assertIn("CASE WHEN message_version=%s THEN message ELSE NULL END", query)
                row = dict(stale_row)
                if row["message_version"] != params[0]:
                    row["message"] = None
                result_set = Mock()
                result_set.fetchall.return_value = [row]
                return result_set
            return None

        store.conn.execute.side_effect = fake_execute
        store.acquire = Mock(return_value=True)
        store.remember = Mock(return_value={"new_notices": 0, "new_matches": 0})
        store.sending = Mock()
        store.sent = Mock()
        store.failure = Mock()
        store.counts = Mock(return_value={})

        portal = Mock(requests=1)
        portal.listing.return_value = []
        portal.details.return_value = Details(
            "385 000,00", "08/10/2026 11:30", False,
            caution="500,00", location="Rabat",
        )
        sender = Mock()
        sender.send.return_value = 42

        with patch("vehicle_bot.service.time.sleep"):
            result = run(store, portal, Matcher(["vehicule"], []), sender,
                         now=NOW, target_date=TODAY)

        self.assertEqual(result["sent"], 1)
        portal.details.assert_called_once()
        self.assertIn("500,00", sender.send.call_args.args[0])
        self.assertIn("Rabat", sender.send.call_args.args[0])
        self.assertEqual(store.conn.execute.call_args_list[1].args[1][1], MESSAGE_VERSION)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.portal = Mock(requests=3)
        self.portal.listing.return_value = [notice(), notice("org:2", "Achat de mobilier")]
        self.portal.details.return_value = Details("100 000,00", "08/10/2026 11:30", False)
        self.sender = Mock()
        self.sender.send.return_value = 42
        self.matcher = Matcher.from_file(ROOT / "keywords.json")
        self.sleep = patch("vehicle_bot.service.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def check(self, **kwargs):
        return run(self.store, self.portal, self.matcher, self.sender, now=NOW, **kwargs)

    def test_two_runs_send_only_once_and_skip_non_vehicle_details(self):
        first = self.check()
        second = self.check()
        self.assertEqual((first["sent"], second["sent"]), (1, 0))
        self.assertEqual(second["new_notices"], 0)
        self.portal.details.assert_called_once()
        self.sender.send.assert_called_once()

    def test_failed_details_are_retried(self):
        self.portal.details.side_effect = [PortalError("temporary"), Details(None, "soon", None)]
        self.assertFalse(self.check()["ok"])
        self.assertEqual(self.check()["sent"], 1)

    def test_rejected_delivery_retries_cached_message(self):
        self.sender.send.side_effect = [DeliveryError("rate limit"), 42]
        self.assertFalse(self.check()["ok"])
        self.assertEqual(self.check()["sent"], 1)
        self.portal.details.assert_called_once()

    def test_uncertain_delivery_is_not_automatically_duplicated(self):
        self.sender.send.side_effect = DeliveryError("timeout", uncertain=True)
        self.check()
        self.check()
        self.sender.send.assert_called_once()
        self.assertEqual(self.store.counts()["uncertain"], 1)

    def test_overlapping_run_skips_network(self):
        self.store.busy = True
        self.assertIn("skipped", self.check())
        self.portal.listing.assert_not_called()

    def test_failed_listing_does_not_advance_checkpoint_but_drains_queue(self):
        self.store.remember([notice()], self.matcher, date(2026, 9, 24))
        self.portal.listing.side_effect = PortalError("maintenance")
        self.assertEqual(self.check()["sent"], 1)
        self.assertEqual(self.store.previous, date(2026, 9, 24))

    def test_budget_defers_instead_of_losing_notice(self):
        self.assertTrue(self.check(budget_seconds=-1)["deferred"])
        self.assertEqual(self.store.counts()["pending"], 1)
        self.sender.send.assert_not_called()

    def test_midnight_and_downtime_catchup(self):
        midnight = NOW.replace(hour=0)
        self.assertEqual(scan_start(midnight, None), date(2026, 9, 24))
        self.assertEqual(scan_start(NOW, date(2026, 9, 23)), date(2026, 9, 23))
        with self.assertRaises(ValueError):
            scan_start(NOW, date(2026, 9, 1))


class TelegramTests(unittest.TestCase):
    def test_caution_and_location_are_html_escaped(self):
        fields = ("Caution provisoire : 500 &lt;DH&gt; &amp; taxes "
                  "Lieu d'exécution : Rabat &lt;centre&gt; &amp; Salé")
        details = parse_details(soup_of(detail_html(fields=fields)), notice())
        message = format_message(notice(), details)
        self.assertIn("500 &lt;DH&gt; &amp; taxes", message)
        self.assertIn("Rabat &lt;centre&gt; &amp; Salé", message)
        self.assertNotIn("500 <DH>", message)
        self.assertNotIn("Rabat <centre>", message)

    def test_message_escape_and_unknown_fields(self):
        msg = format_message(notice(title="Véhicule <test> & matériel"), Details(None, "soon", None))
        self.assertIn("&lt;test&gt; &amp;", msg)
        self.assertIn("Non indiqué", msg)
        self.assertNotIn("<test>", msg)

    def test_api_error_http_200_is_not_success(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": False, "error_code": 429, "parameters": {"retry_after": 1200}}
        with patch("vehicle_bot.telegram.requests.post", return_value=response):
            with self.assertRaises(DeliveryError) as ctx:
                Telegram("secret", "group").send("test")
            self.assertEqual(ctx.exception.retry_seconds, 1200)
            self.assertFalse(ctx.exception.uncertain)

    def test_network_error_hides_token_and_marks_uncertain(self):
        with patch("vehicle_bot.telegram.requests.post", side_effect=requests.Timeout("secret token URL")):
            with self.assertRaises(DeliveryError) as ctx:
                Telegram("secret", "group").send("test")
            self.assertNotIn("secret", str(ctx.exception))
            self.assertTrue(ctx.exception.uncertain)

    def test_authorization_requires_the_exact_url_secret(self):
        self.assertFalse(authorized(None, ""))
        self.assertFalse(authorized("wrong", "secret"))
        self.assertTrue(authorized("secret", "secret"))

    def test_fastapi_check_route_exists(self):
        self.assertIn("/api/check", [route.path for route in app.routes])

    def test_database_message_hides_connection_password(self):
        error = Exception("could not connect to postgresql://user:password@example.com:5432/postgres")
        self.assertEqual(
            safe_database_message(error),
            "could not connect to postgresql://***@example.com:5432/postgres",
        )


if __name__ == "__main__":
    unittest.main()
