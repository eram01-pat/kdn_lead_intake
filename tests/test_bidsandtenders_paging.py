"""Unit tests for the search-pagination payload helpers in the
bids&tenders collector."""

import json
from urllib.parse import parse_qs, urlsplit

from src.collectors.bidsandtenders import (
    _apply_paging,
    _build_page_request,
    _decode_search_payload,
)


class TestDecodeSearchPayload:
    def test_json_payload(self):
        payload, kind = _decode_search_payload('{"Page": 1, "Size": 25}')
        assert kind == "json"
        assert payload == {"Page": 1, "Size": 25}

    def test_form_payload(self):
        payload, kind = _decode_search_payload("page=1&size=25&sort=closing")
        assert kind == "form"
        assert payload == {"page": "1", "size": "25", "sort": "closing"}

    def test_empty_payload(self):
        assert _decode_search_payload("") == (None, "")
        assert _decode_search_payload(None) == (None, "")

    def test_undecodable_payload(self):
        assert _decode_search_payload("not json and not a form") == (None, "")


class TestApplyPaging:
    def test_one_based_page_number(self):
        payload = {"Page": 1, "Size": 25}
        assert _apply_paging(payload, page_no=2, page_size=25)
        assert payload == {"Page": 2, "Size": 25}

    def test_zero_based_page_number(self):
        payload = {"pageIndex": 0, "pageSize": 25}
        assert _apply_paging(payload, page_no=2, page_size=25)
        assert payload == {"pageIndex": 1, "pageSize": 25}

    def test_offset_style(self):
        payload = {"draw": "1", "start": "0", "length": "25"}
        assert _apply_paging(payload, page_no=3, page_size=25)
        assert payload["start"] == "50"

    def test_kendo_sets_all_paging_fields_consistently(self):
        # Kendo requests carry page AND skip — both must advance together.
        payload = {"page": "1", "pageSize": "25", "skip": "0", "take": "25"}
        assert _apply_paging(payload, page_no=2, page_size=25)
        assert payload["page"] == "2"
        assert payload["skip"] == "25"
        assert payload["take"] == "25"  # page size untouched

    def test_nested_json(self):
        payload = {"request": {"pageIndex": 0, "pageSize": 25}, "filters": []}
        assert _apply_paging(payload, page_no=2, page_size=25)
        assert payload["request"]["pageIndex"] == 1

    def test_string_type_preserved(self):
        payload = {"page": "1"}
        _apply_paging(payload, page_no=2, page_size=25)
        assert payload["page"] == "2"

    def test_no_paging_field(self):
        payload = {"sort": "asc", "status": "open"}
        assert not _apply_paging(payload, page_no=2, page_size=25)
        assert payload == {"sort": "asc", "status": "open"}


class TestBuildPageRequest:
    def test_paging_in_url_query(self):
        url = "https://york.bidsandtenders.ca/Module/Tenders/en/Tender/Search/abc?page=1&pageSize=25&skip=0&take=25"
        body = "__RequestVerificationToken=tok123"
        result = _build_page_request(url, body, page_no=2, page_size=25)
        assert result is not None
        new_url, new_body = result
        params = {k: v[0] for k, v in parse_qs(urlsplit(new_url).query).items()}
        assert params["page"] == "2"
        assert params["skip"] == "25"
        assert params["take"] == "25"
        assert new_body == body  # body passes through untouched

    def test_paging_in_json_body(self):
        url = "https://x.example/Tender/Search/abc"
        body = '{"Page": 1, "Size": 25}'
        result = _build_page_request(url, body, page_no=3, page_size=25)
        assert result is not None
        new_url, new_body = result
        assert new_url == url
        assert json.loads(new_body) == {"Page": 3, "Size": 25}

    def test_paging_in_form_body(self):
        url = "https://x.example/Tender/Search/abc"
        result = _build_page_request(url, "page=1&size=25", page_no=2, page_size=25)
        assert result is not None
        assert "page=2" in result[1]

    def test_token_only_body_and_bare_url_returns_none(self):
        # York's actual shape: anti-forgery token in the body, nothing pageable.
        url = "https://york.bidsandtenders.ca/Module/Tenders/en/Tender/Search/abc"
        body = "__RequestVerificationToken=tok123"
        assert _build_page_request(url, body, page_no=2, page_size=25) is None

    def test_undecodable_body_returns_none(self):
        url = "https://x.example/Tender/Search/abc"
        assert _build_page_request(url, "garbage payload", page_no=2, page_size=25) is None
