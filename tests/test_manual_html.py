"""오프라인 HTML 사용설명서의 파일 구조와 내부 링크 검증."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MAIN_MANUAL = ROOT / "사용설명서.html"
DETAIL_DIR = ROOT / "html"
DETAIL_PAGES = {
    "처음사용하기.html",
    "청구서.html",
    "확인서.html",
    "일일배송.html",
    "식수현황.html",
    "문제해결.html",
}


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title_parts: list[str] = []
        self._in_title = False
        self.ids: set[str] = set()
        self.references: list[str] = []
        self.has_viewport = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "html":
            self.lang = attr_map.get("lang") or ""
        if tag == "title":
            self._in_title = True
        if attr_map.get("id"):
            self.ids.add(str(attr_map["id"]))
        if tag in {"a", "link", "script"}:
            reference = attr_map.get("href") or attr_map.get("src")
            if reference:
                self.references.append(reference)
        if tag == "meta" and attr_map.get("name") == "viewport":
            self.has_viewport = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str:
        return "".join(self.title_parts).strip()


def _parse(path: Path) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def test_only_main_manual_is_at_repository_root():
    assert sorted(path.name for path in ROOT.glob("*.html")) == [MAIN_MANUAL.name]


def test_expected_detail_pages_and_assets_exist():
    assert MAIN_MANUAL.is_file()
    assert {path.name for path in DETAIL_DIR.glob("*.html")} == DETAIL_PAGES
    assert (DETAIL_DIR / "assets" / "manual.css").is_file()
    assert (DETAIL_DIR / "assets" / "manual.js").is_file()


def test_every_manual_page_has_metadata_and_valid_local_references():
    pages = [MAIN_MANUAL, *(DETAIL_DIR / name for name in sorted(DETAIL_PAGES))]
    for page in pages:
        parser = _parse(page)
        assert parser.lang == "ko", page
        assert parser.title, page
        assert parser.has_viewport, page
        assert "main-content" in parser.ids, page

        for raw_reference in parser.references:
            assert not raw_reference.startswith(("http://", "https://")), (
                page,
                raw_reference,
            )
            path_part, _, fragment = unquote(raw_reference).partition("#")
            target = (page.parent / path_part).resolve() if path_part else page
            assert target.exists(), (page, raw_reference)
            if fragment and target.suffix.lower() == ".html":
                assert fragment in _parse(target).ids, (page, raw_reference)


def test_manual_mentions_current_high_risk_contracts():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [MAIN_MANUAL, *(DETAIL_DIR / name for name in DETAIL_PAGES)]
    )
    required_phrases = {
        "청구서 생성 없이 일일배송",
        "담당 구역 1순위 컬럼",
        "담당 구역 2순위 컬럼",
        "학교명·시작일·종료일",
        "이름 + 생년월일",
        "assets/일일배송템플릿.xlsx",
        "assets/중식일일배송템플릿.xlsx",
        "구분, 학교명, 학년, 성명, 생년월일, 세대주성명, 아동과의관계, 관할동, 주소, 핸드폰",
        "해당 시트에서 배송 수량이 0인 구역 행은 자동으로 빠지고",
        "중식_yy.mm.dd",
        "석식: 6/13~6/15",
        "17일 중지 19일 배송",
        "17일과 18일만 일시중지",
        "월 합계",
    }
    missing = {phrase for phrase in required_phrases if phrase not in combined}
    assert not missing
