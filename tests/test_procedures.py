"""admin_procedures パッケージのユニットテスト。

統合テスト (test_tools.py) でカバーされる基本動作は省略し、
複雑なロジックのエッジケースのみテストする。
"""

import base64
import json
import polars as pl
import pytest

from admin_procedures.models import (
    ComponentDef,
    ComponentRole,
    ComputedMeasureDef,
    DataStructureDefinition,
    DatasetEntry,
    DatasetRegistry,
    DatasetVersion,
    _clean_value,
    build_provenance,
)
from admin_procedures.query import (
    CursorCodec,
    collect_field_notes,
    compute_aggregation,
    explode_records,
    paginate,
)
from pathlib import Path

from admin_procedures.loader import build_field_map, build_test_dsd, load_dataset_yaml

_ROOT_DIR = Path(__file__).resolve().parent.parent
_DATASET_DIR = _ROOT_DIR / "datasets" / "procedures-survey-r6"
_config = load_dataset_yaml(_DATASET_DIR)
_FIELD_MAP = build_field_map(_config)


# ============================================================
# ヘルパー
# ============================================================


def _make_entry(dataset_id="ds1", version="v1"):
    ver = DatasetVersion(
        dataset_id=dataset_id,
        version=version,
        as_of_date="2024-01-01",
        published_at="2024-06-01",
        source_url="https://example.com/data",
    )
    entry = DatasetEntry(
        dataset_id=dataset_id,
        title="テストDS",
        publisher="テスト",
        ver=ver,
    )
    return entry


def _make_test_dsd(*, with_computed=False):
    """テスト用の小さなDSD。"""
    comps = [
        ComponentDef(ja="手続ID", role=ComponentRole.IDENTIFIER, filterable=True),
        ComponentDef(ja="所管府省庁", role=ComponentRole.DIMENSION,
                     codelist_ref="CL_MINISTRY", groupable=True, filterable=True),
        ComponentDef(ja="オンライン化の実施状況", role=ComponentRole.DIMENSION,
                     codelist_ref="CL_ONLINE_STATUS", groupable=True, filterable=True),
        ComponentDef(ja="手続名", role=ComponentRole.ATTRIBUTE),
        ComponentDef(ja="総手続件数", role=ComponentRole.MEASURE,
                     data_type="integer", aggregatable=True),
        ComponentDef(ja="オンライン手続件数", role=ComponentRole.MEASURE,
                     data_type="integer", aggregatable=True),
    ]
    computed = ()
    if with_computed:
        computed = (ComputedMeasureDef(
            name="オンライン率",
            numerator="オンライン手続件数", denominator="総手続件数",
            description="オンライン手続件数 / 総手続件数",
        ),)
    from admin_procedures.models import CodelistDef, CodelistItem
    cl = CodelistDef(
        codelist_id="CL_MINISTRY", label="所管府省庁",
        items=(CodelistItem("厚生労働省", "厚生労働省"), CodelistItem("総務省", "総務省")),
        field_name="所管府省庁",
    )
    return DataStructureDefinition(
        dataset_id="test", version="v1",
        components=tuple(comps), codelists=(cl,), computed_measures=computed,
    )


# ============================================================
# Registry — load 系のみ
# ============================================================



# ============================================================
# Cursor / Paginate — エッジケース
# ============================================================


class TestCursorCodec:
    def test_roundtrip(self):
        token = CursorCodec.encode(42, "v1")
        assert CursorCodec.decode(token, "v1") == 42

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode("not-a-valid-cursor!!!", "v1")

    @staticmethod
    def _encode_raw(payload: object) -> str:
        """CursorCodec.encode() を経由せず、任意の payload を base64 化するヘルパー。"""
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def test_toplevel_array_payload_rejected(self):
        token = self._encode_raw([])
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_offset_as_list_rejected(self):
        token = self._encode_raw({"o": [], "v": "v1"})
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_negative_offset_rejected(self):
        token = self._encode_raw({"o": -1, "v": "v1"})
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_offset_as_bool_rejected(self):
        token = self._encode_raw({"o": True, "v": "v1"})
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_offset_as_float_rejected(self):
        token = self._encode_raw({"o": 1.5, "v": "v1"})
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_offset_above_signed_64bit_range_rejected(self):
        token = self._encode_raw({"o": 1 << 63, "v": "v1"})
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_max_signed_64bit_offset_is_accepted(self):
        token = self._encode_raw({"o": (1 << 63) - 1, "v": "v1"})
        assert CursorCodec.decode(token, "v1") == (1 << 63) - 1

    @pytest.mark.parametrize("payload", [{"v": "v1"}, {"o": 1}, {}])
    def test_missing_required_keys_rejected(self, payload):
        token = self._encode_raw(payload)
        with pytest.raises(ValueError, match="カーソル形式が不正です"):
            CursorCodec.decode(token, "v1")

    def test_version_mismatch_reports_versions(self):
        """version 不一致は構造的に妥当なカーソルなので、詳細メッセージが維持されること。"""
        token = CursorCodec.encode(0, "v_old")
        with pytest.raises(ValueError, match="v_old"):
            CursorCodec.decode(token, "v_new")


class TestPaginate:
    def test_first_page(self):
        df = pl.DataFrame({"v": list(range(10))})
        page, cursor, total = paginate(df, 3, None, "v1")
        assert page["v"].to_list() == [0, 1, 2] and cursor is not None and total == 10

    def test_last_page(self):
        df = pl.DataFrame({"v": list(range(5))})
        _, c1, _ = paginate(df, 3, None, "v1")
        page, cursor, _ = paginate(df, 3, c1, "v1")
        assert page["v"].to_list() == [3, 4] and cursor is None

    def test_exact_boundary(self):
        df = pl.DataFrame({"v": list(range(6))})
        _, c1, _ = paginate(df, 3, None, "v1")
        page2, c2, _ = paginate(df, 3, c1, "v1")
        assert len(page2) == 3 and c2 is None


# ============================================================
# Aggregation — null 処理エッジケース
# ============================================================


class TestComputeAggregation:
    def test_null_excluded_in_sum(self):
        dsd = _make_test_dsd()
        df = pl.DataFrame({"総手続件数": [100, None, 200]})
        result = compute_aggregation(df, [], [("sum", "総手続件数", None)], dsd)
        assert result["sum:総手続件数"][0] == 300
        assert result["sum:総手続件数:null_excluded"][0] == 1

    def test_null_excluded_in_avg(self):
        dsd = _make_test_dsd()
        df = pl.DataFrame({"総手続件数": [100, None, None, 300]})
        result = compute_aggregation(df, [], [("avg", "総手続件数", None)], dsd)
        assert result["avg:総手続件数"][0] == 200.0
        assert result["avg:総手続件数:null_excluded"][0] == 2

    def test_all_null_returns_none(self):
        dsd = _make_test_dsd()
        df = pl.DataFrame({"総手続件数": [None, None]})
        result = compute_aggregation(
            df, [],
            [("sum", "総手続件数", None), ("avg", "総手続件数", None),
             ("min", "総手続件数", None), ("max", "総手続件数", None)],
            dsd,
        )
        assert result["sum:総手続件数"][0] == 0
        assert result["avg:総手続件数"][0] is None
        assert result["min:総手続件数"][0] is None
        assert result["max:総手続件数"][0] is None

    def test_null_excluded_in_min_max(self):
        dsd = _make_test_dsd()
        df = pl.DataFrame({"総手続件数": [50, None, 200]})
        result = compute_aggregation(
            df, [], [("min", "総手続件数", None), ("max", "総手続件数", None)], dsd,
        )
        assert result["min:総手続件数"][0] == 50
        assert result["max:総手続件数"][0] == 200
        assert result["min:総手続件数:null_excluded"][0] == 1

    def test_computed_avg_denominator_zero(self):
        dsd = _make_test_dsd(with_computed=True)
        cm = dsd.get_computed_measure("オンライン率")
        df = pl.DataFrame({"総手続件数": [0], "オンライン手続件数": [0]})
        result = compute_aggregation(df, [], [("computed_avg", None, cm)], dsd)
        assert result["avg:オンライン率"][0] is None


# ============================================================
# ComputedMeasure DSD バリデーション
# ============================================================


class TestComputedMeasureValidation:
    def test_valid(self):
        dsd = _make_test_dsd(with_computed=True)
        cm = dsd.get_computed_measure("オンライン率")
        assert cm is not None and cm.numerator == "オンライン手続件数"

    def test_name_conflict_raises(self):
        with pytest.raises(ValueError, match="conflicts with existing component"):
            DataStructureDefinition(
                dataset_id="test", version="v1",
                components=(
                    ComponentDef(ja="総手続件数", role=ComponentRole.MEASURE,
                                 data_type="integer", aggregatable=True),
                    ComponentDef(ja="オンライン手続件数", role=ComponentRole.MEASURE,
                                 data_type="integer", aggregatable=True),
                ),
                codelists=(),
                computed_measures=(ComputedMeasureDef(
                    name="総手続件数",
                    numerator="オンライン手続件数", denominator="総手続件数",
                ),),
            )

    def test_invalid_numerator_raises(self):
        with pytest.raises(ValueError, match="numerator"):
            DataStructureDefinition(
                dataset_id="test", version="v1",
                components=(ComponentDef(ja="総手続件数",
                            role=ComponentRole.MEASURE, data_type="integer", aggregatable=True),),
                codelists=(),
                computed_measures=(ComputedMeasureDef(
                    name="テスト率",
                    numerator="nonexistent", denominator="総手続件数",
                ),),
            )

    def test_non_aggregatable_denominator_raises(self):
        with pytest.raises(ValueError, match="denominator"):
            DataStructureDefinition(
                dataset_id="test", version="v1",
                components=(
                    ComponentDef(ja="総手続件数", role=ComponentRole.MEASURE,
                                 data_type="integer", aggregatable=True),
                    ComponentDef(ja="手続名", role=ComponentRole.ATTRIBUTE),
                ),
                codelists=(),
                computed_measures=(ComputedMeasureDef(
                    name="テスト率",
                    numerator="総手続件数", denominator="手続名",
                ),),
            )


# ============================================================
# explode_records — エッジケース
# ============================================================


class TestExplodeRecords:
    def test_semicolon_split(self):
        df = pl.DataFrame({"イベント": ["A;B", "C"], "件数": [100, 200]})
        result = explode_records(df, "イベント")
        assert len(result) == 3
        assert result["イベント"].to_list() == ["A", "B", "C"]

    def test_empty_values_excluded(self):
        df = pl.DataFrame({"イベント": ["A", "", None, None]})
        result = explode_records(df, "イベント")
        assert len(result) == 1


# ============================================================
# YAML フィールド定義 整合性
# ============================================================


class TestFieldMap:
    def test_has_38_entries(self):
        assert len(_FIELD_MAP) == 38

    def test_unique_names(self):
        names = [f.ja for f in _FIELD_MAP]
        assert len(names) == len(set(names))


# ============================================================
# _clean_value — 特殊値のみ
# ============================================================


class TestCleanValue:
    def test_nan(self):
        assert _clean_value(float("nan")) is None

    def test_numpy_like(self):
        class NumpyInt:
            def item(self):
                return 123
        assert _clean_value(NumpyInt()) == 123


# ============================================================
# Caveats (注意事項) テスト
# ============================================================

_DATASET_DIR = Path(__file__).resolve().parent.parent / "datasets" / "procedures-survey-r6"


class TestFieldNotes:
    """ComponentDef.notes (FieldNotes) のテスト。"""

    def test_notes_default_empty(self):
        comp = ComponentDef(ja="テスト", role=ComponentRole.MEASURE)
        assert comp.notes.details == ()

    def test_notes_from_yaml(self):
        """YAML から読み込んだ DSD に notes.details が含まれること。"""
        dsd = build_test_dsd(_DATASET_DIR)
        comp = dsd.get_component("オンライン手続件数")
        assert comp is not None
        assert len(comp.notes.details) > 0
        assert any("不明" in c for c in comp.notes.details)

    def test_computed_measure_count_where(self):
        """count_where モードの算出数値項目が正しく構成されること。"""
        dsd = build_test_dsd(_DATASET_DIR)
        cm = dsd.get_computed_measure("オンライン率")
        assert cm is not None
        assert cm.mode == "count_where"
        assert cm.condition_field == "オンライン化の実施状況"
        assert "1 実施済" in cm.condition_values


class TestCollectFieldNotes:
    """collect_field_notes() ヘルパーのテスト。"""

    def test_collect_for_specific_field(self):
        dsd = build_test_dsd(_DATASET_DIR)
        notes = collect_field_notes(dsd, ["オンライン手続件数"])
        assert isinstance(notes, dict)
        assert "オンライン手続件数" in notes
        assert len(notes["オンライン手続件数"]) > 0

    def test_collect_for_field_without_notes(self):
        dsd = build_test_dsd(_DATASET_DIR)
        notes = collect_field_notes(dsd, ["手続ID"])
        assert notes == {}

    def test_collect_for_computed_measure(self):
        dsd = build_test_dsd(_DATASET_DIR)
        notes = collect_field_notes(dsd, ["オンライン率"])
        # count_where モードの condition_field に notes.details がないため空
        assert len(notes) == 0

    def test_collect_all_fields(self):
        dsd = build_test_dsd(_DATASET_DIR)
        notes = collect_field_notes(dsd)
        assert len(notes) > 0
        assert "オンライン手続件数" in notes
        assert "総手続件数" in notes


# ============================================================
# provenance
# ============================================================


class TestBuildProvenance:
    def test_includes_populated_fields(self):
        entry = _make_entry()
        prov = build_provenance(entry, entry.ver)

        assert prov["dataset_title"] == "テストDS"
        assert prov["as_of_date"] == "2024-01-01"
        assert prov["source_url"] == "https://example.com/data"

    def test_omits_empty_fields(self):
        """未設定の出典項目は空文字で返さず省略すること (LLM の誤解釈を防ぐ)。"""
        entry = _make_entry()
        entry.ver.as_of_date = ""
        entry.ver.published_at = ""

        prov = build_provenance(entry, entry.ver)

        assert "as_of_date" not in prov
        assert "published_at" not in prov
        assert prov["source_url"] == "https://example.com/data"

    def test_source_note_included_when_set_and_omitted_when_empty(self):
        """source.note は設定時のみ provenance に含めること。"""
        entry = _make_entry()
        assert "source_note" not in build_provenance(entry, entry.ver)

        entry.ver.source_note = "e-Stat 全国表より。単位=%。"
        prov = build_provenance(entry, entry.ver)
        assert prov["source_note"] == "e-Stat 全国表より。単位=%。"
