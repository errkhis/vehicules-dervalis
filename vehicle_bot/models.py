from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    key: str
    title: str
    published_date: str
    deadline: str
    url: str


@dataclass(frozen=True)
class Details:
    estimation: str | None
    deadline: str
    documents: bool | None
    caution: str | None = None
    location: str | None = None
