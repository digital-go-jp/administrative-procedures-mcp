"""preview.py (MCP Apps プレビューホスト) のテスト。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin_procedures.preview import (
    TOOL_TEMPLATES,
    build_call_result,
    call_tool,
    make_server,
)
from admin_procedures.response import DatasetResolveError

DATASET_ID = "procedures-survey-r6"


# ============================================================
# call_tool
# ============================================================


def test_call_tool_list_datasets(registry):
    data = call_tool(registry, "list_datasets", {})
    ids = [d["dataset_id"] for d in data["datasets"]]
    assert DATASET_ID in ids


def test_call_tool_summarize_columnar(registry):
    data = call_tool(registry, "summarize_records", {
        "dataset_id": DATASET_ID,
        "group_by": ["所管府省庁"],
        "metrics": ["count"],
    })
    assert "columns" in data and "rows" in data
    assert data["dataset_id"] == DATASET_ID  # UI のドリルダウンが参照する


def test_call_tool_coerces_json_string_params(registry):
    """UI の callServerTool は where を JSON 文字列で送る (MCP サーバーと同じ coercion)。"""
    data = call_tool(registry, "query_records", {
        "dataset_id": DATASET_ID,
        "where": json.dumps({"所管府省庁": "厚生労働省"}),
        "limit": 10,
    })
    assert data["total"] == 2


def test_call_tool_unknown_tool(registry):
    with pytest.raises(ValueError, match="未知のツール"):
        call_tool(registry, "no_such_tool", {})


def test_call_tool_missing_dataset_id(registry):
    with pytest.raises(ValueError, match="dataset_id"):
        call_tool(registry, "inspect_dataset", {})


def test_call_tool_dataset_not_found(registry):
    with pytest.raises(DatasetResolveError):
        call_tool(registry, "inspect_dataset", {"dataset_id": "nope"})


def test_build_call_result_success(registry):
    result = build_call_result(registry, "list_datasets", {})
    assert result["isError"] is False
    assert result["structuredContent"]["total"] >= 1
    assert result["content"][0]["type"] == "text"
    assert result["_meta"]["ui"]["resourceUri"].endswith("/list_datasets")


def test_build_call_result_tool_error_is_not_protocol_error(registry):
    result = build_call_result(registry, "inspect_dataset", {"dataset_id": "nope"})
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


# ============================================================
# HTTP サーバー
# ============================================================


@pytest.fixture()
def http_server(registry):
    server = make_server(registry, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path) as res:
        return res.status, res.read().decode("utf-8")


def _post_json(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_http_host_page(http_server):
    status, body = _get(http_server, "/")
    assert status == 200
    assert "<title>apcli</title>" in body
    assert "INLINE_BASE_CSS" not in body  # base.css が注入済み


def test_http_host_omits_truncated_history_result(http_server):
    """容量制限した履歴を「0件」の結果 UI として復元しない。"""
    _, body = _get(http_server, "/")
    omitted_branch = body.index("e.summary._truncated")
    iframe_branch = body.index(
        "e.summary && e.ok !== false && TOOL_TEMPLATES[e.tool]",
        omitted_branch,
    )
    assert omitted_branch < iframe_branch
    assert "結果の詳細は履歴容量を抑えるため省略しました" in body


def test_http_host_prunes_orphaned_session_storage(http_server):
    """一覧から外れたセッション本体を localStorage に残さない。"""
    _, body = _get(http_server, "/")
    assert 'var LS_SESSION_PREFIX = "apcli-preview:session:"' in body
    assert "staleKeys.push(key)" in body
    assert "staleKeys.forEach(function(key) { localStorage.removeItem(key); });" in body


def test_http_ui_templates(http_server):
    for name in TOOL_TEMPLATES.values():
        status, body = _get(http_server, f"/ui/{name}")
        assert status == 200
        assert "onData(" in body  # base.js が注入済み


def test_http_ui_unknown_template(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(http_server, "/ui/../secret")
    assert exc.value.code == 404


def test_http_describe(http_server):
    status, body = _get(http_server, "/api/describe")
    names = [t["name"] for t in json.loads(body)["tools"]]
    assert sorted(names) == sorted(TOOL_TEMPLATES)


def test_http_call_success(http_server):
    status, data = _post_json(http_server, "/api/call", {
        "name": "summarize_records",
        "arguments": {"dataset_id": DATASET_ID, "group_by": ["所管府省庁"]},
    })
    assert status == 200
    assert data["isError"] is False
    assert "columns" in data["structuredContent"]


def test_http_call_unknown_tool(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(http_server, "/api/call", {"name": "nope", "arguments": {}})
    assert exc.value.code == 400


def test_http_call_bad_json(http_server):
    req = urllib.request.Request(
        http_server + "/api/call",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400


def test_http_version_etag(http_server):
    status, body = _get(http_server, "/api/version")
    assert status == 200
    etag = json.loads(body)["etag"]
    assert len(etag) == 40  # sha1 hex
    # 同一内容なら安定している (watch モードの誤発火防止)
    _, body2 = _get(http_server, "/api/version")
    assert json.loads(body2)["etag"] == etag


def test_http_turn_log_appends(http_server, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PROCEDURES_EVAL_DIR", str(tmp_path))
    payload = {"q": "テスト質問", "calls": [{"tool": "summarize_records",
                                            "query": "集計軸: 所管府省庁"}], "answer": "回答"}
    status, data = _post_json(http_server, "/api/turn-log", payload)
    assert status == 200 and data["ok"] is True
    _post_json(http_server, "/api/turn-log", payload)
    lines = (tmp_path / "results" / "turns.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["q"] == "テスト質問"
    assert "at" in rec  # タイムスタンプが付与される


def test_http_unknown_path(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(http_server, "/api/unknown")
    assert exc.value.code == 404


# ============================================================
# リクエスト検証 (DNS リバインディング / CSRF 対策)
# ============================================================


def _raw_request(base, method, path, headers, body=None):
    """任意の Host ヘッダーを送るため urllib ではなく http.client を使う。"""
    import http.client

    host, port = base[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        return res.status, res.read().decode("utf-8"), \
            {k.lower(): v for k, v in res.getheaders()}
    finally:
        conn.close()


def test_host_header_allowed():
    from admin_procedures.preview import host_header_allowed

    assert host_header_allowed("127.0.0.1:8765", "127.0.0.1")
    assert host_header_allowed("localhost:8765", "127.0.0.1")
    assert host_header_allowed("Localhost", "127.0.0.1")
    assert host_header_allowed("[::1]:8765", "127.0.0.1")
    # DNS リバインディング: 攻撃者ドメインが 127.0.0.1 に解決されても Host で弾く
    assert not host_header_allowed("attacker.example:8765", "127.0.0.1")
    assert not host_header_allowed("", "127.0.0.1")
    assert not host_header_allowed(None, "127.0.0.1")
    # 明示バインドしたホスト名は許可
    assert host_header_allowed("192.168.1.10:8765", "192.168.1.10")
    # ワイルドカードバインドは正当なホスト名を列挙できないため照合しない
    assert host_header_allowed("anything.example", "0.0.0.0")


def test_http_rejects_foreign_host_get(http_server):
    status, _, _ = _raw_request(http_server, "GET", "/", {"Host": "attacker.example"})
    assert status == 403


def test_http_rejects_foreign_host_post(http_server):
    status, _, _ = _raw_request(
        http_server, "POST", "/api/call",
        {"Host": "attacker.example", "Content-Type": "application/json"},
        body=json.dumps({"name": "list_datasets"}),
    )
    assert status == 403


def test_http_rejects_form_content_type(http_server):
    """HTML form が送れる Content-Type (CSRF 経路) は 415 で拒否する。"""
    status, _, _ = _raw_request(
        http_server, "POST", "/api/call",
        {"Host": "127.0.0.1", "Content-Type": "text/plain"},
        body=json.dumps({"name": "list_datasets"}),
    )
    assert status == 415


def test_http_security_headers(http_server):
    _, _, headers = _raw_request(http_server, "GET", "/", {"Host": "127.0.0.1"})
    assert headers.get("x-content-type-options") == "nosniff"
    assert "connect-src 'self'" in headers.get("content-security-policy", "")
    # JSON API にも nosniff は付く (CSP は HTML 応答のみ)
    _, _, api_headers = _raw_request(
        http_server, "GET", "/api/describe", {"Host": "127.0.0.1"})
    assert api_headers.get("x-content-type-options") == "nosniff"


def test_http_host_has_no_legacy_prompt_api_shims(http_server):
    """Chrome 138 以前の API 形 (window.ai / capabilities / systemPrompt) と、
    自分自身を再帰呼び出しする PreviewSession ラッパーを残さない。"""
    _, body = _get(http_server, "/")
    for legacy in (
        "PreviewSession",
        "window.ai",
        "capabilities()",
        "systemPrompt: systemPrompt",
        "tokensSoFar",
        "initWithoutInspect",
        "extractToolCallFromResponse",
    ):
        assert legacy not in body, legacy


def test_http_host_uses_current_prompt_api_names(http_server):
    """Prompt API の現行名 (contextUsage / contextWindow / contextoverflow / samplingMode) を使う。
    旧名 (inputUsage / quotaoverflow) は後方互換としてのみ残す。"""
    _, body = _get(http_server, "/")
    assert "contextUsage" in body and "contextWindow" in body
    assert '"contextoverflow"' in body and '"quotaoverflow"' in body
    assert 'samplingMode: "most-predictable"' in body
    fresh = body.split("function freshSession(")[1].split("\n}")[0]
    assert "watchContextOverflow(s)" in fresh


def test_http_host_reports_prompt_errors_once(http_server):
    """モデル呼び出しの失敗は呼び出し側 (agentTurn 等) が一度だけ表示する。
    promptWithTimeout 内で表示すると、フォールバックで処理される失敗まで画面に出る。"""
    _, body = _get(http_server, "/")
    fn = body.split("function promptWithTimeout(")[1].split("\n}")[0]
    assert 'addMsg("error"' not in fn


def test_http_host_tile_selection_owns_busy_state(http_server):
    """タイル選択の送信可否は setBusy() でハンドラが最後まで管理し、実行中の選択は受け付けない
    (途中で送信が開くと、事前作成と質問のターンが同じセッションを取り合う)。"""
    _, body = _get(http_server, "/")
    fn = body.split("function inspectAndStartSession(")[1].split("\n}")[0]
    assert "setBusy(" not in fn and "busy = " not in fn
    handler = body.split('msg.type === "dataset-selected"')[1].split("return;\n  }")[0]
    assert "sendBtn.disabled" not in handler
    assert "if (busy) {" in handler
    assert handler.index("setBusy(true);") < handler.index("inspectAndStartSession(")
    assert "setBusy(false);" in handler.split("inspectAndStartSession(")[1]


def test_http_host_setup_panel_shows_diagnosis(http_server):
    """Prompt API が使えないとき、LanguageModel が無いのか availability() が unavailable なのかを
    案内の先頭に出す。Edge の手順は現行ドキュメント (Canary/Dev、on-device-internals) に合わせる。"""
    _, body = _get(http_server, "/")
    assert "function showSetupPanel(reason)" in body
    assert 'showSetupPanel("nolm")' in body and 'showSetupPanel("unavailable")' in body
    panel = body.split("function showSetupPanel(reason)")[1].split("\n}")[0]
    assert "setup-diag" in panel
    assert "edge://on-device-internals" in panel
    assert "Edge 131" not in panel


def test_http_host_prompt_recreates_missing_session(http_server):
    """ターン中にセッションが reset されていても、作り直してから呼ぶ (エラーで終えない)。"""
    _, body = _get(http_server, "/")
    fn = body.split("function promptWithTimeout(")[1].split("\n}")[0]
    assert "return ensureSession().then(" in fn.split("var limit")[0]


def test_http_host_shows_progress_for_every_wait(http_server):
    """待ち時間はすべて進行ステップの行に出す: 行はステップ開始時に自動生成し (ensureTurnSteps)、
    ターン終了で閉じる (endTurnSteps)。実行中ステップには経過秒を付け、タイマーを残さない。"""
    _, body = _get(http_server, "/")
    assert "function ensureTurnSteps()" in body
    assert "function endTurnSteps(" in body
    assert "beginTurnSteps" not in body
    step = body.split("function stepStart(")[1].split("\n}")[0]
    assert "var host = ensureTurnSteps();" in step
    assert "setInterval" in step and "clearInterval" in step and "秒" in step
    end = body.split("function endTurnSteps(")[1].split("\n}")[0]
    assert "host.timers.forEach(clearInterval)" in end


def test_http_host_session_creation_is_visible_and_bounded(http_server):
    """create() は「セッションを準備中」のステップとして見せ、watchdog (?ctimeout=) で打ち切る。
    打ち切り後は次の create 試行に進まない。"""
    _, body = _get(http_server, "/")
    ensure = body.split("function ensureSession()")[1].split("\n}")[0]
    assert 'stepStart("内蔵 AI のセッションを準備中")' in ensure
    assert "withWatchdog(" in ensure and "CREATE_TIMEOUT_MS" in ensure
    assert "var CREATE_TIMEOUT_MS" in body and 'get("ctimeout")' in body
    assert "function createSession(onProgress, signal)" in body
    create = body.split("function createSession(onProgress, signal)")[1].split("\n}")[0]
    assert "if (signal && signal.aborted) throw err;" in create


def test_http_host_streams_partial_output(http_server):
    """promptStreaming で生成中の出力を進行ステップに流す (?stream=0 で prompt() に戻せる)。
    途中で失敗した場合も蓄積分 (err.partial) を保持する。"""
    _, body = _get(http_server, "/")
    assert "function streamPrompt(" in body
    assert 'typeof s.promptStreaming !== "function"' in body
    assert 'cfgParam("stream") !== "0"' in body
    pwt = body.split("function promptWithTimeout(")[1].split("\n}")[0]
    assert "streamPrompt(" in pwt and "e.partial" in pwt
    assert "モデル応答がタイムアウトしました" in pwt  # lmPrompt の stall 判定が文言を見る


def test_http_host_cancel_button_aborts_turn(http_server):
    """「中止」ボタンは実行中の create()/prompt() を共有 AbortController で打ち切る。
    ターン外 (タイル選択後の事前作成、eval のケース上限) からも同じ仕組みを使う。"""
    _, body = _get(http_server, "/")
    assert '<button type="button" id="cancel" hidden>中止</button>' in body
    assert "function abortTurn(" in body and "function turnCancelled(" in body
    busy = body.split("function setBusy(b)")[1].split("\n}")[0]
    assert "cancelBtn.hidden = !b" in busy
    watchdog = body.split("function withWatchdog(")[1].split("\n}")[0]
    assert 'removeEventListener("abort"' in watchdog
    selected = body.split('msg.type === "dataset-selected"')[1].split("return;\n  }")[0]
    assert "ensureSession()" in selected and "newTurnController()" in selected
    evals = body.split("function evalCases(")[1]
    assert "newTurnController()" in evals and "abortTurn()" in evals


def test_http_host_explains_model_crash_errors(http_server):
    """モデルプロセスの連続クラッシュ ("crashed too many times") は、復旧手順 (crash count の Reset と再起動) を
    添えた文言で表示する。unavailable の診断にも同じ手掛かりを出す。"""
    _, body = _get(http_server, "/")
    fn = body.split("function humanizeError(")[1].split("\n}")[0]
    assert '"crashed too many times"' in fn and "Reset" in fn
    panel = body.split("function showSetupPanel(reason)")[1].split("\n}")[0]
    assert "Model crash count" in panel


def test_http_host_empty_response_is_not_repaired(http_server):
    """空応答 (モデルプロセスの停止で起きる) には出力し直しを頼まず、その場で止める。
    ?schema=0 で構造化出力 (responseConstraint) を切って切り分けできる。"""
    _, body = _get(http_server, "/")
    turn = body.split("function _agentTurnRun(")[1]
    empty = turn.split('.trim() === "")')[1].split("\n      }")[0]
    assert "応答が得られませんでした" in empty and "return;" in empty
    assert turn.index('.trim() === "")') < turn.index("壊れた JSON は一度だけ修復を促す")
    pwt = body.split("function promptWithTimeout(")[1].split("\n}")[0]
    assert 'cfgParam("schema") === "0"' in pwt and "delete merged.responseConstraint" in pwt
