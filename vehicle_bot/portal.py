"""PMMP search through its public HTML forms; no browser process needed."""
import re
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .matching import normalize
from .models import Details, Notice

BASE = "https://www.marchespublics.gov.ma/"
SEARCH = BASE + "index.php?page=entreprise.EntrepriseAdvancedSearch&searchAnnCons"
PREFIX = "ctl0$CONTENU_PAGE$"
DATE_RE = r"\d{2}/\d{2}/\d{4}"


class PortalError(RuntimeError):
    pass


def soup_of(content):
    if isinstance(content, bytes):
        # PMMP sometimes mixes UTF-8 content and legacy Windows-1252 bytes.
        # Whole-page encoding detection turns valid "véhicule" into "vÃ©hicule".
        content = content.decode("utf-8", errors="surrogateescape")
        content = "".join(bytes([ord(c) - 0xDC00]).decode("cp1252", errors="replace")
                          if 0xDC80 <= ord(c) <= 0xDCFF else c for c in content)
    return BeautifulSoup(content, "lxml")


def clean(text):
    return " ".join(text.split())


def form_data(soup):
    form = soup.find("form")
    if not form:
        raise PortalError("Search form missing; website may have changed")
    result = {}
    # Unchanged dropdown/radio values live in PRADO_PAGESTATE. Posting all the
    # unrelated search widgets can activate extra filters (including empty ones).
    # Preserve input state and explicitly submit only the controls we change.
    for node in form.select("input[name]"):
        if node.has_attr("disabled"):
            continue
        kind = node.get("type", "text").lower()
        if kind in {"submit", "image", "button", "reset", "file", "checkbox", "radio"}:
            continue
        result[node["name"]] = node.get("value", "")
    return result


def official_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "www.marchespublics.gov.ma":
        raise PortalError("Unexpected portal link")
    return url


def parse_listing(soup, start: date, end: date):
    count_node = soup.find(id="ctl0_CONTENU_PAGE_resultSearch_nombreElement")
    if count_node is None and soup.find(id="ctl0_CONTENU_PAGE_resultSearch_panelNoElementFound"):
        return [], 0, 0
    if count_node is None or not count_node.get_text(strip=True).isdigit():
        raise PortalError("Result count missing; refusing to treat an error page as an empty search")
    count = int(count_node.get_text(strip=True))
    table = soup.select_one("table.table-results")
    if count and table is None:
        raise PortalError("Results table missing")
    notices = []
    row_count = 0
    for row in table.select("tr") if table else []:
        link = row.select_one('a[href*="EntrepriseDetailConsultation"]')
        if not link:
            continue
        row_count += 1
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            raise PortalError("Unexpected listing columns")
        meta = clean(cells[1].get_text(" ", strip=True))
        if not re.match(r"AOS\b", meta):
            raise PortalError("AOS filter was not applied")
        dates = re.findall(DATE_RE, meta)
        if not dates:
            raise PortalError("Publication date missing")
        day, month, year = map(int, dates[-1].split("/"))
        published = date(year, month, day)
        if not start <= published <= end:
            continue
        # The full title is in the tooltip; visible text may be shortened.
        title_node = cells[2].find(id=re.compile(r"_infosBullesObjet$"))
        if not title_node:
            title_node = cells[2].find(id=re.compile(r"panelBlocObjet$"))
        if title_node:
            title = clean(title_node.get_text(" ", strip=True))
            title = re.sub(r"^Objet\s*:\s*", "", title, flags=re.I)
        else:
            text = clean(cells[2].get_text(" ", strip=True))
            match = re.search(r"Objet\s*:\s*(.*?)(?:Acheteur public\s*:|$)", text, re.I)
            if not match:
                raise PortalError("Notice title missing")
            title = match.group(1).strip()
        url = official_url(urljoin(BASE, link["href"]))
        query = parse_qs(urlsplit(url).query)
        reference = (query.get("refConsultation") or [""])[0]
        org = (query.get("orgAcronyme") or query.get("orgAccronyme") or [""])[0]
        if not reference or not org or not title:
            raise PortalError("Notice identifier or title missing")
        due = re.search(DATE_RE + r"(?:\s+\d{2}:\d{2})?", cells[4].get_text(" ", strip=True))
        notices.append(Notice(org + ":" + reference, title, published.isoformat(),
                              clean(due.group()) if due else "Non indiquée", url))
    if count and not row_count:
        raise PortalError("No readable rows despite a nonzero result count")
    return notices, count, row_count


def parse_details(soup, notice):
    text = clean(soup.get_text(" ", strip=True))
    if "appel d offres ouvert simplifie" not in normalize(text):
        raise PortalError("Detail page is missing or is not an AOS notice")
    estimation = re.search(r"Estimation\s*\([^)]*\)\s*\*?\s*:\s*(\d+(?:[ .]\d{3})*(?:,\d{1,2}|\.\d{2})?)", text, re.I)
    deadline_node = soup.find(id=re.compile(r"_dateHeureLimiteRemisePlis$"))
    deadline = clean(deadline_node.get_text(" ", strip=True)) if deadline_node else notice.deadline
    docs = re.search(r"Prospectus,\s*notices\s+ou\s+autres\s+documents\s*:\s*(.*?)\s*(?:Date et heure limite|Réunion|Reunion|Visites des lieux|Variante)\s*:", text, re.I)
    documents = None
    if docs:
        value = clean(docs.group(1))
        documents = normalize(value) not in {"", "non", "neant", "aucun", "aucune", "sans objet"}
    field_labels = (
        r"Estimation\s*(?:\([^)]*\))?\s*\*?",
        r"Adresse\s+de\s+retrait\s+des\s+dossiers",
        r"Adresse\s+de\s+d[eé]p[oô]t\s+des\s+offres",
        r"Lieu\s+d'ouverture\s+des\s+plis",
        r"Lieu\s+d['’]ex[eé]cution",
        r"Prix\s+d'acquisition\s+des\s+plans",
        r"Caution\s+provisoire",
        r"Prospectus,\s*notices\s+ou\s+autres\s+documents",
        r"Date\s+et\s+heure\s+limite(?:\s+de\s+remise\s+des\s+plis)?",
        r"R[eé]union",
        r"Visites\s+des\s+lieux",
        r"Variante",
        r"Contact\s+Administratif",
    )

    def field_value(label):
        stops = "|".join(field_labels)
        match = re.search(rf"{label}\s*:\s*(.*?)(?=\s*(?:{stops})\s*:|$)", text, re.I)
        if not match:
            return None
        value = clean(match.group(1)).strip(" :")
        if normalize(value) in {"", "-", "neant", "non indique", "n/a"}:
            return None
        return value

    location = field_value(r"Lieu\s+d['’]ex[eé]cution")
    if location:
        # PMMP sometimes repeats the same location from a tooltip in page text.
        words = location.split()
        midpoint = len(words) // 2
        if len(words) % 2 == 0 and normalize(" ".join(words[:midpoint])) == normalize(" ".join(words[midpoint:])):
            location = " ".join(words[:midpoint])
    caution = field_value(r"Caution\s+provisoire")
    return Details(clean(estimation.group(1)) if estimation else None, deadline,
                   documents, caution=caution, location=location)


class Portal:
    def __init__(self, max_pages=100, budget_seconds=180):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "fr-FR,fr;q=0.9"})
        self.session.mount("https://", HTTPAdapter(max_retries=Retry(
            total=1, backoff_factor=0.5, status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET", "POST"], respect_retry_after_header=False)))
        self.max_pages = max_pages
        self.requests = 0
        self.deadline = time.monotonic() + budget_seconds

    def _request(self, url, data=None):
        if time.monotonic() > self.deadline:
            raise PortalError("Portal time budget exceeded; retry next check")
        self.requests += 1
        response = self.session.request("POST" if data is not None else "GET",
                                        official_url(url), data=data, timeout=(5, 20))
        response.raise_for_status()
        official_url(response.url)
        return soup_of(response.content)

    def _submit(self, soup, changes, event=""):
        fields = form_data(soup)
        fields.update({"PRADO_POSTBACK_TARGET": event, "PRADO_POSTBACK_PARAMETER": ""})
        fields.update(changes)
        return self._request(urljoin(SEARCH, soup.find("form").get("action") or SEARCH), fields)

    def listing(self, start: date, end: date):
        soup = self._request(SEARCH)
        prefix = PREFIX + "AdvancedSearch$"
        if not soup.find("select", attrs={"name": prefix + "procedureType"}):
            raise PortalError("Procedure filter missing")
        soup = self._submit(soup, {
            prefix + "procedureType": "50",
            prefix + "dateMiseEnLigneStart": "",
            prefix + "dateMiseEnLigneEnd": "",
            prefix + "dateMiseEnLigneCalculeStart": start.strftime("%d/%m/%Y"),
            prefix + "dateMiseEnLigneCalculeEnd": (end + timedelta(days=1)).strftime("%d/%m/%Y"),
            prefix + "lancerRecherche": "Lancer la recherche",
        })
        _, total, _ = parse_listing(soup, start, end)
        size = PREFIX + "resultSearch$listePageSizeTop"
        size_control = soup.find("select", attrs={"name": size})
        selected_size = size_control.find("option", selected=True) if size_control else None
        if total > 10 and size_control and (selected_size is None or selected_size.get("value") != "500"):
            soup = self._submit(soup, {size: "500"}, size)
        found = {}
        signatures = set()
        seen_links = set()
        scanned_rows = 0
        for page in range(1, self.max_pages + 1):
            notices, total, row_count = parse_listing(soup, start, end)
            signature = tuple(a["href"] for a in soup.select('a[href*="EntrepriseDetailConsultation"]'))
            if signature in signatures and row_count:
                raise PortalError("Pagination repeated a page")
            if seen_links.intersection(signature):
                raise PortalError("Results shifted between pages; retry the scan")
            signatures.add(signature)
            seen_links.update(signature)
            scanned_rows += row_count
            found.update({notice.key: notice for notice in notices})
            pages_node = soup.find(id="ctl0_CONTENU_PAGE_resultSearch_nombrePageTop")
            if total and pages_node is None:
                raise PortalError("Page count missing")
            pages = int(pages_node.get_text(strip=True)) if pages_node else 1
            if page >= pages:
                if scanned_rows != total:
                    raise PortalError("Result count changed or rows are missing; retry the scan")
                return list(found.values())
            prefix = PREFIX + "resultSearch$"
            soup = self._submit(soup, {prefix + "numPageTop": str(page + 1),
                                      prefix + "DefaultButtonTop": ""})
        raise PortalError("Page limit exceeded; scan was not completed")

    def details(self, notice):
        return parse_details(self._request(notice.url), notice)

    def close(self):
        self.session.close()
