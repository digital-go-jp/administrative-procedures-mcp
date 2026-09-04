"""同梱 dataset.yaml と実データ (Parquet) の整合性テスト。

配布データには解説資料上は単一選択の項目でもセミコロンで複数値を連結した
回答が混じることがある（令和7年度の「添付書類等提出の撤廃/省略状況」など）。
`multi_value` を付けずにいると group_by / where の件数が実際の選択数とずれる
ため、再取り込みや改訂版の取り込みで新たに混入していないかを検出する。

Parquet は配布元から取得するもので同梱していないため、無い環境では skip する。
"""

from pathlib import Path

import polars as pl
import pytest
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DATASETS_DIR = ROOT_DIR / "datasets"
BUNDLED_YAMLS = sorted(DATASETS_DIR.glob("*/dataset.yaml"))
# 令和6年度の定義は公開済みの記事・手順が参照しているため変更しない（凍結）。
# 同年度のデータにも「添付書類等提出の撤廃/省略状況」等にセミコロン連結値が
# あることは確認済みだが、定義を変えない以上このテストの対象からは外す。
FROZEN_DATASETS = {"procedures-survey-r6"}


def _cases():
    for yaml_path in BUNDLED_YAMLS:
        if yaml_path.parent.name in FROZEN_DATASETS:
            continue
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        parquet = yaml_path.parent / config["data_file"]
        for field in config["fields"]:
            if isinstance(field.get("codelist"), list) and not field.get("multi_value"):
                yield pytest.param(parquet, field["name"], id=f"{yaml_path.parent.name}:{field['name']}")


@pytest.mark.parametrize("parquet, name", list(_cases()))
def test_single_value_codelist_field_has_no_semicolon(parquet: Path, name: str):
    """静的 codelist を持つ単一値フィールドの実データにセミコロン連結値が無いこと。"""
    if not parquet.exists():
        pytest.skip(f"{parquet} がない（setup.sh / apcli fetch で取得する）")
    values = pl.read_parquet(parquet, columns=[name])[name].drop_nulls()
    joined = values.filter(values.str.contains(";", literal=True))
    assert joined.len() == 0, (
        f"{name} にセミコロン連結値が {joined.len()} 件ある。"
        " multi_value: true を付けるか、データを確認すること:"
        f" {joined.unique().head(5).to_list()}"
    )
