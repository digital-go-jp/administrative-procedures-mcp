"""入力制限とリソース枯渇対策の検証。

クエリ上限、where 配列サイズ、文字列長、Content-Length など
段階1の制限が正しく機能することを確認する。
"""

import pytest

from admin_procedures.query import (
    parse_where,
    MAX_QUERY_LIMIT,
    MAX_AGGREGATE_LIMIT,
    MAX_WHERE_ARRAY_SIZE,
    MAX_WHERE_STRING_LENGTH,
)


@pytest.fixture
def dsd(registry):
    """procedures-survey-r6 の DataStructureDefinition。"""
    from admin_procedures.response import resolve_dataset
    _, _, dsd = resolve_dataset(registry, "procedures-survey-r6")
    return dsd


class TestQueryLimits:
    """クエリ上限と集計結果上限の検証。"""

    def test_query_limit_values_reduced(self):
        """制限値が段階1で引き下げられたことを確認。"""
        assert MAX_QUERY_LIMIT == 5_000, "クエリ上限は5,000に引き下げる"
        assert MAX_AGGREGATE_LIMIT == 10_000, "集計結果上限は10,000に引き下げる"

    def test_where_array_size_limit(self, dsd):
        """where 配列サイズが200要素に制限されること。"""
        # OK: 200要素
        where_ok = {"所管府省庁": ["test"] * 200}
        predicates = parse_where(where_ok, dsd)
        assert len(predicates) == 1

        # NG: 201要素
        where_ng = {"所管府省庁": ["test"] * 201}
        with pytest.raises(ValueError, match="200 要素以内"):
            parse_where(where_ng, dsd)

    def test_where_string_length_limit(self, dsd):
        """where 文字列値が10,000文字に制限されること。"""
        # OK: 10,000文字
        where_ok = {"所管府省庁": "x" * MAX_WHERE_STRING_LENGTH}
        predicates = parse_where(where_ok, dsd)
        assert len(predicates) == 1

        # NG: 10,001文字
        where_ng = {"所管府省庁": "x" * (MAX_WHERE_STRING_LENGTH + 1)}
        with pytest.raises(ValueError, match="10000 文字以内"):
            parse_where(where_ng, dsd)

    def test_ne_array_size_limit(self, dsd):
        """$ne 演算子の配列サイズが200要素に制限されること。"""
        # OK: 200要素
        where_ok = {"所管府省庁": {"$ne": ["test"] * 200}}
        predicates = parse_where(where_ok, dsd)
        assert len(predicates) == 1

        # NG: 201要素
        where_ng = {"所管府省庁": {"$ne": ["test"] * 201}}
        with pytest.raises(ValueError, match="200 要素以内"):
            parse_where(where_ng, dsd)

    def test_contains_empty_array_rejected(self, dsd):
        """$contains で空配列が拒否されること。"""
        where_ng = {"所管府省庁": {"$contains": []}}
        with pytest.raises(ValueError, match="1 要素以上必要"):
            parse_where(where_ng, dsd)

    def test_not_empty_no_value_allowed(self, dsd):
        """$not_empty は値を取らないこと。"""
        # OK: 値なし（None）
        where_ok = {"所管府省庁": {"$not_empty": None}}
        predicates = parse_where(where_ok, dsd)
        assert len(predicates) == 1

        # NG: 値あり
        where_ng = {"所管府省庁": {"$not_empty": True}}
        with pytest.raises(ValueError, match="値を取りません"):
            parse_where(where_ng, dsd)

    def test_contains_string_length_limit(self, dsd):
        """$contains の文字列値が10,000文字に制限されること。"""
        # OK: 10,000文字
        where_ok = {"所管府省庁": {"$contains": "x" * MAX_WHERE_STRING_LENGTH}}
        predicates = parse_where(where_ok, dsd)
        assert len(predicates) == 1

        # NG: 10,001文字
        where_ng = {"所管府省庁": {"$contains": "x" * (MAX_WHERE_STRING_LENGTH + 1)}}
        with pytest.raises(ValueError, match="10000 文字以内"):
            parse_where(where_ng, dsd)
