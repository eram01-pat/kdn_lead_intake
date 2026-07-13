"""Unit tests for the search-pagination payload helpers in the
bids&tenders collector."""

import json

from src.collectors.bidsandtenders import (
    _build_page_payload,
    _decode_search_payload,
    _locate_paging_field,
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


class TestLocatePagingField:
    def test_top_level_page_number(self):
        container, key, style = _locate_paging_field({"Page": 1, "Size": 25})
        assert (key, style) == ("Page", "number")
        assert container["Page"] == 1

    def test_datatables_offset_style(self):
        container, key, style = _locate_paging_field(
            {"draw": "1", "start": "0", "length": "25"}
        )
        assert (key, style) == ("start", "offset")

    def test_nested_json(self):
        payload = {"request": {"pageIndex": 0, "pageSize": 25}, "filters": []}
        container, key, style = _locate_paging_field(payload)
        assert (key, style) == ("pageIndex", "number")
        assert container is payload["request"]

    def test_underscored_key(self):
        _, key, style = _locate_paging_field({"page_number": 2})
        assert (key, style) == ("page_number", "number")

    def test_no_paging_field(self):
        assert _locate_paging_field({"sort": "asc", "status": "open"}) == (None, None, "")


class TestBuildPagePayload:
    def test_one_based_json_page(self):
        out = _build_page_payload('{"Page": 1, "Size": 25}', page_no=2, page_size=25)
        assert json.loads(out) == {"Page": 2, "Size": 25}

    def test_zero_based_json_page(self):
        out = _build_page_payload('{"pageIndex": 0, "pageSize": 25}', page_no=2, page_size=25)
        assert json.loads(out) == {"pageIndex": 1, "pageSize": 25}

    def test_offset_style_form(self):
        out = _build_page_payload("draw=1&start=0&length=25", page_no=3, page_size=25)
        assert "start=50" in out
        assert "length=25" in out

    def test_form_string_type_preserved(self):
        out = _build_page_payload("page=1&size=25", page_no=2, page_size=25)
        assert "page=2" in out

    def test_no_paging_field_returns_none(self):
        assert _build_page_payload('{"sort": "asc"}', page_no=2, page_size=25) is None

    def test_undecodable_returns_none(self):
        assert _build_page_payload("garbage payload", page_no=2, page_size=25) is None
