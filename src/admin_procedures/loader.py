"""admin_procedures.loader — YAML パーサ・DSD 構築・データセット自動登録。

datasets/<dataset_id>/dataset.yaml を走査し、DatasetRegistry に自動登録する。
Python コード変更なしでデータセットを追加可能にする。

Functions:
    resolve_data_dir              -- データセットディレクトリのパス解決
    discover_datasets             -- base_dir 配下の dataset.yaml を探索
    load_dataset_yaml             -- YAML ファイルの読み込み
    build_field_map               -- fields 定義から FieldDef リストを構築
    build_static_codelists        -- YAML codelists セクションから静的コードリスト構築
    build_dynamic_codelists       -- auto/auto_split フィールドからデータ駆動コードリスト構築
    build_dsd_from_config         -- YAML 設定から DSD を構築
    auto_register_all             -- 全データセットを DatasetRegistry に自動登録
    init_registry                 -- DatasetRegistry の初期化ショートカット
    finalize_entry                -- 遅延ロードのエントリポイント (models から呼出)
    build_test_dsd                -- テスト用 DSD 一括構築
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from admin_procedures.models import (
    CodelistDef,
    CodelistItem,
    ComponentDef,
    ComponentRole,
    ComputedMeasureDef,
    DataStructureDefinition,
    DatasetEntry,
    DatasetRegistry,
    DatasetVersion,
    FieldDef,
    FieldNotes,
    has_data,
)
from admin_procedures.validation import (
    validate_dataset_id,
    validate_metadata_string,
    resolve_under,
)

logger = logging.getLogger(__name__)


# ============================================================
# YAML スキーマ定義（pydantic）
# ============================================================


class SourceConfig(BaseModel):
    """dataset.yaml の source セクション。

    extra="forbid" により、タイプミスや未知キーは黙って無視されずエラーになる。
    キーを追加する場合はスキーマバージョン管理（CURRENT_SCHEMA_VERSION）に従う。
    """

    url: str | None = None
    asset_pattern: str | None = None
    csv_filename: str | None = None
    csv_header_rows: int | None = Field(default=None, ge=0, strict=True)
    legal_basis: str | None = None
    note: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_allowed_hosts(cls, data: Any) -> Any:
        # extra="forbid" でも弾かれるが、廃止キーには移行先を明示的に案内する
        if isinstance(data, dict) and "allowed_hosts" in data:
            raise ValueError(
                "source.allowed_hosts は廃止されました。"
                "接続先を制限する場合は apcli fetch --allowed-host <ホスト名> を使用してください",
            )
        return data


# dataset.yaml のトップレベルで使用される既知キー。
# 未知キーはエラーにせず警告に留める（利用者の注記キーでデータセットを
# ロード不能にしないため）。タイプミス（fields → field 等）の検出が目的。
_KNOWN_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "id", "title", "publisher", "tags", "update_frequency",
    "contact", "id_field", "source", "data_file", "as_of_date", "published_at",
    "fields", "generic_values", "computed_measures", "enabled",
})


DATASETS_DIR_NAME = "datasets"
DATA_DIR_ENV_VAR = "ADMIN_PROCEDURES_DATA_DIR"
# apcli fetch が取得元・取得日を記録するサイドカー。dataset.yaml は配布される
# 定義ファイルなので、ローカル固有の取得状態はこちらに分離する（.gitignore 対象）。
FETCH_STATE_FILENAME = ".fetch.json"

# dataset.yaml スキーマバージョン
# YAML フォーマット自体の互換性管理に使用する。
# 新しいトップレベルキーの追加や既存キーの意味変更時にインクリメントする。
# source 配下などへの後方互換な任意キーの追加はバージョン据え置きとする。
CURRENT_SCHEMA_VERSION = "1"
SUPPORTED_SCHEMA_VERSIONS = {"1"}

# role 短縮名 → ComponentRole + フラグ
_ROLE_MAP: dict[str, tuple[ComponentRole, str, bool, bool, bool]] = {
    "id": (ComponentRole.IDENTIFIER, "string", False, True, False),
    "dim": (ComponentRole.DIMENSION, "string", True, True, False),
    "measure": (ComponentRole.MEASURE, "integer", False, False, True),
    "attr": (ComponentRole.ATTRIBUTE, "string", True, False, False),
}


# ============================================================
# ディスカバリ
# ============================================================


def discover_datasets(base_dir: Path) -> list[Path]:
    """datasets/ ディレクトリを走査し、dataset.yaml を持つサブディレクトリを返す。"""
    datasets_dir = base_dir / DATASETS_DIR_NAME
    if not datasets_dir.exists():
        return []
    return sorted(
        d for d in datasets_dir.iterdir()
        if d.is_dir() and (d / "dataset.yaml").exists()
    )


# ============================================================
# YAML 読み込み
# ============================================================


def load_dataset_yaml(dataset_dir: Path) -> dict[str, Any]:
    """dataset.yaml を読み込み、スキーマをバリデーションする。"""
    path = dataset_dir / "dataset.yaml"
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError("dataset.yaml のトップレベルは辞書で指定してください")

    # トップレベルの未知キーはタイプミスの可能性があるため警告する（ロードは続行）
    unknown_keys = set(config) - _KNOWN_TOP_LEVEL_KEYS
    if unknown_keys:
        logger.warning(
            "dataset.yaml に未知のキーがあります（タイプミスの可能性）: %s (%s)",
            sorted(unknown_keys, key=str), path,  # 非文字列キーが混在してもソート可能に
        )

    # source セクションをバリデーション
    if "source" in config:
        raw_source = config["source"]
        if not isinstance(raw_source, dict):
            raise ValueError(
                "dataset.yaml の source セクションは辞書で指定してください",
            )
        try:
            source_config = SourceConfig.model_validate(raw_source)
            config["source"] = source_config.model_dump(exclude_none=True)
        except ValidationError as e:
            raise ValueError(f"dataset.yaml の source セクションが不正です: {e}") from e

    # fields セクションの構造チェック（build_field_map が前提とする不変条件）
    if "fields" in config:
        if config["fields"] is None:
            # `fields:` とだけ書かれた未記入状態はキー省略と同じ扱いにする
            config["fields"] = []
        if not isinstance(config["fields"], list):
            raise ValueError("dataset.yaml の fields セクションはリストで指定してください")
        for i, fd in enumerate(config["fields"]):
            if not isinstance(fd, dict) or not isinstance(fd.get("name"), str) or not fd["name"].strip():
                raise ValueError(
                    f"dataset.yaml の fields[{i}] には name（空でない文字列）が必要です",
                )

    return config


def load_fetch_state(dataset_dir: Path) -> dict[str, Any]:
    """apcli fetch が書いた .fetch.json を読む。無ければ空 dict。"""
    import json

    path = dataset_dir / FETCH_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Ignoring unreadable fetch state: %s", path)
        return {}


# ============================================================
# フィールド構築
# ============================================================


def build_field_map(config: dict[str, Any]) -> list[FieldDef]:
    """YAML の fields セクションから FieldDef リストを構築する。"""
    fields: list[FieldDef] = []
    for i, fd in enumerate(config.get("fields", [])):
        fields.append(FieldDef(
            ja=fd["name"],
            csv_col_index=fd.get("csv_col_index", i),
            group=fd.get("group", ""),
        ))
    return fields


# ============================================================
# コードリスト構築
# ============================================================


def _parse_codelist_items(raw_items: list[Any]) -> tuple[CodelistItem, ...]:
    """codelist の items リストを CodelistItem タプルに変換する。"""
    items: list[CodelistItem] = []
    for entry in raw_items:
        if isinstance(entry, str):
            items.append(CodelistItem(value=entry))
        elif isinstance(entry, dict):
            v = str(next(iter(entry)))
            desc = entry[next(iter(entry))]
            items.append(CodelistItem(
                value=v, description=str(desc) if desc else None,
            ))
    return tuple(items)


def build_static_codelists(
    config: dict[str, Any],
    field_map: list[FieldDef],
) -> list[CodelistDef]:
    """フィールド定義のインライン codelist から静的 CodelistDef を構築する。

    codelist: [list] → インライン定義（リスト形式）
    codelist: "auto" / "auto_split" → build_dynamic_codelists で処理（ここではスキップ）
    """
    result: list[CodelistDef] = []
    for fd in config.get("fields", []):
        cl = fd.get("codelist")
        if cl is None or isinstance(cl, str):
            # auto / auto_split / None → ここでは処理しない
            continue
        if isinstance(cl, list):
            col_ja = fd["name"]
            cl_id = f"CL_{col_ja.upper()}"
            result.append(CodelistDef(
                codelist_id=cl_id,
                label=col_ja,
                items=_parse_codelist_items(cl),
                field_name=col_ja,
            ))

    return result


def build_dynamic_codelists(
    config: dict[str, Any],
    data: Any,
) -> list[CodelistDef]:
    """codelist: auto / auto_split のフィールドからデータ駆動コードリストを構築する。

    data は polars DataFrame を前提とする。
    """
    import polars as pl

    result: list[CodelistDef] = []
    for fd in config.get("fields", []):
        codelist_type = fd.get("codelist")
        if codelist_type not in ("auto", "auto_split"):
            continue

        col_ja = fd["name"]
        cl_id = f"CL_{col_ja.upper()}"

        if col_ja not in data.columns:
            continue
        col_series = data[col_ja].drop_nulls().cast(pl.Utf8)

        if codelist_type == "auto":
            values = sorted(col_series.filter(col_series.str.strip_chars() != "").unique().to_list())
        else:  # auto_split
            split_series = (
                col_series.filter(col_series.str.strip_chars() != "")
                .str.split(";").explode().str.strip_chars()
            )
            split_series = split_series.filter(split_series != "").unique()
            values = sorted(split_series.to_list())

        result.append(CodelistDef(
            codelist_id=cl_id,
            label=col_ja,
            items=tuple(CodelistItem(value=v) for v in values),
            field_name=col_ja,
            is_static=False,
        ))

    return result


# ============================================================
# DSD 構築
# ============================================================


def _parse_field_notes(fd_yaml: dict[str, Any]) -> FieldNotes:
    """YAML フィールド定義から FieldNotes を構築する。"""
    notes_raw = fd_yaml.get("notes")
    if notes_raw is not None:
        if isinstance(notes_raw, dict):
            return FieldNotes(
                details=tuple(notes_raw.get("details", [])),
            )
        # notes: ["item1", "item2"] リスト形式
        if isinstance(notes_raw, list):
            return FieldNotes(details=tuple(notes_raw))
    return FieldNotes()


def _resolve_computed_notes(
    cm_yaml: dict[str, Any],
    components_by_name: dict[str, ComponentDef],
) -> FieldNotes:
    """算出数値項目の notes を解決する。YAML 直接指定 + 関連フィールドから継承。"""
    explicit = _parse_field_notes(cm_yaml)
    details: list[str] = list(explicit.details)

    if cm_yaml.get("mode") == "count_where":
        cond_comp = components_by_name.get(cm_yaml.get("condition_field", ""))
        if cond_comp and cond_comp.notes.details:
            for c in cond_comp.notes.details:
                details.append(f"[{cond_comp.ja}より継承] {c}")
    else:
        num_comp = components_by_name.get(cm_yaml.get("numerator", ""))
        if num_comp and num_comp.notes.details:
            for c in num_comp.notes.details:
                details.append(f"[{num_comp.ja}より継承] {c}")

        den_comp = components_by_name.get(cm_yaml.get("denominator", ""))
        if den_comp and den_comp.notes.details:
            for c in den_comp.notes.details:
                details.append(f"[{den_comp.ja}より継承] {c}")

    return FieldNotes(details=tuple(details))


def build_dsd_from_config(
    config: dict[str, Any],
    field_map: list[FieldDef],
    codelists: tuple[CodelistDef, ...],
    dataset_id: str,
) -> DataStructureDefinition:
    """YAML 設定から完全な DSD を組み立てる。"""
    fields_yaml = config.get("fields", [])

    # codelist_ref の逆引き: field_name → codelist_id
    codelist_ref_map: dict[str, str] = {}
    for cl in codelists:
        if cl.field_name:
            codelist_ref_map[cl.field_name] = cl.codelist_id

    components: list[ComponentDef] = []
    for fd, fd_yaml in zip(field_map, fields_yaml):
        role_key = fd_yaml.get("role", "attr")
        role_info = _ROLE_MAP.get(role_key, _ROLE_MAP["attr"])
        role, default_dtype, groupable, filterable, aggregatable = role_info
        if "groupable" in fd_yaml:
            groupable = bool(fd_yaml["groupable"])
        data_type = fd_yaml.get("data_type", default_dtype)
        multi_value = fd_yaml.get("multi_value", False)

        field_notes = _parse_field_notes(fd_yaml)

        components.append(ComponentDef(
            ja=fd.ja,
            role=role,
            group=fd.group,
            codelist_ref=codelist_ref_map.get(fd.ja),
            data_type=data_type,
            groupable=groupable,
            filterable=filterable,
            aggregatable=aggregatable,
            multi_value=multi_value,
            description=fd_yaml.get("desc"),
            notes=field_notes,
        ))

    # 算出数値項目（関連フィールドの notes を自動継承）
    by_name_tmp = {c.ja: c for c in components}

    def _build_computed(cm: dict[str, Any]) -> ComputedMeasureDef:
        mode = cm.get("mode", "sum_ratio")
        computed_notes = _resolve_computed_notes(cm, by_name_tmp)
        base = dict(
            name=cm["name"],
            mode=mode,
            data_type=cm.get("data_type", "float"),
            format=cm.get("format", "ratio"),
            description=cm.get("desc", ""),
            notes=computed_notes,
        )
        if mode == "count_where":
            base["condition_field"] = cm["condition_field"]
            base["condition_values"] = tuple(cm.get("condition_values", []))
        else:
            base["numerator"] = cm["numerator"]
            base["denominator"] = cm["denominator"]
        return ComputedMeasureDef(**base)

    computed_measures = tuple(
        _build_computed(cm) for cm in config.get("computed_measures", [])
    )

    return DataStructureDefinition(
        dataset_id=dataset_id,
        version=dataset_id,
        components=tuple(components),
        codelists=codelists,
        computed_measures=computed_measures,
    )


# ============================================================
# Parquet ローダー
# ============================================================


def _load_parquet(path: Path) -> Any:
    """Parquet ファイルを polars LazyFrame として返す (scan_parquet)。

    実データの読み込みは .collect() 呼び出しまで遅延される。
    polars が自動で projection / predicate pushdown を適用する。
    """
    import polars as pl

    if not path.exists():
        return pl.LazyFrame()
    return pl.scan_parquet(path)


def _parquet_record_count(path: Path) -> int:
    """Parquet の行数をメタデータのみから取得する (データ本体はロードしない)。

    ``scan_parquet().select(pl.len())`` は row group メタデータだけを読むため、
    レジストリ登録時に全データセット分を呼んでも安価に済む。
    取得できない場合は 0 を返す。
    """
    import polars as pl

    if not path.exists():
        return 0
    try:
        return int(pl.scan_parquet(path).select(pl.len()).collect().item())
    except Exception:
        logger.warning("Failed to read record count from %s", path, exc_info=True)
        return 0


# ============================================================
# レジストリ登録
# ============================================================


def _validate_schema_version(config: dict[str, Any], dataset_id: str) -> None:
    """dataset.yaml の schema_version を検証する。

    未指定の場合は警告を出しつつ続行する（後方互換）。
    未サポートバージョンの場合は ValueError を送出する。
    """
    sv = config.get("schema_version")
    if sv is None:
        logger.warning(
            "Dataset '%s' has no schema_version. "
            "Add 'schema_version: \"%s\"' to dataset.yaml.",
            dataset_id,
            CURRENT_SCHEMA_VERSION,
        )
        return
    sv_str = str(sv)
    if sv_str not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Dataset '{dataset_id}': unsupported schema_version '{sv_str}'. "
            f"Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}. "
            f"Update the loader or downgrade the YAML."
        )


def _register_one(
    registry: DatasetRegistry,
    dataset_dir: Path,
    config: dict[str, Any],
) -> None:
    """1つのデータセットを YAML 設定からレジストリに登録する。"""
    if config.get("enabled") is False:
        logger.info("Skipping disabled dataset '%s'", config.get("id", dataset_dir.name))
        return

    dataset_id = config.get("id", dataset_dir.name)

    # スキーマバージョンの検証
    _validate_schema_version(config, dataset_id)

    source = config.get("source", {})

    ver = DatasetVersion(
        dataset_id=dataset_id,
        version=dataset_id,
        as_of_date=config.get("as_of_date", ""),
        published_at=config.get("published_at", ""),
        fetched_at=str(load_fetch_state(dataset_dir).get("fetched_at", "") or ""),
        source_url=source.get("url", ""),
        source_note=validate_metadata_string(
            source.get("note", ""),
            label="source.note",
        ),
    )

    data_file_path = config.get("data_file", "")

    # パストラバーサル対策：data_file を containment チェック
    try:
        validated_data_file = resolve_under(dataset_dir, data_file_path, label="data_file")
    except ValueError as e:
        raise ValueError(f"Dataset '{dataset_id}': {e}") from e

    # メタデータの検証・サニタイズ
    validated_dataset_id = validate_dataset_id(dataset_id)
    validated_title = validate_metadata_string(
        config.get("title", dataset_id),
        label="title",
    )
    validated_publisher = validate_metadata_string(
        config.get("publisher", ""),
        label="publisher",
    )

    entry = DatasetEntry(
        dataset_id=validated_dataset_id,
        title=validated_title,
        publisher=validated_publisher,
        ver=ver,
        generic_values=set(config.get("generic_values", [])),
        record_count=_parquet_record_count(validated_data_file),
        schema_version=str(config["schema_version"]) if config.get("schema_version") else None,
    )

    # データローダーをクロージャで作成
    def make_data_loader(data_path: Path) -> Callable[[], Any]:
        def loader() -> Any:
            return _load_parquet(data_path)
        return loader

    entry.data_loader = make_data_loader(validated_data_file)

    registry.register(entry)

    # finalize_entry() で使うメタデータを保持
    entry._yaml_config = config
    entry._dataset_dir = dataset_dir


# ============================================================
# 公開 API
# ============================================================


def resolve_data_dir(explicit_path: str | Path | None = None) -> Path:
    """datasets/ を含むベースディレクトリを解決する。

    優先順:
        1. explicit_path 引数 (--data-dir)
        2. ADMIN_PROCEDURES_DATA_DIR 環境変数
        3. パッケージ相対 (リポジトリルートの datasets/)
        4. カレントディレクトリ

    環境変数をここで解決することで、MCP サーバー・apcli・prepare_dataset の
    どこから呼んでも同じディレクトリを指すようにする。

    Raises:
        FileNotFoundError: datasets/ が見つからない場合。
    """
    if explicit_path is None:
        explicit_path = os.environ.get(DATA_DIR_ENV_VAR) or None

    if explicit_path is not None:
        p = Path(explicit_path)
        if (p / DATASETS_DIR_NAME).exists():
            return p
        if p.name == DATASETS_DIR_NAME and p.is_dir():
            return p.parent
        raise FileNotFoundError(f"No '{DATASETS_DIR_NAME}/' found at {p}")

    # パッケージ相対 (editable install / repo checkout)
    pkg_root = Path(__file__).resolve().parent.parent.parent
    if (pkg_root / DATASETS_DIR_NAME).exists():
        return pkg_root

    # CWD フォールバック
    cwd = Path.cwd()
    if (cwd / DATASETS_DIR_NAME).exists():
        return cwd

    raise FileNotFoundError(
        f"Cannot find '{DATASETS_DIR_NAME}/' directory. "
        f"Set --data-dir or ADMIN_PROCEDURES_DATA_DIR environment variable."
    )


def auto_register_all(registry: DatasetRegistry, base_dir: Path) -> None:
    """datasets/ 配下の全データセットを自動検出・登録する。"""
    dataset_dirs = discover_datasets(base_dir)
    for dataset_dir in dataset_dirs:
        try:
            config = load_dataset_yaml(dataset_dir)
            _register_one(registry, dataset_dir, config)
            logger.info("Registered dataset '%s' from %s", config.get("id", dataset_dir.name), dataset_dir.name)
        except Exception:
            logger.exception("Failed to load dataset from %s", dataset_dir)


def finalize_entry(entry: DatasetEntry) -> None:
    """データセットに DSD を設定する。

    データが読み込み済み (LazyFrame or DataFrame) であることが前提。
    動的コードリスト構築に必要な列だけを collect する。
    """
    import polars as pl

    config = entry._yaml_config
    if config is None:
        return

    ver = entry.ver
    if ver is None or not has_data(ver.data):
        return

    dataset_dir = entry._dataset_dir
    field_map = build_field_map(config)
    static_cls = build_static_codelists(config, field_map)

    # 動的コードリスト用の列だけを collect して渡す
    auto_cols = [
        fd["name"] for fd in config.get("fields", [])
        if fd.get("codelist") in ("auto", "auto_split")
    ]
    if auto_cols and isinstance(ver.data, pl.LazyFrame):
        lazy_cols = ver.data.collect_schema().names()
        available = [c for c in auto_cols if c in lazy_cols]
        auto_data = ver.data.select(available).collect() if available else pl.DataFrame()
    elif isinstance(ver.data, pl.LazyFrame):
        auto_data = pl.DataFrame()
    else:
        auto_data = ver.data

    dynamic_cls = build_dynamic_codelists(config, auto_data)
    all_codelists = tuple(static_cls + dynamic_cls)

    ver.dsd = build_dsd_from_config(
        config, field_map, all_codelists,
        entry.dataset_id,
    )

    logger.info(
        "Finalized DSD for %s version %s (%d components, %d codelists)",
        entry.dataset_id, ver.version,
        len(ver.dsd.components), len(ver.dsd.codelists),
    )


def build_test_dsd(
    dataset_dir: Path,
    dataset_id: str | None = None,
    data: Any | None = None,
) -> DataStructureDefinition:
    """テスト用便利関数: dataset.yaml から DSD を一括構築する。

    *data* が指定された場合は動的コードリスト (auto / auto_split) も含める。
    """
    config = load_dataset_yaml(dataset_dir)
    did = dataset_id or config.get("id", dataset_dir.name)
    field_map = build_field_map(config)
    static_cls = build_static_codelists(config, field_map)
    if data is not None:
        dynamic_cls = build_dynamic_codelists(config, data)
        all_cls = tuple(list(static_cls) + dynamic_cls)
    else:
        all_cls = tuple(static_cls)
    return build_dsd_from_config(config, field_map, all_cls, did)


def init_registry(base_dir: Path) -> DatasetRegistry:
    """DatasetRegistry を初期化する。データは初回アクセス時に遅延ロードされる。"""
    registry = DatasetRegistry()
    auto_register_all(registry, base_dir)
    return registry
