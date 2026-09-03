"""応答に付く出典情報と notes の範囲を検証する。

- inspect_dataset の応答に provenance が付くこと
- list_datasets の各要素に基準日（as_of_date）と公開日（published_at）が付くこと
- notes が metrics 由来のフィールドだけでなく、group_by / where（summarize）や
  where（query の select 指定時）に使ったフィールドにも付くこと
- preview の提案クエリが $not_empty の書式（値は null）に揃っていること
"""

from pathlib import Path

from admin_procedures.response import (
    build_inspect_response,
    build_list_response,
    execute_query,
    execute_summarize,
    resolve_dataset,
)

DATASET_ID = "procedures-survey-r6"
ROOT_DIR = Path(__file__).resolve().parent.parent


class TestProvenanceOnDiscoveryTools:
    def test_inspect_has_provenance(self, registry):
        entry, ver, dsd = resolve_dataset(registry, DATASET_ID)
        result = build_inspect_response(entry, ver, dsd, dataset_id=DATASET_ID)
        prov = result["provenance"]
        assert prov["dataset_title"] == entry.title
        assert prov["as_of_date"] == ver.as_of_date
        assert prov["published_at"] == ver.published_at
        assert "source_url" in prov

    def test_list_items_carry_dates(self, registry):
        result = build_list_response(registry)
        item = next(d for d in result["datasets"] if d["dataset_id"] == DATASET_ID)
        _, ver, _ = resolve_dataset(registry, DATASET_ID)
        assert item["as_of_date"] == ver.as_of_date
        assert item["published_at"] == ver.published_at

    def test_list_items_omit_unset_dates(self, registry):
        """未設定の日付はキーごと省く（空値を出すと「基準日は空」と誤読されるため）。"""
        _, ver, _ = resolve_dataset(registry, DATASET_ID)
        ver.as_of_date = ""
        result = build_list_response(registry)
        item = next(d for d in result["datasets"] if d["dataset_id"] == DATASET_ID)
        assert "as_of_date" not in item
        assert "published_at" in item


class TestNotesScope:
    def test_summarize_where_field_notes_included(self, registry):
        """count だけの集計でも、where に使ったフィールドの notes は付く。"""
        result = execute_summarize(
            registry, DATASET_ID, metrics=["count"],
            where={"総手続件数": {"$gte": 1}},
        )
        assert "総手続件数" in result["notes"]

    def test_summarize_group_by_without_notes_stays_silent(self, registry):
        """notes を持たないフィールドだけの集計では notes を出さない（従来どおり）。"""
        result = execute_summarize(
            registry, DATASET_ID, metrics=["count"], group_by=["所管府省庁"],
        )
        assert "notes" not in result

    def test_query_where_field_notes_included_with_select(self, registry):
        """select で notes 対象を外しても、where に使ったフィールドの notes は付く。"""
        result = execute_query(
            registry, DATASET_ID, select=["手続ID", "手続名"],
            where={"オンライン手続件数": {"$gte": 1}},
        )
        assert "オンライン手続件数" in result["notes"]

    def test_query_select_only_stays_silent(self, registry):
        result = execute_query(registry, DATASET_ID, select=["手続ID", "手続名"])
        assert "notes" not in result


class TestPreviewSuggestionSyntax:
    def test_not_empty_suggestion_uses_null(self):
        """preview の提案クエリはサーバーが受け付ける書式 {"$not_empty": null} を使う。"""
        html = (ROOT_DIR / "src" / "admin_procedures" / "ui" / "preview_host.html").read_text(encoding="utf-8")
        assert '"$not_empty": true' not in html
        assert '"$not_empty": null' in html


class TestPreviewSessionRecovery:
    def test_destroyed_session_is_treated_as_recoverable(self):
        """Chrome 側でセッションが失効したエラー ("has been destroyed" / InvalidStateError) を
        セッション再作成の対象として扱うこと。message だけでなく name も判定に使う。"""
        html = (ROOT_DIR / "src" / "admin_procedures" / "ui" / "preview_host.html").read_text(encoding="utf-8")
        assert "function _sessionLost(err)" in html
        assert "destroyed" in html.split("function _sessionLost(err)")[1].split("}")[0]
        assert "InvalidStateError/i.test(name)" in html
        assert "_sessionLost(err)" in html.split("function lmPrompt(")[1]


class TestPreviewSelectedDataset:
    def test_selected_dataset_is_kept_as_default(self):
        """タイルで選んだデータセットを、質問ごとの並べ替えより優先すること。
        dataset_id の補完も選択中のデータセットを最優先にする。"""
        html = (ROOT_DIR / "src" / "admin_procedures" / "ui" / "preview_host.html").read_text(encoding="utf-8")
        assert "var selectedDatasetId = null;" in html
        # タイル選択 (inspectAndStartSession) で記録し、再探索 (runDiscovery) で解除する
        assert "selectedDatasetId = dataset.dataset_id;" in html.split("function inspectAndStartSession(")[1].split("\n}")[0]
        assert "selectedDatasetId = null;" in html.split("function runDiscovery(")[1].split("\n}")[0]
        # 選択中は ensureCatalogFor の並べ替えを行わない
        body = html.split("function ensureCatalogFor(")[1].split("\n}")[0]
        assert "selectedDatasetId" in body and "return Promise.resolve();" in body
        # dataset_id 補完の優先順位
        assert "out.dataset_id = selectedDatasetId ||" in html
