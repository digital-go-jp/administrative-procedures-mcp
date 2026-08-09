"""MCP ツール統合テスト。"""

import inspect
import json

import pytest

from fastmcp.tools import ToolResult  # 3.x / 4.x 共通の取得元

from admin_procedures.server import (
    _build_instructions,
    register_all_tools,
    register_data_tools,
)


def _parse(result: str | ToolResult) -> dict:
    """ToolResult または JSON 文字列をパースして dict を返す。

    ToolResult の場合は structured_content（UI 側の正規データ）を返す。
    プレーン文字列の場合は notes instruction prefix を除去して JSON 部分をパースする。
    """
    if isinstance(result, ToolResult):
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[-1].text)
    text = result
    if not text.startswith("{"):
        idx = text.find("\n\n{")
        if idx >= 0:
            text = text[idx + 2:]
    return json.loads(text)


def _parse_text(result: str | ToolResult) -> dict:
    """LLM テキスト出力の JSON 部分をパースする (structured_content ではなくテキスト側)。

    ToolResult の場合は最後の TextContent ブロック（JSON データ）をパースする。
    先頭に notes instruction がある場合、それは別ブロックなので最後を取得。
    """
    if isinstance(result, ToolResult):
        return json.loads(result.content[-1].text)
    text = result
    if not text.startswith("{"):
        idx = text.find("\n\n{")
        if idx >= 0:
            text = text[idx + 2:]
    return json.loads(text)



def _rows(d: dict) -> list[dict]:
    """columnar format (columns + rows) を list[dict] に復元する。

    None 値はスキップし、元の translate_record_coded の出力と同じ形式にする。
    """
    cols = d.get("columns", [])
    return [
        {c: row[i] for i, c in enumerate(cols) if row[i] is not None}
        for row in d.get("rows", [])
    ]


class _FakeMCP:
    """テスト用のFake MCPサーバー。"""

    def __init__(self):
        self._funcs = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self._funcs[fn.__name__] = fn
            return fn
        return decorator

    @property
    def funcs(self):
        return self._funcs


def _register(registry, register_fn, **kwargs):
    """指定の登録関数で FakeMCP にツールを登録し、関数マップを返す。"""
    mcp = _FakeMCP()
    register_fn(mcp, registry, **kwargs)
    return mcp.funcs


# フィクスチャ — Tools
@pytest.fixture()
def data_tools(registry):
    """データアクセスツールを登録済みの関数マップ。"""
    return _register(registry, register_data_tools)


@pytest.fixture()
def all_tools(registry):
    """全ツールを登録済みの関数マップ。"""
    return _register(registry, register_all_tools)


@pytest.fixture()
def data_tools_no_ui(registry):
    """UI 無効モードのデータアクセスツール関数マップ。"""
    return _register(registry, register_data_tools, enable_ui=False)


@pytest.fixture()
def all_tools_no_ui(registry):
    """全ツールを UI 無効モードで登録済みの関数マップ。"""
    return _register(registry, register_all_tools, enable_ui=False)


# ============================================================
# ディスカバリ: inspect_dataset
# ============================================================


class TestDescribeDataset:
    def test_structure_and_quality(self, all_tools):
        """columnar 構造・codelist・quality_summary・numeric_stats を包括的に検証。"""
        result = _parse(all_tools["inspect_dataset"](
            "procedures-survey-r6",
        ))
        assert result["dataset_id"] == "procedures-survey-r6"
        assert result["title"] == "行政手続等の棚卸調査結果"
        assert result["record_count"] == 4
        assert "columns" in result
        assert "rows" in result
        cols = result["columns"]
        rows = result["rows"]
        assert "id" in cols
        assert "role" in cols
        assert "fill_rate" in cols
        assert len(rows) == 38  # 全コンポーネント
        # codelist がインライン展開されていること
        cl_idx = cols.index("codelist")
        codelist_rows = [r for r in rows if len(r) > cl_idx and r[cl_idx] is not None]
        assert len(codelist_rows) > 0
        for r in codelist_rows:
            assert isinstance(r[cl_idx], list)
        # fill_rate が全行にあること
        fr_idx = cols.index("fill_rate")
        for r in rows:
            if len(r) > fr_idx:
                assert 0.0 <= (r[fr_idx] or 0) <= 1.0
        # quality_summary
        assert "quality_summary" in result
        qs = result["quality_summary"]
        for key in ("fully_populated", "mostly_populated", "sparse"):
            assert key in qs
        # numeric_stats
        assert "numeric_stats" in result
        assert "総手続件数" in result["numeric_stats"]
        ns = result["numeric_stats"]["総手続件数"]
        assert ns["min"] == 50
        assert ns["max"] == 500000

    def test_error(self, all_tools):
        """存在しないデータセットでエラーを検証。"""
        assert "error" in _parse(all_tools["inspect_dataset"]("nonexistent"))


# ============================================================
# データアクセス: query_records
# ============================================================


class TestQueryData:
    def test_no_filter(self, data_tools):
        result = _parse(data_tools["query_records"]("procedures-survey-r6"))
        assert result["dataset_id"] == "procedures-survey-r6"
        assert result["total"] == 4
        assert len(_rows(result)) == 4
        assert len(result["rows"]) == 4
        assert "columns" in result

    def test_comprehensive_filters(self, data_tools):
        """$in, code, partial match, range, combined, $ne, $not_empty, multi_value IN を検証。"""
        # $in (label)
        r = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"所管府省庁": ["厚生労働省"]},
        ))
        assert r["total"] == 2
        for rec in _rows(r):
            assert rec["所管府省庁"] == "厚生労働省"
        # 値フィルタ
        r2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"オンライン化の実施状況": ["1 実施済"]},
        ))
        assert r2["total"] == 2
        for rec in _rows(r2):
            assert rec["オンライン化の実施状況"] == "1 実施済"
        # 部分一致（$contains）
        r3 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"手続名": "年金"},
        ))
        assert r3["total"] == 1
        assert _rows(r3)[0]["手続名"] == "国民年金の届出"
        # 範囲（$gte/$lte）
        r4 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"総手続件数": {"$gte": 100, "$lte": 1000}},
        ))
        assert r4["total"] == 2
        # 複合条件
        r5 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"所管府省庁": ["厚生労働省"], "オンライン化の実施状況": ["1 実施済"]},
        ))
        assert r5["total"] == 1
        assert _rows(r5)[0]["手続ID"] == "1"
        # $ne
        r6 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"オンライン化の実施状況": {"$ne": "1 実施済"}},
        ))
        for rec in _rows(r6):
            assert rec["オンライン化の実施状況"] != "1 実施済"
        assert r6["total"] == 2
        # $not_empty
        r7 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"手数料等の納付方法": {"$not_empty": None}},
        ))
        assert r7["total"] == 1
        # multi_value IN
        r8 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"申請に関連する士業": ["行政書士"]},
        ))
        assert r8["total"] == 1
        assert _rows(r8)[0]["手続ID"] == "1"
        r9 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"申請に関連する士業": ["社会保険労務士"]},
        ))
        assert r9["total"] == 2
        r10 = _parse(data_tools["query_records"](
            "procedures-survey-r6",
            where={"申請に関連する士業": {"$in": ["行政書士"]}},
        ))
        assert r10["total"] == 1
        # $not_contains
        r11 = _parse(data_tools["query_records"](
            "procedures-survey-r6",
            where={"情報システム(申請)": {"$not_contains": "電子"}},
        ))
        assert r11["total"] == 3
        assert all("電子" not in (rec.get("情報システム(申請)") or "") for rec in _rows(r11))

    def test_select_and_ordering(self, data_tools):
        """select, order_by asc/desc を検証。"""
        r = _parse(data_tools["query_records"](
            "procedures-survey-r6", select=["手続ID", "手続名"],
        ))
        for rec in _rows(r):
            assert "手続ID" in rec
            assert "手続名" in rec
            assert "所管府省庁" not in rec
        r2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", order_by="総手続件数",
        ))
        volumes = [r["総手続件数"] for r in _rows(r2)]
        assert volumes == sorted(volumes)
        r3 = _parse(data_tools["query_records"](
            "procedures-survey-r6", order_by="-総手続件数",
        ))
        volumes_desc = [r["総手続件数"] for r in _rows(r3)]
        assert volumes_desc == sorted(volumes_desc, reverse=True)

    def test_pagination(self, data_tools):
        result = _parse(data_tools["query_records"]("procedures-survey-r6", limit=2))
        assert len(_rows(result)) == 2
        assert result["total"] == 4
        assert result["next_cursor"] is not None
        result2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", limit=2, cursor=result["next_cursor"],
        ))
        assert len(_rows(result2)) == 2
        assert result2["next_cursor"] is None
        ids1 = {r["手続ID"] for r in _rows(result)}
        ids2 = {r["手続ID"] for r in _rows(result2)}
        assert ids1.isdisjoint(ids2)

    def test_q_search(self, data_tools):
        """全文検索: 基本、法令名、大文字小文字、AND結合、ヒットなし、空白、search_fields を検証。"""
        # 基本
        r = _parse(data_tools["query_records"]("procedures-survey-r6", q="年金"))
        assert r["total"] == 1
        assert _rows(r)[0]["手続名"] == "国民年金の届出"
        # 法令名
        r2 = _parse(data_tools["query_records"]("procedures-survey-r6", q="雇用保険"))
        assert r2["total"] == 1
        assert _rows(r2)[0]["手続ID"] == "2"
        # 大文字小文字無視
        r3 = _parse(data_tools["query_records"]("procedures-survey-r6", q="E-GOV"))
        assert r3["total"] == 1
        # q + where AND 結合
        r4 = _parse(data_tools["query_records"](
            "procedures-survey-r6", q="届出", where={"所管府省庁": ["総務省"]},
        ))
        assert r4["total"] == 1
        assert _rows(r4)[0]["手続ID"] == "3"
        # ヒットなし
        assert _parse(data_tools["query_records"]("procedures-survey-r6", q="存在しないワード"))["total"] == 0
        # 空白無視
        for blank in ("", "   "):
            assert _parse(data_tools["query_records"]("procedures-survey-r6", q=blank))["total"] == 4
        # search_fields
        assert _parse(data_tools["query_records"](
            "procedures-survey-r6", q="2 未実施", search_fields=["オンライン化の実施状況"],
        ))["total"] == 1
        assert _parse(data_tools["query_records"](
            "procedures-survey-r6", q="実施済", search_fields=["オンライン化の実施状況"],
        ))["total"] == 2
        assert _parse(data_tools["query_records"](
            "procedures-survey-r6", q="年金", search_fields=["手続名"],
        ))["total"] == 1

    def test_json_string_params_and_errors(self, data_tools):
        """JSON 文字列パラメータ・各種エラーを検証。"""
        result = _parse(data_tools["query_records"](
            "procedures-survey-r6",
            where='{"所管府省庁": ["厚生労働省"]}',
            select='["手続ID", "手続名"]',
        ))
        assert result["total"] == 2
        assert "所管府省庁" not in _rows(result)[0]
        assert "error" in _parse(data_tools["query_records"](
            "procedures-survey-r6", where="not valid json",
        ))
        assert "error" in _parse(data_tools["query_records"]("nonexistent"))
        assert "error" in _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"bad_field": "x"},
        ))
        r = _parse(data_tools["query_records"](
            "procedures-survey-r6", select=["手続ID", "bad_field"],
        ))
        assert "error" in r
        assert "select field" in r["error"]
        r2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"owning_ministry": ["厚生労働省"]},
        ))
        assert "error" in r2
        assert "field_id_hint" in r2

    def test_search_and_order_unknown_field_errors(self, data_tools):
        """search_fields / order_by の未知フィールドを個別に案内できることを検証。"""
        search_error = _parse(data_tools["query_records"](
            "procedures-survey-r6", q="年金", search_fields=["bad_field"],
        ))
        assert "error" in search_error
        assert "search field" in search_error["error"]

        order_error = _parse(data_tools["query_records"](
            "procedures-survey-r6", order_by="bad_field",
        ))
        assert "error" in order_error
        assert "order_by field" in order_error["error"]


# ============================================================
# データアクセス: summarize_records
# ============================================================


class TestSummarize:
    def test_basic_and_grouping(self, data_tools):
        """グローバル集計・group_by・sum・複数メトリクス・コード除去を検証。"""
        r = _parse(data_tools["summarize_records"]("procedures-survey-r6"))
        assert len(_rows(r)) == 1
        assert _rows(r)[0]["count"] == 4
        r2 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", group_by=["所管府省庁"],
        ))
        assert len(_rows(r2)) == 2
        by_ministry = {g["所管府省庁"]: g for g in _rows(r2)}
        assert by_ministry["厚生労働省"]["count"] == 2
        assert by_ministry["総務省"]["count"] == 2
        r3 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["sum:総手続件数"], group_by=["所管府省庁"],
        ))
        by_m = {g["所管府省庁"]: g for g in _rows(r3)}
        assert by_m["厚生労働省"]["sum:総手続件数"] == 501000
        assert by_m["総務省"]["sum:総手続件数"] == 250
        r4 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count", "sum:総手続件数", "avg:総手続件数"],
        ))
        g = _rows(r4)[0]
        assert g["count"] == 4
        assert "sum:総手続件数" in g
        assert "avg:総手続件数" in g
        r5 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", group_by=["オンライン化の実施状況"],
        ))
        group_values = {g["オンライン化の実施状況"] for g in _rows(r5)}
        assert "1 実施済" in group_values

    def test_having_and_computed_measures(self, data_tools):
        """having 句 + 算出数値項目 avg:online_rate を検証。"""
        # count >= 2
        r = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", group_by=["オンライン化の実施状況"],
            having={"count": {"$gte": 2}},
        ))
        assert len(_rows(r)) == 1
        assert _rows(r)[0]["count"] >= 2
        # sum_total_volume >= 1000
        r2 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count", "sum:総手続件数"],
            group_by=["所管府省庁"], having={"sum:総手続件数": {"$gte": 1000}},
        ))
        assert len(_rows(r2)) == 1
        assert _rows(r2)[0]["所管府省庁"] == "厚生労働省"
        # 全グループ除外
        r3 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", group_by=["所管府省庁"],
            having={"count": {"$gte": 100}},
        ))
        assert len(_rows(r3)) == 0
        # 算出数値項目
        r4 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count", "avg:オンライン率"],
            group_by=["所管府省庁"],
        ))
        by_m = {g["所管府省庁"]: g for g in _rows(r4)}
        # count_where: 厚生労働省 = 1実施済/2件, 総務省 = 1実施済/2件
        assert by_m["厚生労働省"]["avg:オンライン率"] == 0.5
        assert by_m["総務省"]["avg:オンライン率"] == 0.5
        r5 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["avg:オンライン率"],
        ))
        # 全体: 2実施済/4件
        assert _rows(r5)[0]["avg:オンライン率"] == 0.5
        # having + 算出数値項目
        r6 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count", "avg:オンライン率"],
            group_by=["所管府省庁"], having={"avg:オンライン率": {"$gte": 0.5}},
        ))
        assert len(_rows(r6)) == 2

    def test_explode(self, data_tools):
        """explode: 基本、auto group_by、non-groupable OK、groupable attribute を検証。"""
        r = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"], explode="手続が行われるイベント(法人)",
        ))
        assert r["exploded_field"] == "手続が行われるイベント(法人)"
        assert r["pre_explode_records"] == 4
        
        r2 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"],
            group_by=["所管府省庁"], explode="手続が行われるイベント(法人)",
        ))
        assert len(_rows(r2)) >= 1
        for g in _rows(r2):
            assert "手続が行われるイベント(法人)" in g
            assert "所管府省庁" in g
        r3 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"], explode="手続が行われるイベント(個人)",
        ))
        assert "error" not in r3
        r3b = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"],
            explode='["手続が行われるイベント(法人)"]',
        ))
        assert r3b["exploded_field"] == "手続が行われるイベント(法人)"
        r4 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count", "sum:総手続件数"],
            group_by=["手数料等の納付有無"],
        ))
        assert "error" not in r4
        assert len(_rows(r4)) >= 1

    def test_explode_with_where_on_exploded_field(self, data_tools):
        """explode + where で explode 対象フィールドを絞り込めることを検証。"""
        # 「申請に関連する士業」は multi_value。テストデータに "社会保険労務士;行政書士" がある。
        # where で "社会保険労務士" に絞り込むと、explode 後に "行政書士" は含まれないはず。
        r = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"],
            explode="申請に関連する士業",
            where={"申請に関連する士業": "社会保険労務士"},
        ))
        assert "error" not in r
        groups = _rows(r)
        # 全グループの「申請に関連する士業」値を確認
        values = [g["申請に関連する士業"] for g in groups]
        assert "社会保険労務士" in values
        # explode 後フィルタにより "行政書士" 単独のグループは残らない
        assert "行政書士" not in values

    def test_group_by_multi_value_auto_enables_explode(self, data_tools):
        """multi_value フィールドを group_by に入れたとき自動で explode することを検証。"""
        result = _parse(data_tools["summarize_records"](
            "procedures-survey-r6",
            metrics=["count"],
            group_by=["申請に関連する士業"],
        ))
        assert result["exploded_field"] == "申請に関連する士業"
        assert result["pre_explode_records"] == 4
        assert len(_rows(result)) >= 1

    def test_json_filter_and_errors(self, data_tools):
        """JSON 文字列パラメータ・where フィルタ・各種エラーを検証。"""
        r = _parse(data_tools["summarize_records"](
            "procedures-survey-r6",
            metrics='["count", "sum:総手続件数"]',
            group_by='["所管府省庁"]',
            where='{"所管府省庁": ["厚生労働省"]}',
            having='{"count": {"$gte": 1}}',
        ))
        
        assert len(_rows(r)) >= 1
        # エラー系: measure は groupable=False なのでエラー
        assert "error" in _parse(data_tools["summarize_records"](
            "procedures-survey-r6", group_by=["総手続件数"],
        ))
        assert "error" in _parse(data_tools["summarize_records"](
            "procedures-survey-r6", group_by=["nonexistent"],
        ))
        assert "error" in _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["bogus"],
        ))
        assert "error" in _parse(data_tools["summarize_records"]("nonexistent"))
        r2 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", having={"nonexistent_col": {"$gte": 1}},
        ))
        assert "error" in r2
        assert "valid_having_columns" in r2
        r2b = _parse(data_tools["summarize_records"](
            "procedures-survey-r6",
            metrics=["count", "sum:総手続件数"],
            having={"sum:total_volume": {"$gte": 1}},
        ))
        assert "error" in r2b
        assert "valid_having_columns" in r2b
        assert "field_id_hint" in r2b
        assert "error" in _parse(data_tools["summarize_records"](
            "procedures-survey-r6", explode="nonexistent_field",
        ))
        r3 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["sum:total_volume"],
        ))
        assert "error" in r3
        assert "field_id_hint" in r3


# ============================================================
# list_datasets
# ============================================================


class TestListDatasets:
    def test_returns_datasets(self, all_tools):
        """データセット一覧を返すことを検証。"""
        result = _parse(all_tools["list_datasets"]())
        assert "datasets" in result
        assert result["total"] > 0
        ds = result["datasets"][0]
        assert "dataset_id" in ds
        assert "title" in ds

    def test_ui_mode_returns_tool_result(self, all_tools):
        """UI 有効時に ToolResult を返し、meta に UI URI が含まれること。"""
        result = all_tools["list_datasets"]()
        assert isinstance(result, ToolResult)
        assert result.structured_content is not None
        assert "datasets" in result.structured_content
        assert result.meta == {"ui": {"resourceUri": "ui://administrative-procedures-mcp/list_datasets"}}

    def test_no_ui_returns_plain_string(self, all_tools_no_ui):
        """UI 無効時にプレーン JSON テキストを返すこと。"""
        result = all_tools_no_ui["list_datasets"]()
        assert isinstance(result, str)
        data = _parse(result)
        assert "datasets" in data

    def test_q_filters_datasets(self, all_tools):
        """q パラメータでデータセットを絞り込めること。"""
        result = _parse(all_tools["list_datasets"](q="r6"))
        assert result["total"] >= 1
        assert all("r6" in d["dataset_id"] for d in result["datasets"])

    def test_q_no_match_returns_empty(self, all_tools):
        """q パラメータで一致なしの場合は空リストを返すこと。"""
        result = _parse(all_tools["list_datasets"](q="nonexistent-xyz"))
        assert result["total"] == 0
        assert result["datasets"] == []

    def test_publisher_filters_datasets(self, all_tools):
        """publisher パラメータでデータセットを絞り込めること。"""
        all_result = _parse(all_tools["list_datasets"]())
        pub = all_result["datasets"][0]["publisher"]
        result = _parse(all_tools["list_datasets"](publisher=pub))
        assert result["total"] >= 1


# ============================================================
# ツール登録 + エンドツーエンド
# ============================================================


class TestToolRegistration:
    EXPECTED_TOOLS = [
        "inspect_dataset", "list_datasets",
        "query_records", "summarize_records",
    ]

    def test_all_tools_registered(self, all_tools):
        for name in self.EXPECTED_TOOLS:
            assert name in all_tools, f"Tool '{name}' not registered"

    def test_summarize_records_accepts_string_params(self, all_tools):
        """ローカル LLM が文字列で渡すケースに対応するため str も受け付ける。"""
        sig = inspect.signature(all_tools["summarize_records"])
        for param in ("metrics", "group_by"):
            ann = str(sig.parameters[param].annotation)
            assert "str" in ann, f"{param} should accept str"
            assert "list[str]" in ann, f"{param} should accept list[str]"
        for param in ("where", "having"):
            ann = str(sig.parameters[param].annotation)
            assert "str" in ann, f"{param} should accept str"
            assert "dict" in ann, f"{param} should accept dict"

    def test_server_instructions_bundle_cross_tab_axes_in_one_call(self, registry):
        """Host に複数軸クロス集計を 1 call で行うよう明示する。"""
        instructions = _build_instructions()
        assert "クロス集計は 1 回で実行" in instructions
        assert 'group_by=["軸1", "軸2", ...]' in instructions


class TestEndToEndWorkflow:
    def test_discovery_to_data(self, all_tools):
        """Discovery → Structure → Data → Summarize の典型フロー。"""
        structure = _parse(all_tools["inspect_dataset"](
            "procedures-survey-r6",
        ))
        assert structure["dataset_id"] == "procedures-survey-r6"
        assert len(structure["rows"]) > 0
        data = _parse(all_tools["query_records"](
            "procedures-survey-r6", limit=2,
        ))
        assert data["total"] == 4
        assert len(_rows(data)) == 2
        r = _parse(all_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count", "sum:総手続件数"],
            group_by=["所管府省庁"], where={"オンライン化の実施状況": ["1 実施済"]},
        ))
        assert len(_rows(r)) >= 1


# ============================================================
# メタデータ・品質: provenance, field_quality, missing_fields, displayable
# ============================================================


class TestMetadataAndQuality:
    def test_provenance(self, data_tools):
        """全ツール出力に _provenance が含まれること。"""
        for result in [
            data_tools["query_records"]("procedures-survey-r6"),
            data_tools["summarize_records"]("procedures-survey-r6", metrics=["count"]),
        ]:
            assert "provenance" in result.structured_content
        result = data_tools["query_records"]("procedures-survey-r6")
        prov = result.structured_content["provenance"]
        required_keys = {
            "dataset_title", "as_of_date",
            "published_at", "source_url", "publisher",
        }
        assert required_keys.issubset(prov.keys())
        assert "modification" not in prov
        assert "disclaimer" not in prov

    def test_field_metadata(self, data_tools):
        """field_metadata が TextContent と structured_content の両方に含まれること。"""
        result = data_tools["query_records"]("procedures-survey-r6")
        text_data = _parse_text(result)
        assert "field_metadata" in text_data
        assert "columns" in text_data
        assert "rows" in text_data
        fm = text_data["field_metadata"]
        assert isinstance(fm, dict)
        assert len(fm) > 0
        for v in fm.values():
            assert "role" in v
        # structured_content は text と同一
        assert result.structured_content["field_metadata"] == fm

    def test_missing_fields_not_included(self, data_tools):
        """__missing_fields はデフォルトで付与しない。"""
        result = _parse(data_tools["query_records"]("procedures-survey-r6"))
        for rec in _rows(result):
            assert "__missing_fields" not in rec

    def test_suppressed_fields_filtered(self, data_tools):
        """全件 null フィールドはレコードから除外されること。"""
        result = _parse(data_tools["query_records"]("procedures-survey-r6"))
        assert "手続ID" in result["columns"]
        assert "手続名" in result["columns"]
        # displayable_fields / suppressed_fields キーは存在しない
        assert "displayable_fields" not in result
        assert "suppressed_fields" not in result
        # 全件 null のフィールドは columns に含まれない
        r2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"手続類型": ["3 縦覧等"]},
        ))
        assert "処理期間(非オンライン)" not in r2["columns"]



# ============================================================
# グループ化可能な複数値フィールド
# ============================================================


class TestGroupableMultiValueFields:
    def test_comprehensive(self, all_tools, data_tools):
        """structure での属性・コードリスト・group_by/explode の動作を包括検証。"""
        FIELDS = ("申請に関連する士業", "オンライン化の実施予定及び検討時の懸念点", "申請を提出する機関")
        result = _parse(all_tools["inspect_dataset"]("procedures-survey-r6"))
        cols = result["columns"]
        id_idx = cols.index("id")
        groupable_idx = cols.index("groupable")
        multi_value_idx = cols.index("multi_value")
        codelist_idx = cols.index("codelist")
        for fid in FIELDS:
            row = next(r for r in result["rows"] if r[id_idx] == fid)
            assert row[groupable_idx] == 1
            assert row[multi_value_idx] == 1
        # static codelist は返る、auto/auto_split は省略される
        static_row = next(r for r in result["rows"] if r[id_idx] == "申請に関連する士業")
        assert isinstance(static_row[codelist_idx], list)
        auto_row = next(r for r in result["rows"] if r[id_idx] == "申請を提出する機関")
        auto_cl = auto_row[codelist_idx] if len(auto_row) > codelist_idx else None
        assert auto_cl is None
        for fid in ("オンライン化の実施予定及び検討時の懸念点", "申請を提出する機関"):
            r_sum = _parse(data_tools["summarize_records"](
                "procedures-survey-r6", metrics=["count"], group_by=[fid],
            ))
            assert "error" not in r_sum
        r2 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"], explode="申請に関連する士業",
        ))
        assert r2["exploded_field"] == "申請に関連する士業"


# ============================================================
# resolved_fields — フィールド名自動補正の開示
# ============================================================


class TestResolvedFields:
    """完全一致以外でフィールド名が解決された場合の開示を検証。"""

    def test_fuzzy_group_by_disclosed(self, data_tools):
        """difflib 近似一致（漢字混同）は resolved_fields で開示されること。"""
        result = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"],
            group_by=["オンライン化の実施予定及び検討時の憸念点"],
        ))
        assert result["resolved_fields"] == {
            "オンライン化の実施予定及び検討時の憸念点":
                "オンライン化の実施予定及び検討時の懸念点",
        }
        # 補正後の正式名で集計が実行されている
        assert "オンライン化の実施予定及び検討時の懸念点" in result["columns"]

    def test_nfkc_where_key_disclosed(self, data_tools):
        """NFKC 正規化一致（全角英字等）も resolved_fields で開示されること。"""
        result = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"手続ＩＤ": {"$not_empty": None}},
        ))
        assert result["resolved_fields"] == {"手続ＩＤ": "手続ID"}

    def test_exact_match_no_disclosure(self, data_tools):
        """完全一致で解決した場合は resolved_fields を含まないこと。"""
        result = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"], group_by=["所管府省庁"],
        ))
        assert "resolved_fields" not in result
        r2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", where={"手続類型": ["3 縦覧等"]},
        ))
        assert "resolved_fields" not in r2


# ============================================================
# notes — フィールド注意事項
# ============================================================


class TestFieldNotes:
    """フィールドレベル notes の検証。"""

    def test_summarizenotes(self, data_tools):
        """summarize: 数値項目集計時の notes 存在・count のみ時の非存在を検証。"""
        r = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["sum:オンライン手続件数"],
            group_by=["所管府省庁"],
        ))
        assert "notes" in r
        assert "オンライン手続件数" in r["notes"]
        assert any("不明" in c for c in r["notes"]["オンライン手続件数"])
        # count のみ → notes なし
        r2 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"], group_by=["所管府省庁"],
        ))
        assert "notes" not in r2
        # count_where 算出数値項目 → condition_field に notes なし
        r3 = _parse(data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["avg:オンライン率"],
            group_by=["所管府省庁"],
        ))
        assert "notes" not in r3

    def test_querynotes(self, data_tools):
        """query_records: notes 存在・select で数値項目なし時の非存在を検証。"""
        r = _parse(data_tools["query_records"]("procedures-survey-r6"))
        assert "notes" in r
        assert "オンライン手続件数" in r["notes"]
        r2 = _parse(data_tools["query_records"](
            "procedures-survey-r6", select=["手続ID", "手続名", "所管府省庁"],
        ))
        assert "notes" not in r2

    def testnotes_in_json_not_separate_block(self, data_tools):
        """notes は JSON 内のキーとして出力され、別 TextBlock にならないこと。"""
        result = data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["sum:オンライン手続件数"],
            group_by=["所管府省庁"],
        )
        # 1 block のみ（別 TextBlock なし）
        assert len(result.content) == 1
        r = _parse_text(result)
        assert "notes" in r
        # count のみ → notes なし
        result2 = data_tools["summarize_records"](
            "procedures-survey-r6", metrics=["count"], group_by=["所管府省庁"],
        )
        assert len(result2.content) == 1


# ============================================================
# UI 無効モード (--no-ui)
# ============================================================


class TestUiDisabledMode:
    def test_no_ui_and_ui_modes(self, data_tools_no_ui, data_tools):
        """--no-ui モードでプレーン JSON テキスト、UI モードで ToolResult を返すこと。"""
        cases = [
            data_tools_no_ui["query_records"]("procedures-survey-r6"),
            data_tools_no_ui["summarize_records"]("procedures-survey-r6", metrics=["count"]),
        ]
        for result in cases:
            assert isinstance(result, str)
            data = _parse(result)
            assert "columns" in data
            assert "rows" in data
            assert "field_metadata" in data
        # UI 有効: text と structured_content は同一キー
        result = data_tools["query_records"]("procedures-survey-r6")
        assert isinstance(result, ToolResult)
        assert result.structured_content is not None
        assert "field_metadata" in result.structured_content
        text_data = _parse_text(result)
        assert text_data.keys() == result.structured_content.keys()
