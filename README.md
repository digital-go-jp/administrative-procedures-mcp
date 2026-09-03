# 行政手続データ分析 MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.2%2B-green.svg)](https://gofastmcp.com)

デジタル庁が公表している行政手続等の棚卸調査結果（約75,000件）を分析できる MCP サーバーです。Claude Desktop や ChatGPT 等の MCP 対応チャットから接続することで、自然言語でデータの検索・集計が行えます。MCP Apps 対応クライアントではチャット内にグラフや表を直接表示することもできます。

また、チャットや LLM を使わずにコマンドラインから同じデータにアクセスできる専用 CLI（`apcli`）も用意しています。

本実装は技術検証を目的としたサンプルコードです（[免責事項](#免責事項)を参照）。



## 特徴

- **データ定義（dataset.yaml）による意味の明示** — フィールドの役割・コードリスト・注意事項をサーバー側で定義し、AI が誤った補完・推測をしにくい構造にしています
- **サーバー側で集計を完結** — group-by・メトリクス・computed measures の計算をサーバー側で行い、AI に生データを渡して計算させることによる誤りを防ぎます
- **出典・品質情報の付与** — ツール応答に出典（provenance）・フィールド注意事項（notes）・充填率（quality_summary）を付与し、回答の根拠を示せるようにしています
- **MCP Apps 対応** — チャット UI 内にグラフや表を直接表示できる対話型 UI を搭載しています (ui resource)
- **YAML 追加だけでデータセットを拡張可能** — コード変更なしに新しいデータセットを追加できます

## インストール

```bash
git clone https://github.com/digital-go-jp/administrative-procedures-mcp.git
cd administrative-procedures-mcp
./setup.sh
```

`setup.sh` が依存インストール・データ取得・接続方法の案内までを行います。
取得する調査年度は対話で選べます（`--dataset all|r7|r6`、既定は令和7年度と令和6年度の両方）。
手動で行う場合は次のとおりです。

```bash
uv sync --extra excel              # uv を使う場合（推奨）
apcli fetch procedures-survey-r7   # 令和7年度の調査結果データを配布元から取得
apcli fetch procedures-survey-r6   # 令和6年度分（年度間で比較する場合）
```

### データについて

**調査結果データはリポジトリに同梱していません。** 公表後に修正や更新が入ることがあるためです。
`apcli fetch` がデジタル庁の配布ページから最新版を取得し、Parquet に変換します。

> `apcli fetch` は同梱または内容を確認済みの `dataset.yaml` に対してのみ実行してください。

取得日は `.fetch.json` に記録され、ツール応答の `provenance.fetched_at` に出力されます
（データの基準時点を表す `as_of_date` とは別項目です）。

配布ページの構成変更などで自動取得に失敗した場合は、ページからファイルを保存して
次のコマンドで取り込めます。

```bash
apcli add procedures-survey-r6 --csv <保存したファイル>
```

### MCP 仕様バージョン

既定のインストールは MCP 仕様 `2025-11-25` で動作します。最新の `2026-07-28`（stateless core・レスポンスキャッシュ対応）で動かす場合は FastMCP 4 系を追加で以下のような形で入れてください。

```bash
pip install --pre "fastmcp>=4.0.0b1"
```

サーバー実装は現時点で両系列に対応しており、実際に話している版は `/health` の `mcp_protocol_version` で確認できます。FastMCP 4 系は執筆時点でプレリリースのため、既定の依存は 3 系のままにしています。

**MCP 2026-07-28 対応で行った実装の詳細は [docs/development.md](docs/development.md#2026-07-28-対応で行った実装) を参照してください。**

## 設定

### Claude Code

リポジトリに `.mcp.json` を同梱しています。**追加設定は不要です。**
クローンしたディレクトリで Claude Code を起動すると接続されます。

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` に追加：

```json
{
  "mcpServers": {
    "admin-procedures": {
      "command": "/path/to/.venv/bin/fastmcp",
      "args": ["run", "-m", "admin_procedures"]
    }
  }
}
```

> `command` には仮想環境内の `fastmcp` のフルパスを指定してください。
> MCP Apps UI を無効化する場合は `"env": { "MCP_NO_UI": "1" }` を追加。
> リポジトリ外から起動する場合は `"env": { "ADMIN_PROCEDURES_DATA_DIR": "/path/to/repo" }` で `datasets/` の位置を指定してください。

設定ファイルを手で編集する代わりに、次のコマンドで登録できます。

```bash
apcli install desktop     # Claude Desktop
apcli install json        # 設定内容を表示するだけ（ファイルは変更しない）
```

### ChatGPT

MCP サーバーを HTTP モードで起動し、HTTPS でアクセスできる環境に配置してから、Settings > Connectors > Create でサーバーURL（`https://<your-domain>/mcp`）を登録し、チャットで有効化してください。詳細は[実行方法](#実行方法)の MCP サーバー（HTTP モード）を参照してください。

## 実行方法

### CLI（`apcli`）

LLM 不要で動作する軽量コマンドラインツール。MCP サーバー不要でレジストリをインプロセス構築し、直接データにアクセスします。

```bash
apcli list                                    # データセット一覧
apcli list -q 棚卸                             # キーワードで絞り込み
apcli inspect procedures-survey-r6            # 構造・品質検査
apcli query procedures-survey-r6 -q 相続 --limit 5  # データ検索
apcli summarize procedures-survey-r6 -g 所管府省庁 -m count  # 集計（短縮形）
apcli summarize procedures-survey-r6 \
  --group-by '["所管府省庁"]' --metrics '["count"]'  # 集計（JSON 配列互換形式）
apcli describe                                # 全ツール定義を表示（エージェント向け）
apcli describe query_records                  # 特定ツール定義を表示
apcli --quiet query procedures-survey-r6      # stderr への診断メッセージを抑制
```

複数指定オプションは繰り返しフラグ（`-g` 等）または JSON 配列の両方に対応しています：

```bash
apcli summarize procedures-survey-r6 -g 所管府省庁 -g 手続類型 -m count  # クロス集計（推奨）
apcli query procedures-survey-r6 -w '{"所管府省庁":["厚生労働省"]}' -s 手続名  # フィルタ・選択
```

`datasets/` の位置は環境変数 `ADMIN_PROCEDURES_DATA_DIR` で上書きできます（未設定時はリポジトリルートを自動検出）。

#### HTML 出力

`--html` フラグまたは `-o` オプションで自己完結型 HTML を出力できます。

```bash
apcli inspect procedures-survey-r6 --html           # HTML を stdout に出力
apcli inspect procedures-survey-r6 -o report.html   # ファイルに保存
apcli summarize procedures-survey-r6 -g 所管府省庁 -m count -o result.html
```

#### プレビュー

`apcli preview` で MCP Apps UI をローカルに起動し、Chrome の内蔵 AI（Prompt API / Gemini Nano）でアプリケーションの動作確認ができます。

```bash
apcli preview            # ブラウザが開く (既定: http://127.0.0.1:8765/)
apcli preview --port 9000 --no-open
```

詳細は下記を参照してください：
- **ブラウザ内蔵 AI の制限** — 単純なクエリは動作しますが、複数条件の組み合わせや複雑なフィルタは期待と異なることがあります。本格的な分析には Claude など フル機能の LLM をお使いください。
- **プラットフォーム限定** — Prompt API は Chrome（138+）と Microsoft Edge Canary/Dev（138.0.3309.2+）で利用可能です。ただし起動時のデータセット探索までは任意のブラウザで利用可能です。

### MCP サーバー（HTTP モード）

外部クライアント（ChatGPT など）から接続する場合、HTTP transport モードでサーバーを起動し、HTTPS でアクセスできる環境に配置します。

```bash
fastmcp run -m admin_procedures --transport streamable-http --port 8000
```

または環境変数で指定：

```bash
ADMIN_PROCEDURES_PORT=8000 python -m admin_procedures
```

> **既定では `127.0.0.1` のみにバインドされ、外部からは到達できません。** 同一ホスト上のリバースプロキシ（nginx 等）で HTTPS 終端してこのポートへ転送する構成であれば、このままで問題ありません。
>
> コンテナ等でプロセス自体を外部公開したい場合は、明示的にバインド先を指定してください:
> - `fastmcp run` 経由: `--host 0.0.0.0` を追加
> - `python -m admin_procedures` 経由: 環境変数 `ADMIN_PROCEDURES_HOST=0.0.0.0`（または `ADMIN_PROCEDURES_PUBLIC=1`）を設定
>
> 外部公開する場合は認証・レート制限を備えたリバースプロキシを前段に配置してください（詳細は[利用に際しての注意事項](#利用に際しての注意事項)を参照）。

クライアント側でサーバーURL（`https://<your-domain>/mcp`）を登録してください。

## ツール

| ツール | 説明 |
|-------|------|
| `list_datasets` | 利用可能なデータセットの一覧を返す |
| `inspect_dataset` | データセットの構造・品質概要を取得 |
| `query_records` | フィルタ・全文検索・ソート・ページネーション付きデータ取得 |
| `summarize_records` | グループ別集計（count/sum/avg/min/max）をサーバー側で計算 |

ツール応答には出典情報（`provenance`）・フィールド注意事項（`notes`）・品質サマリ（`quality_summary`）が付与され、AI によるデータの誤読や不適切な集計を抑えることを狙っています。入力フィールド名を自動補正（表記揺れの正規化・類似名の近似一致）した場合は、補正内容を `resolved_fields` として応答に明示します。

<details>
<summary>パラメータ詳細</summary>

#### `inspect_dataset`

- `dataset_id` (str): データセットID

#### `query_records`

- `dataset_id` (str): データセットID
- `where` (dict, optional): フィルタ条件（文字列=部分一致、配列=IN、`$gte`/`$lte`=範囲、`$ne`=不等、`$not_contains`=部分不一致、`$not_empty`=非空。値は `null` 固定で `{"$not_empty": null}` と書く）
- `q` (str, optional): 全文検索キーワード
- `search_fields` (list, optional): 全文検索スコープの限定
- `select` (list, optional): 取得フィールド
- `order_by` (str, optional): ソート（`-` プレフィックスで降順）
- `limit` (int, optional): 取得件数（デフォルト 50、上限 5,000）
- `cursor` (str, optional): ページネーションカーソル

#### `summarize_records`

- `dataset_id` (str): データセットID
- `group_by` (list, optional): グループ化フィールド
- `metrics` (list): 集計メトリクス（例: `["count", "sum:総手続件数", "avg:オンライン率"]`）
- `where` (dict, optional): フィルタ条件
- `explode` (str, optional): セミコロン区切りフィールドの展開
- `having` (dict, optional): 集計後フィルタ（例: `{"count": {"$gte": 10}}`）
- `limit` (int, optional): 最大グループ数（デフォルト 200、上限 10,000）

</details>

## 利用例

MCP サーバーに接続した Claude Desktop や ChatGPT 等のチャットから、以下のようなプロンプトを入力できます。
（`apcli preview` のブラウザ内蔵 AI は単純なクエリ向けです。複雑な要件は CLI または Claude など フル機能の LLM をお使いください）

```text
所管府省庁ごとの手続件数ランキング（上位）を教えてください。
先にデータセット構造を確認した上で、出典と品質情報も含めてください。
```

```text
オンライン未実施の手続にはどんな傾向があるか知りたい。
まず全体を集計して、そのあと具体例を数件見せて。
```

```text
優先的に見直し候補になりそうな行政手続を探してください。
申請等の手続に絞り、オンライン未実施のものから改善候補を抽出してください。
事実と提案は分けて書いてください。
```

```text
令和6年度と令和7年度のデータセットで、所管府省庁ごとのオンライン率を比較してください。
年度ごとに集計した結果を並べ、変化の大きい府省庁を示してください。
年度間で手続の集合や調査項目の定義が異なる点は注記してください。
```

## データセット

サンプルとして、令和6年度および令和7年度の行政手続等の棚卸調査結果のデータセット定義（dataset.yaml）を同梱しています。データ本体はリポジトリに含まれず、`apcli fetch` が[配布ページ](https://www.digital.go.jp/resources/procedures-survey-results)から取得して Parquet に変換します（[データについて](#データについて)を参照）。

| データセット ID | タイトル | 提供元 |
|----------------|---------|--------|
| `procedures-survey-r7` | 行政手続等の棚卸調査結果（令和7年度、令和7年11月1日時点） | デジタル庁 |
| `procedures-survey-r6` | 行政手続等の棚卸調査結果（令和6年度悉皆調査、令和6年3月31日時点） | デジタル庁 |

年度間で列構成とコード値が一部異なります（令和7年度には「法令番号」の項目がなく、オンライン化の実施状況の「5 一部実施済」区分もありません。補足の自由記述項目が追加されています）。比較する際は各データセットの `inspect_dataset` で定義を確認してください。

データセットの追加方法は [docs/development.md](docs/development.md) を、dataset.yaml の記述方法は [docs/dataset-yaml-guide.md](docs/dataset-yaml-guide.md) を参照してください（補完・検証用の JSON Schema を [datasets/dataset-v1.schema.json](datasets/dataset-v1.schema.json) に同梱しています）。

AI エージェント向けに、リポジトリのドキュメント一覧を [llms.txt](llms.txt) として公開しています。

## 利用に際しての注意事項

このリポジトリは、公開データを使って MCP による検索・集計と MCP Apps の表示を試すための、ローカルまたは単一利用者向けの実験用サンプルです。複数利用者を収容する本番サービスや、基盤としての運用は対象としていません。

- CLI、stdio、`apcli preview` はローカルでの試用を基本とします。プレビューは既定で `127.0.0.1` のみにバインドされます。
- **本番環境に展開する場合**：HTTP transport 自体には利用者認証、認可、レート制限、監査ログなどを実装していません。外部から到達可能にする場合は、認証と通信制限を備えたリバースプロキシ等を前段に配置してください。
- `dataset.yaml` は信頼済みの設定ファイルとして扱います。出所不明の YAML に対して `apcli fetch` や `apcli add` を実行しないでください。

実装上の信頼境界と公開時の考慮事項は、[開発ガイド](docs/development.md#11-適用範囲と信頼境界)を参照してください。

## 開発

詳細は [docs/development.md](docs/development.md) を参照してください。

## ライセンス

[MIT License](LICENSE)

## 免責事項

本実装は技術検証を目的としたサンプルコードです。

- 動作の安定性や継続的な保守を保証するものではありません
- 搭載データの正確性や最新性を保証するものではありません
- 本実装の出力は政府の公式見解ではありません
- データは各府省庁が公表した調査時点の情報であり、現在の状況と異なる場合があります
- データの利用に際しては、[原典資料](https://www.digital.go.jp/resources/procedures-survey-results)を併せてご確認ください
