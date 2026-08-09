# dataset.yaml 記述ガイド

`datasets/<dataset_id>/dataset.yaml` はデータセットの定義ファイルです。
Python コードを変更せずに、データセットの追加・変更が可能です。

## 目次

- [基本構造](#基本構造)
- [メタデータ](#メタデータ)
- [フィールド (fields)](#フィールド-fields)
- [コードリスト (codelist)](#コードリスト-codelist)
- [算出メジャー (computed_measures)](#算出メジャー-computed_measures)
- [汎用値除外 (generic_values)](#汎用値除外-generic_values)
- [データセットの作成手順](#データセットの作成手順)

---

## 基本構造

```yaml
schema_version: "1"                  # YAML スキーマバージョン (必須)
title: データセット名                # 日本語タイトル
publisher: 発行者名
id_field: 手続ID                    # 識別子フィールドの name
data_file: data.parquet             # Parquet ファイルパス (dataset.yaml からの相対)

source:
  url: https://example.com/data
  csv_filename: original.csv        # prepare_dataset.py が参照する CSV ファイル名
  csv_header_rows: 1                # CSV ヘッダー行数

as_of_date: '2024-03-31'           # データ基準日
published_at: '2025-07-24'         # 公開日

fields: ...
computed_measures: ...
generic_values: ...
```

> **Note**: データセット ID は `datasets/` 配下のディレクトリ名から自動取得されます。YAML 内に `id:` キーを記述した場合はそちらが優先されます。

---

## メタデータ

| キー | 必須 | デフォルト | 説明 |
|------|------|-----------|------|
| `schema_version` | Yes | - | YAML フォーマットのスキーマバージョン。現在は `"1"` |
| `title` | No | ディレクトリ名 | 日本語タイトル（最大256字、改行・制御文字禁止） |
| `publisher` | No | `""` | 発行組織（最大256字、改行・制御文字禁止） |
| `id_field` | No | `""` | 識別子フィールドの `name`。Parquet カラム名と一致させる |
| `data_file` | Yes | - | Parquet ファイルへの相対パス (dataset.yaml からの相対) |
| `as_of_date` | No | `""` | データ基準日 (YYYY-MM-DD) |
| `published_at` | No | `""` | 公開日 (YYYY-MM-DD) |
| `tags` | No | `[]` | 分類タグの配列。説明用メタデータ（ツールは参照しない） |
| `update_frequency` | No | `""` | 更新頻度。説明用メタデータ（ツールは参照しない） |
| `contact` | No | `""` | 問い合わせ先。説明用メタデータ（ツールは参照しない） |
| `enabled` | No | `true` | `false` にするとレジストリへの登録をスキップする |

### source セクション

```yaml
source:
  url: https://example.com/data        # 配布ページURL
  asset_pattern: 'data.*\.csv$'        # 配布ページ内で探すファイル名パターン
  csv_filename: data.csv               # 元CSVファイル名 (prepare_dataset.py が使用)
  csv_header_rows: 1                   # CSVヘッダー行数 (prepare_dataset.py が使用)
  legal_basis: ''                      # 根拠法令など。説明用メタデータ
  note: ''                             # 出典の補足メモ。provenance に反映
```

| キー | 必須 | 説明 |
|------|------|------|
| `url` | No | データ公開 URL。`provenance.source_url` に反映され、`apcli fetch` では配布ページとして使用 |
| `asset_pattern` | `apcli fetch` 使用時 | 配布ページ内のリンクから取得対象を選ぶ正規表現 |
| `csv_filename` | No | 元 CSV ファイル名。`prepare_dataset.py` で使用 |
| `csv_header_rows` | No | CSV ヘッダー行数 (デフォルト: 1)。非負整数 |
| `legal_basis` | No | 配布データの根拠法令など。説明用メタデータ（ツールは参照しない） |
| `note` | No | 出典の補足メモ（単位・取得元の注記など）。`provenance.source_note` として全ツール応答と UI の出典表示に反映。256 文字以内・改行不可 |

`source` セクションは上記以外のキーを受け付けません（タイプミス検出のため、未知キーはロード時にエラーになります）。

---

## フィールド (fields)

データの各カラムを定義します。

```yaml
fields:
- role: id
  name: 手続ID                # フィールド名 = Parquet カラム名 (必須)
  desc: 手続を一意に識別するID
  csv_col_index: 0

- role: dim
  name: 所管府省庁
  codelist: auto               # データから自動生成
  desc: 手続が規定されている法令を所管する府省庁。

- role: measure
  name: 総手続件数
  data_type: integer
  desc: 令和5年度の年間の全手続件数。
  notes:
    - 件数は有効数字2桁以上の概数であり、一部試算値を含む。
```

### ロール (role)

| ロール | ComponentRole | groupable | filterable | aggregatable | data_type デフォルト |
|--------|---------------|-----------|------------|--------------|---------------------|
| `id` | IDENTIFIER | No | Yes | No | string |
| `dim` | DIMENSION | Yes | Yes | No | string |
| `measure` | MEASURE | No | No | Yes | integer |
| `attr` | ATTRIBUTE | Yes | No | No | string |

- **`id`**: レコードの一意識別子
- **`dim`**: グループ化・フィルタリング可能な分類軸
- **`measure`**: 数値データ。`sum`, `avg`, `min`, `max` で集計可能
- **`attr`**: 属性。デフォルトでグループ化可能。`groupable: false` で無効化可

### フィールドオプション

| キー | 必須 | デフォルト | 説明 |
|------|------|-----------|------|
| `name` | Yes | - | フィールド名 (Parquet カラム名と一致) |
| `role` | Yes | - | `id`, `dim`, `measure`, `attr` のいずれか |
| `groupable` | No | ロールによる | `true`/`false` でロールデフォルトを上書き |
| `desc` | No | - | フィールドの説明文 |
| `data_type` | No | ロールによる | `string`, `integer`, `float` のいずれか |
| `codelist` | No | - | コードリスト (下記参照) |
| `multi_value` | No | `false` | セミコロン区切りの複数値フィールド |
| `notes` | No | `[]` | データ解釈上の注意事項リスト。ツール出力の `data_caveats` に反映 |
| `csv_col_index` | No | 配列インデックス | CSV カラムの0始まりインデックス |
| `group` | No | `""` | フィールドの分類グループ名 |

### notes (注意事項)

フィールドのデータ解釈上の注意事項を定義します。ツール出力（`summarize_records`, `query_records`）の `data_caveats` に反映され、LLM に常に送信されます。

```yaml
- name: オンライン手続件数
  role: measure
  data_type: integer
  notes:
    - "null（欠損）は「件数不明」を意味する。0 は基本的に「オンライン手続なし」だが、地方等で件数集計が困難な一部の手続では 0 と記録されている場合がある。"
    - "件数は有効数字1〜2桁程度の概数であり、一部試算値を含む。"
```

算出メジャー（`computed_measures`）は、関連フィールドの notes を自動継承します。

> **Note**: 数値フィールド (`data_type: integer`) で CSV の空セルは Parquet 上で `null` になります。
> `0` と `null` は異なる意味を持ちます（`0` = ゼロ、`null` = 不明/欠損）。
> 集計（`summarize_records`）では `null` レコードは自動的に除外されます。

---

## コードリスト (codelist)

フィールドが取りうる値の一覧をフィールド定義内に記述します。

### 指定方法

| 値 | 動作 |
|------|------|
| リスト (`[...]`) | インライン静的コードリスト |
| `auto` | データから一意値を自動抽出 |
| `auto_split` | セミコロン区切りの各値を自動抽出 |

### インライン静的コードリスト

```yaml
fields:
- name: 手続類型
  role: dim
  codelist:
  - 1 申請等: 申請、届出その他の法令の規定に基づき行政機関等に対して行われる通知
  - 2-1 申請等に基づく処分通知等: 上記1の申請等に基づき処分の通知
  - 3 縦覧等                    # 説明なしの場合は文字列のみ
  - 4 作成・保存等
```

各アイテムは **文字列** または **1エントリの dict** で指定します。

```yaml
codelist:
# 文字列形式: 値のみ
- 1 実施済
- 2 未実施

# dict 形式: 値と説明
- 1 実施済: オンライン化を実施済の場合に選択する。
- 2 未実施: オンライン化の実施を予定または検討中の場合等に選択する。
```

### 自動コードリスト

```yaml
# データから一意な値を自動収集
- name: 所管府省庁
  codelist: auto

# セミコロン区切りの値を分割して収集
- name: 申請を提出する機関
  codelist: auto_split
  multi_value: true
```

---

## 算出メジャー (computed_measures)

既存フィールドから算出される比率などを定義します。2つのモードがあります。

### sum_ratio モード (デフォルト)

measure フィールドの比率を計算します。

```yaml
computed_measures:
- name: オンライン率
  mode: sum_ratio
  numerator: オンライン手続件数   # 分子の measure フィールド name
  denominator: 総手続件数         # 分母の measure フィールド name
  desc: オンライン手続件数 / 総手続件数
```

### count_where モード

条件に一致するレコードの割合を計算します。

```yaml
computed_measures:
- name: オンライン率
  mode: count_where
  condition_field: オンライン化の実施状況
  condition_values:
  - 1 実施済
  desc: オンライン化実施済の手続種類数 / 全手続種類数
```

### オプション

| キー | 必須 | デフォルト | 説明 |
|------|------|-----------|------|
| `name` | Yes | - | 日本語名 |
| `mode` | No | `sum_ratio` | `sum_ratio` または `count_where` |
| `numerator` | sum_ratio 時 | - | 分子の measure フィールド name |
| `denominator` | sum_ratio 時 | - | 分母の measure フィールド name |
| `condition_field` | count_where 時 | - | 条件フィールド name |
| `condition_values` | count_where 時 | - | 条件に一致する値のリスト |
| `data_type` | No | `float` | データ型 |
| `format` | No | `ratio` | `ratio` (0〜1) または `percentage` (0〜100) |
| `desc` | No | - | 説明文 |
| `notes` | No | `[]` | 注意事項リスト。未指定でも関連フィールドの notes を自動継承 |

---

## 汎用値除外 (generic_values)

UI の分布表示で非表示にする汎用的な値を定義します。
「その他」のような意味の薄い値がチャートを占有するのを防ぎます。

```yaml
generic_values:
  - その他
```

データのフィルタリングや集計には影響せず、UI 表示のみに影響します。

---

## データセットの作成手順

### 1. 新規データセットの作成

`prepare_dataset.py` を使えば、CSV から YAML 定義と Parquet を自動生成できます。

```bash
python -m admin_procedures.prepare_dataset my-dataset --csv source-data/my_data.csv
```

- `dataset.yaml` が存在しない場合、CSV を分析してフィールドの role を自動分類し、YAML と Parquet を生成します
- `--header-rows 2` でヘッダー行数を指定可能（デフォルト: 1）

生成後は YAML を編集して desc、codelist、source.note（出典の補足メモ）などを精緻化してください。

### 2. 既存データセットの Parquet 再変換

```bash
# YAML の source.csv_filename を使用
python -m admin_procedures.prepare_dataset my-dataset

# CSV ファイルを明示指定
python -m admin_procedures.prepare_dataset my-dataset --csv source-data/updated.csv
```

- `dataset.yaml` が存在する場合、YAML の fields 定義（`csv_col_index`）に基づいて Parquet を変換します

### 3. YAML の強制再生成

```bash
python -m admin_procedures.prepare_dataset my-dataset --csv source-data/my_data.csv --force-scaffold
```

`--force-scaffold` を付けると、既存の `dataset.yaml` を上書きして再生成します。

---

## ディレクトリ構成

```
datasets/
  my-dataset/
    dataset.yaml              # データセット定義 (必須)
    data.parquet              # Parquet データファイル
```

- `datasets/` ディレクトリ直下に `dataset.yaml` を持つサブディレクトリが自動検出される
- Parquet ファイルのパスは `data_file` で指定する (dataset.yaml からの相対パス)
