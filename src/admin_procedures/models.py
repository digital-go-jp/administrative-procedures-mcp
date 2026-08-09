"""admin_procedures.models — データモデル・フィールド定義・データセット管理。

SDMX Data Structure Definition (DSD) を参考にしたスキーマ体系を定義する。

Classes:
    ComponentRole       -- 次元 / 属性 / メジャー を表す列挙型
    FieldNotes          -- フィールドに付随する注釈・注意事項
    ComponentDef        -- 1 カラムのメタ情報 (名前・役割・コードリスト等)
    CodelistItem        -- コードリスト内の 1 項目
    CodelistDef         -- コードリストの定義と逆引きマップ
    ComputedMeasureDef  -- 仮想算出メジャーの定義
    DataStructureDefinition -- データセット全体のスキーマ
    FieldDef            -- YAML fields セクションの 1 行分
    DatasetVersion      -- 特定バージョンのデータセット
    DatasetEntry        -- データセットカタログエントリ
    DatasetRegistry     -- 全データセットの管理レジストリ

Functions:
    has_data            -- data が空でないか判定する
    build_provenance    -- provenance 出典メタデータ辞書を構築する
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar
import unicodedata

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _normalize_lookup_key(value: str) -> str:
    """lookup 用にキーを正規化する。"""
    return unicodedata.normalize("NFKC", value).strip().casefold()


# 完全一致以外でフィールド名が解決された際の {入力名: 正式名} の記録先。
# track_field_resolutions() のコンテキスト内でのみ記録される。
_field_resolutions: ContextVar[dict[str, str] | None] = ContextVar(
    "_field_resolutions", default=None,
)


@contextmanager
def track_field_resolutions() -> Iterator[dict[str, str]]:
    """フィールド名の自動補正（正規化一致・類似一致）を記録するコンテキスト。

    コンテキスト内で _fuzzy_get が完全一致以外でキーを解決した場合、
    yield した dict に {入力名: 解決された正式名} が蓄積される。
    暗黙の補正をツール応答 (resolved_fields) で開示するために使う。
    """
    log: dict[str, str] = {}
    token = _field_resolutions.set(log)
    try:
        yield log
    finally:
        _field_resolutions.reset(token)


def _record_resolution(requested: str, resolved: str) -> None:
    log = _field_resolutions.get()
    if log is not None and requested != resolved:
        log[requested] = resolved


def _fuzzy_get(mapping: dict[str, _T], key: str) -> _T | None:
    """正確一致 → 正規化一致 → 類似文字列一致の順でフォールバック検索する。

    LLM が類似漢字を混同するケース (懸→憸 等) を救済するため、
    正規化でも見つからない場合は difflib で最も近いキーにフォールバックする。
    完全一致以外で解決した場合は track_field_resolutions() に記録し、
    補正の事実を応答に開示できるようにする。
    """
    exact = mapping.get(key)
    if exact is not None:
        return exact
    normalized = _normalize_lookup_key(key)
    for k, v in mapping.items():
        if _normalize_lookup_key(k) == normalized:
            _record_resolution(key, k)
            return v
    # 類似文字列フォールバック（LLM の漢字混同対策）
    if len(key) >= 3 and mapping:
        import difflib
        matches = difflib.get_close_matches(key, list(mapping.keys()), n=1, cutoff=0.85)
        if matches:
            _record_resolution(key, matches[0])
            return mapping[matches[0]]
    return None


# ============================================================
# DSD データモデル
# ============================================================


class ComponentRole(str, Enum):
    DIMENSION = "dimension"
    MEASURE = "measure"
    ATTRIBUTE = "attribute"
    IDENTIFIER = "identifier"


@dataclass(frozen=True)
class FieldNotes:
    """フィールドに付随する注意事項。

    details: LLM に送信する詳細な注意事項リスト
    """

    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComponentDef:
    """DSD コンポーネント。"""

    ja: str
    role: ComponentRole
    group: str = ""
    codelist_ref: str | None = None
    data_type: str = "string"       # "string" | "integer" | "float"
    groupable: bool = False
    filterable: bool = False
    aggregatable: bool = False
    multi_value: bool = False
    description: str | None = None
    notes: FieldNotes = FieldNotes()


@dataclass(frozen=True)
class CodelistItem:
    """コードリスト内の1項目。"""

    value: str                      # Parquet 生データそのまま ("1 実施済")
    description: str | None = None


@dataclass(frozen=True)
class CodelistDef:
    """名前付きコードリスト。"""

    codelist_id: str
    label: str
    items: tuple[CodelistItem, ...]
    field_name: str  # 対応するコンポーネントのフィールド名
    is_static: bool = True  # YAML codelists セクション定義=True, auto/auto_split=False

    @property
    def size(self) -> int:
        return len(self.items)

    def get_item(self, value: str) -> CodelistItem | None:
        """指定値に一致するコードリスト項目を返す。"""
        for item in self.items:
            if item.value == value:
                return item
        return None


@dataclass(frozen=True)
class ComputedMeasureDef:
    """DSD 算出数値項目。他の数値項目から導出される仮想フィールド。"""

    name: str                # "オンライン率"
    mode: str = "sum_ratio"  # "sum_ratio" | "count_where"
    numerator: str | None = None        # sum_ratio 用
    denominator: str | None = None      # sum_ratio 用
    condition_field: str | None = None  # count_where 用
    condition_values: tuple[str, ...] = ()  # count_where 用
    data_type: str = "float"
    format: str = "ratio"    # "ratio" (0-1) or "percentage" (0-100)
    description: str = ""
    notes: FieldNotes = FieldNotes()


@dataclass(frozen=True)
class DataStructureDefinition:
    """1データセット・バージョンの完全な構造定義（DSD）。"""

    dataset_id: str
    version: str
    components: tuple[ComponentDef, ...]
    codelists: tuple[CodelistDef, ...]
    computed_measures: tuple[ComputedMeasureDef, ...] = ()

    # 内部 lookup（__post_init__ で構築）
    _by_name: dict[str, ComponentDef] = field(
        default_factory=dict, repr=False, compare=False,
    )
    _by_role: dict[ComponentRole, list[ComponentDef]] = field(
        default_factory=dict, repr=False, compare=False,
    )
    _codelist_map: dict[str, CodelistDef] = field(
        default_factory=dict, repr=False, compare=False,
    )
    _computed_by_name: dict[str, ComputedMeasureDef] = field(
        default_factory=dict, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        """内部索引を構築し、算出数値項目のバリデーションを行う。"""
        by_name = {c.ja: c for c in self.components}
        by_role: dict[ComponentRole, list[ComponentDef]] = {}
        for c in self.components:
            by_role.setdefault(c.role, []).append(c)
        cl_map = {cl.codelist_id: cl for cl in self.codelists}

        # 算出数値項目の索引 + バリデーション
        cm_map: dict[str, ComputedMeasureDef] = {}
        for cm in self.computed_measures:
            if cm.name in by_name:
                raise ValueError(
                    f"Computed measure '{cm.name}' conflicts with existing component."
                )
            if cm.mode == "count_where":
                if not cm.condition_field or by_name.get(cm.condition_field) is None:
                    raise ValueError(
                        f"Computed measure '{cm.name}': condition_field "
                        f"'{cm.condition_field}' not found."
                    )
            else:  # sum_ratio
                num = by_name.get(cm.numerator)
                if num is None or not num.aggregatable:
                    raise ValueError(
                        f"Computed measure '{cm.name}': numerator '{cm.numerator}' "
                        f"must be an aggregatable measure."
                    )
                den = by_name.get(cm.denominator)
                if den is None or not den.aggregatable:
                    raise ValueError(
                        f"Computed measure '{cm.name}': denominator '{cm.denominator}' "
                        f"must be an aggregatable measure."
                    )
            cm_map[cm.name] = cm

        # frozen dataclass のため通常の代入は不可。object.__setattr__ で直接設定する。
        object.__setattr__(self, "_by_name", by_name)
        object.__setattr__(self, "_by_role", by_role)
        object.__setattr__(self, "_codelist_map", cl_map)
        object.__setattr__(self, "_computed_by_name", cm_map)

    # --- 役割別アクセサ ---

    @property
    def identifier(self) -> ComponentDef | None:
        """識別子コンポーネントを返す。存在しない場合は None。"""
        ids = self._by_role.get(ComponentRole.IDENTIFIER, [])
        return ids[0] if ids else None

    @property
    def dimensions(self) -> list[ComponentDef]:
        """分析軸役割のコンポーネント一覧を返す。"""
        return self._by_role.get(ComponentRole.DIMENSION, [])

    @property
    def measures(self) -> list[ComponentDef]:
        """数値項目役割のコンポーネント一覧を返す。"""
        return self._by_role.get(ComponentRole.MEASURE, [])

    @property
    def attributes(self) -> list[ComponentDef]:
        """属性役割のコンポーネント一覧を返す。"""
        return self._by_role.get(ComponentRole.ATTRIBUTE, [])

    # --- lookup ---

    def get_component(self, name: str) -> ComponentDef | None:
        """フィールド名でコンポーネントを検索する。正規化フォールバック付き。"""
        return _fuzzy_get(self._by_name, name)

    def get_codelist(self, codelist_id: str) -> CodelistDef | None:
        """コードリスト ID でコードリストを検索する。"""
        return self._codelist_map.get(codelist_id)

    def get_computed_measure(self, name: str) -> ComputedMeasureDef | None:
        """算出数値項目を検索する。正規化フォールバック付き。"""
        return _fuzzy_get(self._computed_by_name, name)

    def all_field_names(self) -> list[str]:
        """全コンポーネントのフィールド名リストを返す。"""
        return [c.ja for c in self.components]


# ============================================================
# フィールド定義・版情報・出典
# ============================================================


@dataclass(frozen=True)
class FieldDef:
    """1つのデータフィールドの定義。"""
    ja: str            # 日本語カラム名（Parquet カラム名 = フィールドキー）
    csv_col_index: int # CSV→Parquet 変換用の列番号 (prepare_dataset.py で使用)
    group: str         # 論理グループ


# --- ヘルパー関数 ---


def has_data(data: Any) -> bool:
    """データが存在するか判定する。DataFrame/LazyFrame/list 対応。"""
    if data is None:
        return False
    import polars as pl
    if isinstance(data, pl.LazyFrame):
        # LazyFrame は len() 不可。スキーマが存在すれば有効とみなす。
        return len(data.collect_schema().names()) > 0
    return len(data) > 0


def _clean_value(value: object) -> object:
    """numpy型/NaN を Python ネイティブ型に変換。空値は None。"""
    if value is None:
        return None
    if hasattr(value, "item"):          # numpy scalar
        value = value.item()
    if isinstance(value, float) and value != value:  # NaN
        return None
    if value == "":
        return None
    return value


def build_provenance(
    entry: DatasetEntry,
    ver: DatasetVersion,
) -> dict[str, Any]:
    """全ツール共通の出典メタデータブロックを構築する。

    値が未設定 (空文字/None) の項目は出力しない。空の出典項目を返すと
    LLM が「調査時点は空である」等と誤って解釈する余地を残すため。
    """
    fields = {
        "dataset_title": entry.title,
        "as_of_date": ver.as_of_date,
        "published_at": ver.published_at,
        "fetched_at": ver.fetched_at,
        "source_url": ver.source_url,
        "source_note": ver.source_note,
        "publisher": entry.publisher,
    }
    return {k: v for k, v in fields.items() if v}


# ============================================================
# データセットレジストリ
# ============================================================


@dataclass
class DatasetVersion:
    """あるデータセットの1バージョン。"""

    dataset_id: str
    version: str
    as_of_date: str
    published_at: str
    source_url: str
    # 配布元から取得した日 (as_of_date = データの基準時点 とは別物)
    fetched_at: str = ""
    # 出典の補足メモ (dataset.yaml の source.note。単位・取得元の注記など)
    source_note: str = ""

    # ランタイム状態（遅延ロードで充填。polars DataFrame）
    data: Any = field(default=None, repr=False)
    dsd: DataStructureDefinition | None = None

    # 品質統計キャッシュ: {field_name: FieldQualityStats} — 初回アクセス時に全フィールド一括計算
    _quality_stats_cache: dict[str, Any] | None = field(
        default=None, repr=False,
    )


@dataclass
class DatasetEntry:
    """登録済みデータセット。"""

    dataset_id: str
    title: str
    publisher: str
    ver: DatasetVersion | None = None

    # データ読み込み関数
    data_loader: Callable[[], Any] | None = None
    # 表示抑制する汎用値 (YAML generic_values)
    generic_values: set[str] = field(default_factory=set)
    # Parquet メタデータから取得したレコード数 (データ本体のロード不要)
    record_count: int = 0
    # dataset.yaml の schema_version
    schema_version: str | None = None
    # 遅延ロード用メタデータ (finalize_entry で使用)
    _yaml_config: dict[str, Any] | None = field(default=None, repr=False)
    _dataset_dir: Path | None = field(default=None, repr=False)


class DatasetRegistry:
    """全データセットの中央レジストリ。"""

    def __init__(self) -> None:
        """空のレジストリを初期化する。"""
        self._datasets: dict[str, DatasetEntry] = {}

    def register(self, entry: DatasetEntry) -> None:
        """データセットをレジストリに登録する。同一 ID は上書きされる。"""
        self._datasets[entry.dataset_id] = entry

    def get_dataset(self, dataset_id: str) -> DatasetEntry | None:
        """データセット ID で DatasetEntry を取得する。"""
        return self._datasets.get(dataset_id)

    def list_datasets(self) -> list[DatasetEntry]:
        """登録済みの全 DatasetEntry をリストで返す。"""
        return list(self._datasets.values())

    def resolve(
        self,
        dataset_id: str,
    ) -> tuple[DatasetEntry, DatasetVersion] | None:
        """dataset_id を解決し (entry, version) を返す。

        初回アクセス時にデータを遅延ロードする。
        """
        entry = self._datasets.get(dataset_id)
        if not entry:
            return None
        self.ensure_loaded(entry)
        if not entry.ver:
            return None
        return (entry, entry.ver)

    def ensure_loaded(self, entry: DatasetEntry) -> None:
        """必要に応じてデータセットのデータを遅延ロードし DSD を確定する。"""
        ver = entry.ver
        if ver and has_data(ver.data):
            return

        self._load_entry(entry)

        # DSD 確定 (循環 import 回避のため遅延 import)
        from admin_procedures.loader import finalize_entry
        finalize_entry(entry)

    def _load_entry(self, entry: DatasetEntry) -> None:
        """データセットのデータをロードする。データは polars LazyFrame。"""
        if not entry.data_loader or not entry.ver:
            return
        ver = entry.ver
        if has_data(ver.data):
            return
        data = entry.data_loader()
        ver.data = data
        logger.info(
            "Loaded LazyFrame for %s version %s",
            entry.dataset_id,
            ver.version,
        )
