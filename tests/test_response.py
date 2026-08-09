"""response モジュールのレスポンスビルダー回帰テスト。"""

import polars as pl

from admin_procedures.response import (
    build_inspect_response,
    build_list_response,
    build_query_response,
    build_summarize_response,
    sort_groups_by_primary_metric,
)


def _resolved(registry):
    entry = registry.list_datasets()[0]
    ver = entry.ver
    assert ver is not None
    assert ver.dsd is not None
    return entry, ver, ver.dsd


# ============================================================
# inspect_dataset レスポンス
# ============================================================


class TestBuildInspectResponse:
    def test_includes_quality_by_default(self, registry):
        entry, ver, dsd = _resolved(registry)

        result = build_inspect_response(
            entry,
            ver,
            dsd,
            dataset_id=entry.dataset_id,
        )

        assert result["dataset_id"] == entry.dataset_id
        assert "columns" in result
        assert "rows" in result
        assert "quality_summary" in result
        assert "numeric_stats" in result


# ============================================================
# list_datasets レスポンス
# ============================================================


class TestBuildListResponse:
    def test_returns_datasets(self, registry):
        result = build_list_response(registry)

        assert result["total"] >= 1
        assert result["datasets"][0]["dataset_id"] == "procedures-survey-r6"

    def test_includes_record_count(self, registry):
        """ツール docstring と list_datasets UI が参照するため record_count を返すこと。"""
        entry = registry.list_datasets()[0]
        result = build_list_response(registry)

        assert result["datasets"][0]["record_count"] == entry.record_count

    def test_omits_record_count_when_unavailable(self, registry):
        """レコード数を取得できなかったデータセットでは record_count を出さないこと。"""
        registry.list_datasets()[0].record_count = 0
        result = build_list_response(registry)

        assert "record_count" not in result["datasets"][0]

    def test_q_filters_by_keyword(self, registry):
        result = build_list_response(registry, q="r6")
        assert result["total"] >= 1
        assert all("r6" in d["dataset_id"] for d in result["datasets"])

    def test_q_no_match(self, registry):
        result = build_list_response(registry, q="nonexistent-xyz")
        assert result["total"] == 0
        assert result["datasets"] == []

    def test_publisher_filter(self, registry):
        all_result = build_list_response(registry)
        pub = all_result["datasets"][0]["publisher"]
        result = build_list_response(registry, publisher=pub)
        assert result["total"] >= 1
        assert all(pub.lower() in d["publisher"].lower() for d in result["datasets"])


# ============================================================
# select フィールド検証
# ============================================================


class TestUnknownSelectFields:
    def test_ignores_existing_fields(self, registry):
        _, ver, dsd = _resolved(registry)

        missing = [f for f in ["手続ID", "does-not-exist"] if dsd.get_component(f) is None]

        assert missing == ["does-not-exist"]


# ============================================================
# ソート
# ============================================================


class TestSortGroupsByPrimaryMetric:
    def test_skips_null_excluded_columns(self):
        groups = pl.DataFrame({
            "sum:総手続件数:null_excluded": [2, 1],
            "avg:オンライン率": [0.3, 0.8],
            "count": [10, 3],
        })

        sorted_groups = sort_groups_by_primary_metric(groups)

        assert sorted_groups["avg:オンライン率"][0] == 0.8


# ============================================================
# query_records レスポンス
# ============================================================


class TestBuildQueryResponse:
    def test_includes_optional_metadata(self, registry):
        entry, ver, dsd = _resolved(registry)

        result = build_query_response(
            entry,
            ver,
            dsd,
            dataset_id=entry.dataset_id,
            total=1,
            records=pl.DataFrame({"オンライン手続件数": [123]}),
            next_cursor="next",
            hint="refine filters",
            query_params={"limit": 10},
            selected_fields=["オンライン手続件数"],
        )

        assert result["hint"] == "refine filters"
        assert result["query_params"] == {"limit": 10}
        assert "notes" in result
        assert "オンライン手続件数" in result["notes"]
        assert "displayable_fields" not in result
        assert "suppressed_fields" not in result


# ============================================================
# summarize_records レスポンス
# ============================================================


class TestBuildSummarizeResponse:
    def test_includes_explode_metadata(self, registry):
        entry, ver, dsd = _resolved(registry)

        result = build_summarize_response(
            entry,
            ver,
            dsd,
            dataset_id=entry.dataset_id,
            groups=pl.DataFrame({"count": [2], "申請に関連する士業": ["行政書士"]}),
            total_group_count=1,
            query_params={"explode": "申請に関連する士業"},
            metric_fields=["オンライン手続件数"],
            exploded_field="申請に関連する士業",
            pre_explode_records=4,
        )

        assert "_quality_warnings" not in result
        assert result["query_params"] == {"explode": "申請に関連する士業"}
        assert result["exploded_field"] == "申請に関連する士業"
        assert result["pre_explode_records"] == 4
        assert "notes" in result
