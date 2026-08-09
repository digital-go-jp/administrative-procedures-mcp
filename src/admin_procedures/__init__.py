"""admin_procedures — 行政手続カタログ MCP Server。

SDMX/DSD を参考にした 4 Tools で行政手続データを提供する。

Modules:
    models   -- DatasetRegistry, DSD モデル, フィールド定義
    loader   -- YAML パーサ, データセット自動登録
    query    -- フィルタ・集計エンジン, ページネーション
    response -- MCP 非依存レスポンスビルダー・パイプライン・ツール定義
    server   -- MCP ツール登録 + サーバー起動 (FastMCP)
    cli      -- apcli CLI (インプロセス実行)
"""

__version__ = "0.2.0"
