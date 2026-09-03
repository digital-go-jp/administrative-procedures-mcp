"""同梱 dataset.yaml 間の整合性テスト。

令和6年度 (r6) と令和7年度 (r7) は同じ調査の年度違いで、共通項目は同じ
フィールド名で定義する。r7 の YAML は配布 Excel の列構成に合わせて手作業で
列位置を付け替えているため、フィールド名の打ち間違いや csv_col_index の
重複・欠落を検出する。実データ (Parquet) は不要。
"""

from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DATASETS_DIR = ROOT_DIR / "datasets"

# 令和7年度で追加された列（配布 Excel のヘッダーと解説資料に基づく）
R7_ADDED_FIELDS = {
    "関連条項号",
    "関連する根拠法令",
    "手続(類型、主体、受け手等)に関する補足",
    "実施府省庁(府省共通手続)",
    "オンライン化の実施状況に関する補足",
    "手数料等に関する補足",
    "情報システム（申請）複数ある場合",
    "添付書類等に関する補足",
    "士業、機関に関する補足",
}
# 令和7年度で無くなった列
R7_REMOVED_FIELDS = {"法令番号"}


def _load(dataset_id: str) -> dict:
    path = DATASETS_DIR / dataset_id / "dataset.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _field_names(config: dict) -> list[str]:
    return [f["name"] for f in config["fields"]]


class TestR6R7Consistency:
    def test_r7_field_names_differ_from_r6_only_by_known_changes(self):
        """共通項目は r6 と同じ名前で定義され、差分は既知の追加・削除だけであること。"""
        r6 = set(_field_names(_load("procedures-survey-r6")))
        r7 = set(_field_names(_load("procedures-survey-r7")))
        assert r7 - r6 == R7_ADDED_FIELDS
        assert r6 - r7 == R7_REMOVED_FIELDS

    def test_r7_common_fields_keep_r6_order(self):
        """共通項目の並び順が r6 と同じであること（配布 Excel でも順序は維持されている）。"""
        r6 = _field_names(_load("procedures-survey-r6"))
        r7 = _field_names(_load("procedures-survey-r7"))
        common = set(r6) & set(r7)
        assert [n for n in r7 if n in common] == [n for n in r6 if n in common]

    def test_r7_csv_col_index_is_contiguous(self):
        """csv_col_index が 0 から欠落・重複なく並ぶこと（Excel の 46 列に対応）。"""
        idx = [f["csv_col_index"] for f in _load("procedures-survey-r7")["fields"]]
        assert idx == list(range(46))

    def test_r7_online_status_has_no_partial_code(self):
        """令和7年度調査に無い「5 一部実施済」を codelist に残していないこと。"""
        config = _load("procedures-survey-r7")
        field = next(f for f in config["fields"] if f["name"] == "オンライン化の実施状況")
        values = [next(iter(c)) if isinstance(c, dict) else c for c in field["codelist"]]
        assert values == ["1 実施済", "2 未実施", "3 適用除外", "4 その他"]

    def test_r7_is_distinguishable_from_r6(self):
        """r6 と並べたときに年度を区別できるよう、r7 は基準時点・公開日・年度入りタイトルを持つこと。

        r6 の dataset.yaml は公開済みの記事・手順が参照しているため変更しない。
        """
        r6 = _load("procedures-survey-r6")
        r7 = _load("procedures-survey-r7")
        assert str(r7["as_of_date"]) == "2025-11-01"
        assert str(r7["published_at"]) == "2026-08-26"
        assert r7["title"] != r6["title"]
        assert "令和7年度" in r7["title"]
