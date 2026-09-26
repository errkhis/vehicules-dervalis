import json
import re
import unicodedata
from pathlib import Path


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ـ", "").replace("œ", "oe").replace("ى", "ي")
    return " ".join(re.sub(r"[^\w\s]", " ", text).replace("_", " ").split())


class Matcher:
    def __init__(self, include: list[str], exclude: list[str]):
        if not include:
            raise ValueError("Vehicle keyword list is empty")
        self.include = self._patterns(include)
        self.exclude = self._patterns(exclude)

    @staticmethod
    def _patterns(words):
        return [(word, re.compile(r"(?<!\w)" + re.escape(normalize(word)) + r"(?!\w)"))
                for word in words if normalize(word)]

    @classmethod
    def from_file(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["include"], data.get("exclude", []))

    def matches(self, title: str) -> list[str]:
        text = normalize(title)
        if any(pattern.search(text) for _, pattern in self.exclude):
            return []
        return [word for word, pattern in self.include if pattern.search(text)]
