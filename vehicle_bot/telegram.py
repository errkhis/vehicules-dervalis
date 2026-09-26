import html

import requests


class DeliveryError(RuntimeError):
    def __init__(self, message, uncertain=False, retry_seconds=600):
        super().__init__(message)
        self.uncertain = uncertain
        self.retry_seconds = retry_seconds


def format_message(notice, details):
    esc = html.escape
    docs = {True: "Oui", False: "Non", None: "Non indiqué"}[details.documents]
    # Bound title length before escaping; Telegram counts rendered characters.
    title = notice.title if len(notice.title) <= 1800 else notice.title[:1797] + "..."
    estimation = (details.estimation + " DH TTC") if details.estimation else "Non indiquée"
    return (f"🚗 <b>Nouvel appel d'offres AOS</b>\n\n<b>{esc(title)}</b>\n\n"
            f"💰 Estimation : {esc(estimation)}\n"
            f"📅 Date limite : {esc(details.deadline)}\n"
            f"📂 Prospectus / notices / autres documents : <b>{docs}</b>\n\n"
            f'<a href="{esc(notice.url, quote=True)}">Voir l’avis officiel</a>')


class Telegram:
    def __init__(self, token, chat_id):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, message):
        try:
            response = requests.post(self.url, json={"chat_id": self.chat_id,
                "text": message, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}},
                timeout=(5, 20))
        except requests.RequestException:
            # Never log the requests exception: its URL contains the bot token.
            raise DeliveryError("Telegram connection failed; delivery outcome unknown", uncertain=True) from None
        try:
            data = response.json()
        except ValueError:
            raise DeliveryError("Invalid Telegram response; delivery outcome unknown", uncertain=True) from None
        if data.get("ok") is not True:
            code = data.get("error_code", response.status_code)
            retry = max(600, int(data.get("parameters", {}).get("retry_after", 600)))
            raise DeliveryError(f"Telegram rejected request (code {code})",
                                uncertain=response.status_code >= 500, retry_seconds=retry)
        message_id = data.get("result", {}).get("message_id")
        if not isinstance(message_id, int):
            raise DeliveryError("Telegram omitted message ID", uncertain=True)
        return message_id
