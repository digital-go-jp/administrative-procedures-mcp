"""共通テストフィクスチャ。"""

from pathlib import Path
import sys

import polars as pl
import pytest

# テストで src パッケージをインポート可能にする
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from admin_procedures.models import DatasetEntry, DatasetRegistry, DatasetVersion
from admin_procedures.loader import (
    build_test_dsd,
    load_dataset_yaml,
    DATA_DIR_ENV_VAR,
)

DATASET_ID = "procedures-survey-r6"

# ============================================================
# サンプルデータ
# ============================================================

SAMPLE_DATA = [
    {
        "手続ID": "1",
        "所管府省庁": "厚生労働省",
        "手続名": "国民年金の届出",
        "法令名": "国民年金法",
        "法令番号": "昭和34年法律第141号",
        "根拠条項号": "第12条",
        "手続類型": "1 申請等",
        "手続主体": "5 国民等",
        "手続の受け手": "3 地方等",
        "経由機関": "",
        "独立行政法人等の名称": "",
        "事務区分": "",
        "府省共通手続": "いいえ",
        "実施府省庁": "厚生労働省",
        "オンライン化の実施状況": "1 実施済",
        "オンライン化の実施予定及び検討時の懸念点": "",
        "オンライン化実施時期": "",
        "申請等における本人確認手法": "マイナンバーカード",
        "手数料等の納付有無": "無",
        "手数料等の納付方法": "",
        "手数料等のオンライン納付時の優遇措置": "",
        "処理期間(オンライン)": "14日",
        "処理期間(非オンライン)": "30日",
        "情報システム(申請)": "e-Gov",
        "情報システム(事務処理)": "年金システム",
        "総手続件数": 500000,
        "オンライン手続件数": 400000,
        "非オンライン手続件数": 100000,
        "申請書等に記載させる情報": "氏名;住所;基礎年金番号",
        "申請時に添付させる書類": "住民票",
        "添付書類等提出の撤廃/省略状況": "",
        "添付書類等の提出方法": "電子",
        "添付書類等への電子署名": "不要",
        "添付形式等が定められた規定": "",
        "手続が行われるイベント(個人)": "就職・転職",
        "手続が行われるイベント(法人)": "",
        "申請に関連する士業": "社会保険労務士;行政書士",
        "申請を提出する機関": "地方等",
    },
    {
        "手続ID": "2",
        "所管府省庁": "厚生労働省",
        "手続名": "雇用保険の届出",
        "法令名": "雇用保険法",
        "法令番号": "昭和49年法律第116号",
        "根拠条項号": "第7条",
        "手続類型": "1 申請等",
        "手続主体": "6 民間事業者等",
        "手続の受け手": "1 国",
        "経由機関": "",
        "独立行政法人等の名称": "",
        "事務区分": "",
        "府省共通手続": "いいえ",
        "実施府省庁": "厚生労働省",
        "オンライン化の実施状況": "2 未実施",
        "オンライン化の実施予定及び検討時の懸念点": "6 オンライン化の費用対効果が小さい又は不明",
        "オンライン化実施時期": "",
        "申請等における本人確認手法": "",
        "手数料等の納付有無": "無",
        "手数料等の納付方法": "",
        "手数料等のオンライン納付時の優遇措置": "",
        "処理期間(オンライン)": "",
        "処理期間(非オンライン)": "60日",
        "情報システム(申請)": "",
        "情報システム(事務処理)": "",
        "総手続件数": 1000,
        "オンライン手続件数": 0,
        "非オンライン手続件数": 1000,
        "申請書等に記載させる情報": "法人番号;商号",
        "申請時に添付させる書類": "登記事項証明書",
        "添付書類等提出の撤廃/省略状況": "",
        "添付書類等の提出方法": "紙",
        "添付書類等への電子署名": "",
        "添付形式等が定められた規定": "",
        "手続が行われるイベント(個人)": "",
        "手続が行われるイベント(法人)": "新規採用",
        "申請に関連する士業": "社会保険労務士",
        "申請を提出する機関": "本府省庁",
    },
    {
        "手続ID": "3",
        "所管府省庁": "総務省",
        "手続名": "電波利用料の納付届出",
        "法令名": "電波法",
        "法令番号": "昭和25年法律第131号",
        "根拠条項号": "第103条の2",
        "手続類型": "1 申請等",
        "手続主体": "6 民間事業者等",
        "手続の受け手": "1 国",
        "経由機関": "",
        "独立行政法人等の名称": "",
        "事務区分": "",
        "府省共通手続": "いいえ",
        "実施府省庁": "総務省",
        "オンライン化の実施状況": "1 実施済",
        "オンライン化の実施予定及び検討時の懸念点": "",
        "オンライン化実施時期": "",
        "申請等における本人確認手法": "",
        "手数料等の納付有無": "有",
        "手数料等の納付方法": "ペイジー",
        "手数料等のオンライン納付時の優遇措置": "",
        "処理期間(オンライン)": "7日",
        "処理期間(非オンライン)": "14日",
        "情報システム(申請)": "電子申請・届出システム",
        "情報システム(事務処理)": "電波利用システム",
        "総手続件数": 200,
        "オンライン手続件数": 150,
        "非オンライン手続件数": 50,
        "申請書等に記載させる情報": "免許番号;法人番号",
        "申請時に添付させる書類": "",
        "添付書類等提出の撤廃/省略状況": "",
        "添付書類等の提出方法": "",
        "添付書類等への電子署名": "",
        "添付形式等が定められた規定": "",
        "手続が行われるイベント(個人)": "",
        "手続が行われるイベント(法人)": "事業開始",
        "申請に関連する士業": "",
        "申請を提出する機関": "本府省庁",
    },
    {
        "手続ID": "4",
        "所管府省庁": "総務省",
        "手続名": "住民基本台帳の閲覧",
        "法令名": "住民基本台帳法",
        "法令番号": "昭和42年法律第81号",
        "根拠条項号": "第11条",
        "手続類型": "3 縦覧等",
        "手続主体": "5 国民等",
        "手続の受け手": "3 地方等",
        "経由機関": "",
        "独立行政法人等の名称": "",
        "事務区分": "",
        "府省共通手続": "いいえ",
        "実施府省庁": "総務省",
        "オンライン化の実施状況": "3 適用除外",
        "オンライン化の実施予定及び検討時の懸念点": "",
        "オンライン化実施時期": "",
        "申請等における本人確認手法": "",
        "手数料等の納付有無": "無",
        "手数料等の納付方法": "",
        "手数料等のオンライン納付時の優遇措置": "",
        "処理期間(オンライン)": "",
        "処理期間(非オンライン)": "",
        "情報システム(申請)": "",
        "情報システム(事務処理)": "",
        "総手続件数": 50,
        "オンライン手続件数": 0,
        "非オンライン手続件数": 50,
        "申請書等に記載させる情報": "",
        "申請時に添付させる書類": "",
        "添付書類等提出の撤廃/省略状況": "",
        "添付書類等の提出方法": "",
        "添付書類等への電子署名": "",
        "添付形式等が定められた規定": "",
        "手続が行われるイベント(個人)": "",
        "手続が行われるイベント(法人)": "",
        "申請に関連する士業": "",
        "申請を提出する機関": "地方等",
    },
]


# ============================================================
# フィクスチャ
# ============================================================


def _build_test_registry(data: list[dict]) -> DatasetRegistry:
    """テスト用レジストリを構築する。"""
    reg = DatasetRegistry()

    dataset_dir = ROOT_DIR / "datasets" / DATASET_ID
    df = pl.from_dicts(data, infer_schema_length=None)
    dsd = build_test_dsd(dataset_dir, data=df)

    config = load_dataset_yaml(dataset_dir)

    ver = DatasetVersion(
        dataset_id=DATASET_ID,
        version=DATASET_ID,
        as_of_date="2024-03-31",
        published_at="2025-07-24",
        source_url="https://www.digital.go.jp/resources/administrative-procedure-survey",
        data=df,
        dsd=dsd,
    )

    entry = DatasetEntry(
        dataset_id=DATASET_ID,
        title="行政手続等の棚卸調査結果",
        publisher="デジタル庁",
        ver=ver,
        generic_values=set(config.get("generic_values", [])),
        record_count=len(df),
    )
    entry._yaml_config = config
    reg.register(entry)
    return reg


@pytest.fixture()
def sample_data():
    """テスト用の4件のサンプルレコードを返す。"""
    return SAMPLE_DATA


@pytest.fixture()
def registry(sample_data):
    """テスト用レジストリを構築して返す。"""
    return _build_test_registry(sample_data)


@pytest.fixture()
def test_data_dir(tmp_path, sample_data, monkeypatch):
    """テスト用データセットを一時ディレクトリに作成し、ADMIN_PROCEDURES_DATA_DIR を設定する。"""
    from admin_procedures.loader import load_dataset_yaml

    # テスト用データセットディレクトリを作成
    dataset_dir = tmp_path / "datasets" / DATASET_ID
    dataset_dir.mkdir(parents=True)

    # サンプルデータを Parquet に変換して保存
    df = pl.from_dicts(sample_data, infer_schema_length=None)
    df.write_parquet(dataset_dir / "data.parquet")

    # dataset.yaml をコピー
    src_yaml = ROOT_DIR / "datasets" / DATASET_ID / "dataset.yaml"
    if src_yaml.exists():
        import shutil
        shutil.copy(src_yaml, dataset_dir / "dataset.yaml")

    # ADMIN_PROCEDURES_DATA_DIR を設定
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))

    # キャッシュをクリア
    from admin_procedures.cli import _get_registry
    _get_registry.cache_clear()

    yield tmp_path

    _get_registry.cache_clear()


def _setup_test_data_env(sample_data, tmp_path, monkeypatch):
    """テスト用データセットを一時ディレクトリに作成し、環境変数を設定する。"""
    from admin_procedures.cli import _get_registry
    import shutil

    # テスト用データセットディレクトリを作成
    dataset_dir = tmp_path / "datasets" / DATASET_ID
    dataset_dir.mkdir(parents=True)

    # サンプルデータを Parquet に変換して保存
    df = pl.from_dicts(sample_data, infer_schema_length=None)
    df.write_parquet(dataset_dir / "data.parquet")

    # dataset.yaml をコピー
    src_yaml = ROOT_DIR / "datasets" / DATASET_ID / "dataset.yaml"
    if src_yaml.exists():
        shutil.copy(src_yaml, dataset_dir / "dataset.yaml")

    # ADMIN_PROCEDURES_DATA_DIR を設定
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))

    # キャッシュをクリア
    _get_registry.cache_clear()


@pytest.fixture(autouse=True, scope="function")
def _setup_test_data_for_query_tests(request, sample_data, tmp_path, monkeypatch):
    """procedures-survey-r6 を必要とするテストでテスト用データセットを自動セットアップする。"""
    # 対象テストクラスのみで実行
    target_classes = (
        "TestQueryUnknownSelect",
        "TestSummarizeUnknownExplode",
        "TestSummarizeNonGroupable",
        "TestSummarizeUnknownGroupBy",
        "TestCLIBasicCommands",
    )

    if not any(c in request.node.nodeid for c in target_classes):
        yield
        return

    _setup_test_data_env(sample_data, tmp_path, monkeypatch)
    yield

    from admin_procedures.cli import _get_registry
    _get_registry.cache_clear()
