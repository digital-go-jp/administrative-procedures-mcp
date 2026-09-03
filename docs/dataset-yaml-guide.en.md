# dataset.yaml Guide

[日本語](dataset-yaml-guide.md) | English

> This is an English translation of [dataset-yaml-guide.md](dataset-yaml-guide.md). The Japanese version is authoritative; when the two differ, follow the Japanese text. Command examples are kept identical in both versions. The YAML examples use the Japanese field names of the bundled survey dataset, because field names must match the column names of the data.

`datasets/<dataset_id>/dataset.yaml` is the definition file of a dataset.
Datasets can be added or changed without touching the Python code.

## Contents

- [Basic Structure](#basic-structure)
- [Metadata](#metadata)
- [Fields (fields)](#fields-fields)
- [Code Lists (codelist)](#code-lists-codelist)
- [Computed Measures (computed_measures)](#computed-measures-computed_measures)
- [Generic Values (generic_values)](#generic-values-generic_values)
- [Creating a Dataset](#creating-a-dataset)

---

## Basic Structure

```yaml
# yaml-language-server: $schema=../dataset-v1.schema.json
schema_version: "1"                  # YAML schema version (required)
title: データセット名                # Title (Japanese in the bundled datasets)
publisher: 発行者名                  # Publisher
id_field: 手続ID                    # name of the identifier field
data_file: data.parquet             # Parquet file path (relative to dataset.yaml)

source:
  url: https://example.com/data
  csv_filename: original.csv        # CSV file name used by prepare_dataset.py
  csv_header_rows: 1                # number of CSV header rows

as_of_date: '2024-03-31'           # reference date of the data
published_at: '2025-07-24'         # publication date

fields: ...
computed_measures: ...
generic_values: ...
```

> **Note**: The dataset ID is taken from the directory name under `datasets/`. If an `id:` key is written in the YAML, it takes precedence.

---

## Editor Completion and Validation (JSON Schema)

A JSON Schema for dataset.yaml is bundled at [datasets/dataset-v1.schema.json](../datasets/dataset-v1.schema.json). Put the relative path to the schema in the modeline on the first line (the `# yaml-language-server: $schema=...` in the example above) and editors such as VS Code (with the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)) and AI coding agents can use it for completion, typo detection, and type checking (from `datasets/<dataset_id>/dataset.yaml` the path is `../dataset-v1.schema.json`).

The schema is a loose definition for completion and reference at the time of writing, and the modeline is optional. Keys that are not in the schema are still accepted.

---

## Metadata

| Key | Required | Default | Description |
|------|------|-----------|------|
| `schema_version` | Yes | - | Schema version of the YAML format. Currently `"1"` |
| `title` | No | directory name | Title (at most 256 characters; newlines and control characters are rejected) |
| `publisher` | No | `""` | Publishing organization (at most 256 characters; newlines and control characters are rejected) |
| `id_field` | No | `""` | `name` of the identifier field. Must match the Parquet column name |
| `data_file` | Yes | - | Relative path to the Parquet file (relative to dataset.yaml) |
| `as_of_date` | No | `""` | Reference date of the data (YYYY-MM-DD) |
| `published_at` | No | `""` | Publication date (YYYY-MM-DD) |
| `tags` | No | `[]` | Array of classification tags. Descriptive metadata (not used by the tools) |
| `update_frequency` | No | `""` | Update frequency. Descriptive metadata (not used by the tools) |
| `contact` | No | `""` | Contact. Descriptive metadata (not used by the tools) |
| `enabled` | No | `true` | Set to `false` to skip registration in the registry |

### The source Section

```yaml
source:
  url: https://example.com/data        # distribution page URL
  asset_pattern: 'data.*\.csv$'        # file name pattern to look for on the distribution page
  csv_filename: data.csv               # original CSV file name (used by prepare_dataset.py)
  csv_header_rows: 1                   # number of CSV header rows (used by prepare_dataset.py)
  legal_basis: ''                      # legal basis and similar. Descriptive metadata
  note: ''                             # supplementary note on the source. Reported in provenance
```

| Key | Required | Description |
|------|------|------|
| `url` | No | URL where the data is published. Reported as `provenance.source_url`; `apcli fetch` uses it as the distribution page |
| `asset_pattern` | when using `apcli fetch` | Regular expression that selects the file to download among the links on the distribution page |
| `csv_filename` | No | Original CSV file name. Used by `prepare_dataset.py` |
| `csv_header_rows` | No | Number of CSV header rows (default: 1). Non-negative integer |
| `legal_basis` | No | Legal basis of the distributed data and similar. Descriptive metadata (not used by the tools) |
| `note` | No | Supplementary note on the source (units, where it was obtained, and so on). Reported as `provenance.source_note` in every tool response and in the source display of the UI. At most 256 characters, no newlines |

The `source` section accepts no keys other than the above (unknown keys raise an error at load time to catch typos).

---

## Fields (fields)

Defines each column of the data.

```yaml
fields:
- role: id
  name: 手続ID                # field name = Parquet column name (required)
  desc: ID that uniquely identifies a procedure
  csv_col_index: 0

- role: dim
  name: 所管府省庁
  codelist: auto               # generated from the data
  desc: Ministry with jurisdiction over the law that defines the procedure.

- role: measure
  name: 総手続件数
  data_type: integer
  desc: Annual number of procedure instances in FY2023.
  notes:
    - Counts are approximate (two or more significant digits) and partly estimated.
```

### Roles (role)

| Role | ComponentRole | groupable | filterable | aggregatable | default data_type |
|--------|---------------|-----------|------------|--------------|---------------------|
| `id` | IDENTIFIER | No | Yes | No | string |
| `dim` | DIMENSION | Yes | Yes | No | string |
| `measure` | MEASURE | No | No | Yes | integer |
| `attr` | ATTRIBUTE | Yes | No | No | string |

- **`id`**: unique identifier of a record
- **`dim`**: classification axis that can be grouped and filtered
- **`measure`**: numeric data. Can be aggregated with `sum`, `avg`, `min`, `max`
- **`attr`**: attribute. Groupable by default; disable with `groupable: false`

### Field Options

| Key | Required | Default | Description |
|------|------|-----------|------|
| `name` | Yes | - | Field name (must match the Parquet column name) |
| `role` | No | `attr` | One of `id`, `dim`, `measure`, `attr` |
| `groupable` | No | by role | `true`/`false` overrides the role default |
| `desc` | No | - | Description of the field |
| `data_type` | No | by role | One of `string`, `integer`, `float` |
| `codelist` | No | - | Code list (see below) |
| `multi_value` | No | `false` | Semicolon-separated multi-value field |
| `notes` | No | `[]` | List of caveats for interpreting the data. Reported as `notes` in tool responses |
| `csv_col_index` | No | array index | Zero-based index of the CSV column |
| `group` | No | `""` | Name of the field's classification group |

### notes (caveats)

Defines caveats for interpreting a field's data. They are reported as `notes` in tool responses (`summarize_records`, `query_records`): `summarize_records` includes the notes of the fields involved in `metrics` (including the fields a computed measure depends on), and `query_records` includes the notes of the fields in `select`.

```yaml
- name: オンライン手続件数
  role: measure
  data_type: integer
  notes:
    - "null (missing) means the count is unknown. 0 basically means no online procedures, but for some procedures handled by local governments, where counting is difficult, 0 has been recorded."
    - "Counts are approximate (one to two significant digits) and partly estimated."
```

Computed measures (`computed_measures`) inherit the notes of the fields they depend on automatically.

`notes` can be written as a list, or as a mapping `notes: {details: [...]}` (the meaning is the same).

> **Note**: For numeric fields (`data_type: integer`), empty CSV cells become `null` in Parquet.
> `0` and `null` have different meanings (`0` = zero, `null` = unknown/missing).
> Aggregation (`summarize_records`) excludes `null` records automatically.

---

## Code Lists (codelist)

Lists the values a field can take, inside the field definition.

### How to Specify

| Value | Behavior |
|------|------|
| List (`[...]`) | Inline static code list |
| `auto` | Collect the distinct values from the data |
| `auto_split` | Split semicolon-separated values and collect each |

### Inline Static Code List

```yaml
fields:
- name: 手続類型
  role: dim
  codelist:
  - 1 申請等: Applications, notifications, and other notices made to administrative bodies under laws and regulations
  - 2-1 申請等に基づく処分通知等: Notices of dispositions made in response to the applications in 1
  - 3 縦覧等                    # a plain string when there is no description
  - 4 作成・保存等
```

Each item is either a **string** or a **single-entry dict**.

```yaml
codelist:
# string form: value only
- 1 実施済
- 2 未実施

# dict form: value and description
- 1 実施済: Selected when the procedure is already available online.
- 2 未実施: Selected when going online is planned or under consideration.
```

### Automatic Code Lists

```yaml
# collect the distinct values from the data
- name: 所管府省庁
  codelist: auto

# split semicolon-separated values and collect each
- name: 申請を提出する機関
  codelist: auto_split
  multi_value: true
```

---

## Computed Measures (computed_measures)

Defines ratios and similar values derived from existing fields. There are two modes.

### sum_ratio Mode (default)

Computes the ratio of two measure fields.

```yaml
computed_measures:
- name: オンライン率
  mode: sum_ratio
  numerator: オンライン手続件数   # name of the numerator measure field
  denominator: 総手続件数         # name of the denominator measure field
  desc: online procedure count / total procedure count
```

### count_where Mode

Computes the share of records that match a condition.

```yaml
computed_measures:
- name: オンライン率
  mode: count_where
  condition_field: オンライン化の実施状況
  condition_values:
  - 1 実施済
  desc: number of procedure types available online / number of all procedure types
```

### Options

| Key | Required | Default | Description |
|------|------|-----------|------|
| `name` | Yes | - | Name |
| `mode` | No | `sum_ratio` | `sum_ratio` or `count_where` |
| `numerator` | for sum_ratio | - | Name of the numerator measure field |
| `denominator` | for sum_ratio | - | Name of the denominator measure field |
| `condition_field` | for count_where | - | Name of the condition field |
| `condition_values` | for count_where | - | List of values that satisfy the condition |
| `data_type` | No | `float` | Data type |
| `format` | No | `ratio` | `ratio` (0 to 1) or `percentage` (0 to 100) |
| `desc` | No | - | Description |
| `notes` | No | `[]` | List of caveats. When omitted, the notes of the related fields are inherited |

---

## Generic Values (generic_values)

Defines generic values that the UI hides in distribution views.
This keeps low-information values such as "その他" (other) from dominating a chart.

```yaml
generic_values:
  - その他
```

It does not affect filtering or aggregation, only the UI display.

---

## Creating a Dataset

### 1. Create a New Dataset

`prepare_dataset.py` generates the YAML definition and the Parquet file from a CSV.

```bash
python -m admin_procedures.prepare_dataset my-dataset --csv source-data/my_data.csv
```

- When `dataset.yaml` does not exist, the CSV is analyzed, the role of each field is inferred, and the YAML and Parquet are generated
- `--header-rows 2` sets the number of header rows (default: 1)

After generation, edit the YAML to refine `desc`, `codelist`, `source.note` (a supplementary note on the source), and so on.

### 2. Reconvert the Parquet of an Existing Dataset

```bash
# use source.csv_filename from the YAML
python -m admin_procedures.prepare_dataset my-dataset

# specify the CSV file explicitly
python -m admin_procedures.prepare_dataset my-dataset --csv source-data/updated.csv
```

- When `dataset.yaml` exists, the Parquet is converted according to the field definitions in the YAML (`csv_col_index`)

### 3. Force Regeneration of the YAML

```bash
python -m admin_procedures.prepare_dataset my-dataset --csv source-data/my_data.csv --force-scaffold
```

With `--force-scaffold`, the existing `dataset.yaml` is overwritten and regenerated.

---

## Directory Layout

```
datasets/
  my-dataset/
    dataset.yaml              # dataset definition (required)
    data.parquet              # Parquet data file
```

- Subdirectories directly under `datasets/` that contain a `dataset.yaml` are detected automatically
- The path of the Parquet file is given by `data_file` (relative to dataset.yaml)
