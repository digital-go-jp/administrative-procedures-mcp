"""datasets/dataset-v1.schema.json の適合テスト。

同梱の dataset.yaml がスキーマに適合し続けること（フォーマット変更時の
スキーマ更新漏れの検出）を検証する。
"""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT_DIR / "datasets" / "dataset-v1.schema.json"
# 同梱の全 dataset.yaml（datasets/*/dataset.yaml）を対象にする
BUNDLED_YAMLS = sorted((ROOT_DIR / "datasets").glob("*/dataset.yaml"))


class TestDatasetSchema:
    def test_schema_is_well_formed(self):
        """スキーマ自体が draft-07 として妥当であること。"""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft7Validator.check_schema(schema)

    def test_bundled_yamls_present(self):
        """同梱 dataset.yaml を取りこぼしていないこと（glob が空だと以降のテストが無意味になる）。"""
        assert {p.parent.name for p in BUNDLED_YAMLS} >= {"procedures-survey-r6", "procedures-survey-r7"}

    @pytest.mark.parametrize("yaml_path", BUNDLED_YAMLS, ids=lambda p: p.parent.name)
    def test_bundled_dataset_yaml_is_valid(self, yaml_path):
        """同梱の dataset.yaml がスキーマに適合すること。"""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft7Validator(schema).iter_errors(config))
        assert errors == [], [e.message for e in errors]
