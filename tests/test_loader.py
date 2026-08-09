"""config_loader モジュールのテスト。

YAML 設定からの登録結果が、Python 直接定義と同等であることを検証する。
"""

from pathlib import Path
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from admin_procedures.loader import (
    auto_register_all,
    build_dsd_from_config,
    build_field_map,
    build_static_codelists,
    discover_datasets,
    load_dataset_yaml,
)
from admin_procedures.models import (
    DatasetRegistry,
)


# ============================================================
# ディスカバリ
# ============================================================


class TestDiscovery:
    def test_discover_datasets(self):
        """datasets/ ディレクトリから procedures-survey-r6 を検出する。"""
        dirs = discover_datasets(ROOT_DIR)
        assert len(dirs) >= 1
        names = [d.name for d in dirs]
        assert "procedures-survey-r6" in names

    def test_discover_empty_dir(self, tmp_path):
        """空ディレクトリでは何も検出されない。"""
        assert discover_datasets(tmp_path) == []


# ============================================================
# YAML 読み込み
# ============================================================


class TestLoadYaml:
    def test_load_dataset_yaml(self):
        """dataset.yaml を読み込めること。"""
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        assert "version" not in config
        assert len(config["fields"]) == 38

    def test_inline_codelists_present(self):
        """インラインコードリストがフィールド定義に含まれること。"""
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        # codelist がリスト形式でインライン定義されていること
        inline_count = sum(
            1 for fd in config.get("fields", [])
            if isinstance(fd.get("codelist"), list)
        )
        assert inline_count >= 10

    @pytest.mark.parametrize("source_value", ["[]", '""', "0", "null"])
    def test_source_must_be_mapping_even_when_falsey(self, tmp_path, source_value):
        """空値を含め、source の非辞書値を受理しないこと。"""
        (tmp_path / "dataset.yaml").write_text(
            f"source: {source_value}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="source セクションは辞書"):
            load_dataset_yaml(tmp_path)

    def test_source_omitted_values_are_not_materialized_as_none(self, tmp_path):
        """省略フィールドを None で追加せず、利用側のデフォルトを保つこと。"""
        (tmp_path / "dataset.yaml").write_text(
            "source:\n  url: https://example.test/data\n",
            encoding="utf-8",
        )

        config = load_dataset_yaml(tmp_path)

        assert config["source"] == {"url": "https://example.test/data"}

    def test_top_level_must_be_mapping(self, tmp_path):
        (tmp_path / "dataset.yaml").write_text("[]\n", encoding="utf-8")

        with pytest.raises(ValueError, match="トップレベルは辞書"):
            load_dataset_yaml(tmp_path)

    def test_removed_allowed_hosts_key_is_rejected_with_guidance(self, tmp_path):
        """廃止した source.allowed_hosts を黙って無視せず、移行先を案内すること。"""
        (tmp_path / "dataset.yaml").write_text(
            "source:\n"
            "  url: https://example.test/data\n"
            "  allowed_hosts:\n"
            "    - safe.example\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="--allowed-host"):
            load_dataset_yaml(tmp_path)

    @pytest.mark.parametrize("bad_value", ["-1", "true", "1.0", '"1"'])
    def test_csv_header_rows_must_be_nonnegative_strict_int(self, tmp_path, bad_value):
        """csv_header_rows は非負整数のみ。bool/float/文字列/負数を受理しないこと。"""
        (tmp_path / "dataset.yaml").write_text(
            f"source:\n  csv_header_rows: {bad_value}\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="source セクションが不正"):
            load_dataset_yaml(tmp_path)

    @pytest.mark.parametrize("good_value, expected", [("0", 0), ("2", 2)])
    def test_csv_header_rows_accepts_nonnegative_int(self, tmp_path, good_value, expected):
        (tmp_path / "dataset.yaml").write_text(
            f"source:\n  csv_header_rows: {good_value}\n",
            encoding="utf-8",
        )

        config = load_dataset_yaml(tmp_path)

        assert config["source"]["csv_header_rows"] == expected

    def test_source_typo_key_is_rejected(self, tmp_path):
        """source の未知キー（タイプミス）を黙って保持しないこと。"""
        (tmp_path / "dataset.yaml").write_text(
            "source:\n  asset_patern: 'data.*\\.csv$'\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="source セクションが不正"):
            load_dataset_yaml(tmp_path)

    def test_source_accepts_scaffold_keys(self, tmp_path):
        """apcli add が生成する legal_basis を含む source を受理すること。"""
        (tmp_path / "dataset.yaml").write_text(
            "source:\n"
            "  url: ''\n"
            "  legal_basis: ''\n"
            "  csv_filename: data.csv\n"
            "  csv_header_rows: 1\n",
            encoding="utf-8",
        )

        config = load_dataset_yaml(tmp_path)

        assert config["source"]["legal_basis"] == ""

    def test_source_accepts_note(self, tmp_path):
        """source.note（出典の補足メモ）を受理すること。"""
        (tmp_path / "dataset.yaml").write_text(
            "source:\n"
            "  url: https://example.test/data\n"
            "  note: e-Stat 全国表より。単位=%。\n",
            encoding="utf-8",
        )

        config = load_dataset_yaml(tmp_path)

        assert config["source"]["note"] == "e-Stat 全国表より。単位=%。"

    def test_unknown_top_level_key_warns_but_loads(self, tmp_path, caplog):
        """トップレベルの未知キーは警告のみで、ロードは続行すること。"""
        import logging

        (tmp_path / "dataset.yaml").write_text(
            "titel: タイプミス\nfields:\n- name: 手続ID\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="admin_procedures.loader"):
            config = load_dataset_yaml(tmp_path)

        assert config["titel"] == "タイプミス"
        assert any("titel" in r.message for r in caplog.records)

    def test_fields_must_be_list(self, tmp_path):
        (tmp_path / "dataset.yaml").write_text(
            "fields:\n  name: 手続ID\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="fields セクションはリスト"):
            load_dataset_yaml(tmp_path)

    def test_fields_null_is_normalized_to_empty_list(self, tmp_path):
        """`fields:` とだけ書かれた未記入状態を、後段の TypeError にしないこと。"""
        (tmp_path / "dataset.yaml").write_text(
            "fields:\n",
            encoding="utf-8",
        )

        config = load_dataset_yaml(tmp_path)

        assert config["fields"] == []
        assert build_field_map(config) == []

    def test_non_string_top_level_keys_do_not_break_warning(self, tmp_path, caplog):
        """文字列と非文字列のキーが混在しても警告処理が落ちないこと。"""
        import logging

        (tmp_path / "dataset.yaml").write_text(
            "1: 数値キー\ntitel: タイプミス\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="admin_procedures.loader"):
            config = load_dataset_yaml(tmp_path)

        assert config[1] == "数値キー"
        assert any("titel" in r.message for r in caplog.records)

    def test_field_entry_requires_name(self, tmp_path):
        """name のないフィールド定義を、後段の KeyError ではなく明確なエラーにすること。"""
        (tmp_path / "dataset.yaml").write_text(
            "fields:\n- name: 手続ID\n- desc: 名前を忘れた\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"fields\[1\]"):
            load_dataset_yaml(tmp_path)


# ============================================================
# フィールドマップ構築
# ============================================================


class TestBuildFieldMap:
    def test_field_count(self):
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        field_map = build_field_map(config)
        assert len(field_map) == 38

    def test_field_properties(self):
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        field_map = build_field_map(config)

        fd = field_map[0]
        assert fd.ja == "手続ID"
        assert fd.csv_col_index == 0

    def test_field_map_consistency(self):
        """YAML フィールド定義の自己整合性を検証する。"""
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        yaml_fields = build_field_map(config)

        assert len(yaml_fields) == 38
        # 名前がユニーク
        names = [f.ja for f in yaml_fields]
        assert len(names) == len(set(names))
        # 先頭は手続ID
        assert yaml_fields[0].ja == "手続ID"
        # csv_col_index が連番
        for i, f in enumerate(yaml_fields):
            assert f.csv_col_index == i, f"{f.ja}: csv_col_index {f.csv_col_index} != {i}"


# ============================================================
# コードリスト構築
# ============================================================


class TestBuildCodelists:
    def test_inline_codelist(self):
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        field_map = build_field_map(config)
        cls = build_static_codelists(config, field_map)

        cl_map = {cl.field_name: cl for cl in cls}
        cl = cl_map["手続類型"]
        assert cl.size == 6
        assert cl.items[0].value == "1 申請等"
        assert cl.items[0].description is not None



# ============================================================
# DSD 構築
# ============================================================


class TestBuildDsd:
    def test_dsd_components(self):
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        field_map = build_field_map(config)
        cls = build_static_codelists(config, field_map)

        dsd = build_dsd_from_config(
            config, field_map, tuple(cls),
            "procedures-survey-r6",
        )
        assert len(dsd.components) == 38
        assert dsd.identifier is not None
        assert dsd.identifier.ja == "手続ID"

    def test_computed_measures(self):
        dataset_dir = ROOT_DIR / "datasets" / "procedures-survey-r6"
        config = load_dataset_yaml(dataset_dir)
        field_map = build_field_map(config)
        cls = build_static_codelists(config, field_map)
        dsd = build_dsd_from_config(
            config, field_map, tuple(cls),
            "procedures-survey-r6",
        )
        cm = dsd.get_computed_measure("オンライン率")
        assert cm is not None
        assert cm.mode == "count_where"
        assert cm.condition_field == "オンライン化の実施状況"
        assert cm.condition_values == ("1 実施済",)


# ============================================================
# 統合テスト: auto_register_all
# ============================================================


class TestAutoRegister:
    def test_register_and_finalize(self):
        """YAML から自動登録の E2E テスト。"""
        registry = DatasetRegistry()
        auto_register_all(registry, ROOT_DIR)

        entries = registry.list_datasets()
        assert len(entries) >= 1

        entry = registry.get_dataset("procedures-survey-r6")
        assert entry is not None
        assert entry.ver is not None
        assert entry.ver.version == "procedures-survey-r6"
