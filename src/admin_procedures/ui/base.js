/* base.js — MCP Apps 共通ユーティリティ (軽量 postMessage 実装) */

/* ── Columnar → Records 復元 ── */
/**
 * columnar 形式 { columns, rows } を list[dict] に復元する。
 * UI テンプレート内部で dict ベースのアクセスを維持するために使用。
 */
function columnarToRecords(data) {
  var columns = data.columns || [];
  var rows = data.rows || [];
  return rows.map(function(row) {
    var obj = {};
    columns.forEach(function(col, i) {
      if (row[i] != null) obj[col] = row[i];
    });
    return obj;
  });
}

/* ── Apache ECharts (インライン注入済み) ── */
var echarts = window.echarts;


/* ── MCP Apps 軽量クライアント ── */
/*
 * @modelcontextprotocol/ext-apps SDK 互換の軽量実装。
 * CDN からの動的 import() は Claude Desktop の CSP でブロックされるため、
 * 必要最小限の JSON-RPC over postMessage を直接実装する。
 *
 * プロトコル:
 *   connect()       → ui/initialize リクエスト送信 → ui/notifications/initialized 通知
 *   ontoolresult    → ui/notifications/tool-result 通知を受信
 *   callServerTool  → tools/call リクエスト送信 → レスポンス受信
 */
var _McpApp = (function() {
  var _nextId = 1;
  var _pending = {};  // id → { resolve, reject }
  var _handlers = {}; // method → callback

  function App(info) {
    this._info = info || {};
    this._connected = false;
  }

  App.prototype.connect = function() {
    var self = this;
    return new Promise(function(resolve, reject) {
      // postMessage リスナー登録
      window.addEventListener("message", function(event) {
        // 送信元がホスト (親フレーム) であることを確認する。
        // 仕様上ホストのオリジンは事前に分からないため origin では検証できないが、
        // source を照合すれば他フレームからの偽装メッセージは弾ける。
        if (event.source !== window.parent) return;
        var msg = event.data;
        if (!msg || msg.jsonrpc !== "2.0") return;

        if (msg.id != null && _pending[msg.id]) {
          // レスポンス (result or error)
          var p = _pending[msg.id];
          delete _pending[msg.id];
          if (msg.error) {
            p.reject(new Error(msg.error.message || "RPC error"));
          } else {
            p.resolve(msg.result);
          }
        } else if (msg.method && !msg.id) {
          // 通知 (notification)
          var handler = _handlers[msg.method];
          if (handler) handler(msg.params);
        }
      });

      // ui/initialize リクエスト送信
      var initId = _nextId++;
      _pending[initId] = {
        resolve: function(result) {
          self._connected = true;
          // ui/notifications/initialized 通知送信
          window.parent.postMessage({
            jsonrpc: "2.0",
            method: "ui/notifications/initialized",
          }, "*");
          // サイズ通知
          self._reportSize();
          resolve(self);
        },
        reject: reject,
      };
      window.parent.postMessage({
        jsonrpc: "2.0",
        id: initId,
        method: "ui/initialize",
        params: {
          protocolVersion: "2026-01-26",
          appInfo: {
            name: self._info.name || "administrative-procedures-mcp",
            version: self._info.version || "1.0.0",
          },
          appCapabilities: {},
        },
      }, "*");
    });
  };

  App.prototype._reportSize = function() {
    /* documentElement.scrollHeight は iframe の表示高さ以上を常に返すため、
     * ホストがその値で iframe を伸ばすと互いに成長し続ける (ラチェット)。
     * ビューポート高さに依存しない body の実寸 (上端オフセット込み) で報告する。 */
    var rect = document.body.getBoundingClientRect();
    var w = document.documentElement.scrollWidth;
    var h = Math.ceil(rect.bottom);
    window.parent.postMessage({
      jsonrpc: "2.0",
      method: "ui/notifications/size-changed",
      params: { width: w, height: h },
    }, "*");
  };

  App.prototype._sendRequest = function(method, params) {
    var id = _nextId++;
    return new Promise(function(resolve, reject) {
      _pending[id] = { resolve: resolve, reject: reject };
      window.parent.postMessage({
        jsonrpc: "2.0",
        id: id,
        method: method,
        params: params,
      }, "*");
    });
  };

  App.prototype.callServerTool = function(params) {
    return this._sendRequest("tools/call", params);
  };

  // ontoolresult セッター
  Object.defineProperty(App.prototype, "ontoolresult", {
    set: function(callback) {
      _handlers["ui/notifications/tool-result"] = callback;
    },
  });

  // ontoolinput セッター
  Object.defineProperty(App.prototype, "ontoolinput", {
    set: function(callback) {
      _handlers["ui/notifications/tool-input"] = callback;
    },
  });

  return App;
})();

/* ── データ受信 & App 管理 ── */
var _onData = null;
var _app = null;
var _appReady = false;

/**
 * ホストからの structuredContent を受信するコールバックを登録し、
 * App を自動初期化する。
 *
 * スタンドアロンモード: window.__STANDALONE_DATA__ が存在する場合、
 * MCP Apps 接続をスキップし埋め込みデータを直接コールバックに渡す。
 * CLI の --html 出力で使用。
 *
 * @param {function} callback - structuredContent dict を受け取る関数
 */
function onData(callback) {
  _onData = callback;
  // スタンドアロンモード: 埋め込みデータを直接使用
  if (window.__STANDALONE_DATA__) {
    callback(window.__STANDALONE_DATA__);
    return;
  }
  if (!_appReady) {
    initApp();
  }
}

/**
 * MCP Apps クライアントを初期化する。
 * 通常は onData() が自動で呼ぶ。明示的に呼んでもよい（二重初期化は防止）。
 * @param {Object} [opts] - { name: string }
 * @returns {Promise<App>} 初期化済み App インスタンス
 */
function initApp(opts) {
  if (_appReady) {
    return Promise.resolve(_app);
  }
  _appReady = true;
  opts = opts || {};
  _app = new _McpApp({
    name: opts.name || "administrative-procedures-mcp",
    version: "1.0.0",
  });
  _app.ontoolresult = function(result) {
    var data = result.structuredContent;
    // structuredContent が無い場合、content 内の JSON テキストをパースして復元
    if (!data && result.content && Array.isArray(result.content)) {
      for (var i = 0; i < result.content.length; i++) {
        if (result.content[i].type === "text" && result.content[i].text) {
          try { data = JSON.parse(result.content[i].text); } catch(e) {}
          if (data) break;
        }
      }
    }
    if (!data) data = result;
    if (_onData && data && typeof data === "object") {
      _onData(data);
      // コンテンツ描画後に iframe サイズを再報告
      setTimeout(function() { _app._reportSize(); }, 100);
    }
  };
  /* チャート展開など後からの拡大縮小も自動で追随する */
  if (typeof ResizeObserver !== "undefined" && document.body) {
    var _roScheduled = false;
    new ResizeObserver(function() {
      if (_roScheduled) return;
      _roScheduled = true;
      requestAnimationFrame(function() { _roScheduled = false; _app._reportSize(); });
    }).observe(document.body);
  }
  var connectPromise = _app.connect();
  var timeout = setTimeout(function() {
    console.warn("MCP Apps: connect timeout (5s) — UI may not receive data");
  }, 5000);
  connectPromise.then(function() { clearTimeout(timeout); });
  return connectPromise;
}

/* ── 数値フォーマッタ ── */
const _numFmt = new Intl.NumberFormat("ja-JP");
const _pctFmt = new Intl.NumberFormat("ja-JP", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

function fmtNum(n) {
  if (n == null) return "—";
  return _numFmt.format(n);
}

function fmtPct(n) {
  if (n == null) return "—";
  return _pctFmt.format(n);
}

function fmtFloat(n, digits) {
  if (n == null) return "—";
  return new Intl.NumberFormat("ja-JP", {
    minimumFractionDigits: digits || 2,
    maximumFractionDigits: digits || 2,
  }).format(n);
}

/* ── HTML エスケープ ── */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/* ── DSD ロール名 → 日本語ラベル (SDMX 準拠) ── */
var _ROLE_LABELS = { dimension: "分析軸", measure: "数値項目", attribute: "属性", identifier: "識別子" };
function roleLabel(role) { return _ROLE_LABELS[role] || role || ""; }

/* ── テーブルビルダー ── */

/**
 * 配列データからソート可能なテーブルを生成する。
 * @param {HTMLElement} container - テーブルを挿入する要素
 * @param {Array<Object>} rows - データ行
 * @param {Array<string>} columns - カラム名リスト
 * @param {Object} [opts] - オプション
 * @param {Object} [opts.labels] - カラム名 → 表示ラベル
 * @param {Object} [opts.formatters] - カラム名 → フォーマッタ関数
 * @param {Set<string>} [opts.rightAlign] - 右寄せカラム名
 * @param {Object} [opts.fieldMeta] - カラム名 → {role: string}
 */
const TYPE_LABELS = { string: "文字列", integer: "整数", float: "数値" };

/* ヘッダホバーでフィールド定義 (dataset.yaml 由来のロール・型・説明) を表示する。
 * desc は信頼済みでない自由記述のため textContent でのみ描画する (innerHTML 禁止) */
function attachFieldTooltip(th, name, meta) {
  let tip = null;
  function hide() {
    if (tip) { tip.remove(); tip = null; }
  }
  function show() {
    hide();
    tip = document.createElement("div");
    tip.className = "field-tooltip";
    const title = document.createElement("div");
    title.className = "ft-name";
    title.textContent = name;
    tip.appendChild(title);
    const rows = [["ロール", roleLabel(meta.role) || meta.role || "-"]];
    if (meta.type) {
      rows.push(["型", (TYPE_LABELS[meta.type] || meta.type) +
        (meta.multi_value ? "（複数値: 「;」区切り）" : "")]);
    }
    if (meta.desc) rows.push(["説明", meta.desc]);
    rows.forEach(([k, v]) => {
      const r = document.createElement("div");
      r.className = "ft-row";
      const kEl = document.createElement("span");
      kEl.className = "ft-key";
      kEl.textContent = k;
      r.appendChild(kEl);
      r.appendChild(document.createTextNode(v));
      tip.appendChild(r);
    });
    const hint = document.createElement("div");
    hint.className = "ft-hint";
    hint.textContent = "クリックでソート";
    tip.appendChild(hint);
    document.body.appendChild(tip);
    // ヘッダの下に出し、収まらなければ上に。左右は画面内に収める
    const rect = th.getBoundingClientRect();
    tip.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - tip.offsetWidth - 4)) + "px";
    const below = rect.bottom + 6;
    if (below + tip.offsetHeight < window.innerHeight - 4) {
      tip.style.top = below + "px";
    } else {
      tip.style.top = Math.max(4, rect.top - tip.offsetHeight - 6) + "px";
    }
  }
  th.addEventListener("mouseenter", show);
  th.addEventListener("mouseleave", hide);
  // ソートで th ごと再描画されるため、クリック時に消して取り残しを防ぐ
  th.addEventListener("click", hide);
}

function buildTable(container, rows, columns, opts) {
  opts = opts || {};
  const labels = opts.labels || {};
  const formatters = opts.formatters || {};
  const rightAlign = opts.rightAlign || new Set();
  const fieldMeta = opts.fieldMeta || {};
  const showRowNum = !!opts.showRowNum;
  const onRowClick = opts.onRowClick || null;

  const pageSize = opts.pageSize || 50;
  let sortCol = null;
  let sortAsc = true;
  let currentRows = rows.slice();
  let currentPage = 0;

  function totalPages() {
    return Math.max(1, Math.ceil(currentRows.length / pageSize));
  }

  function render() {
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const table = document.createElement("table");

    // thead
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    if (showRowNum) {
      const thNum = document.createElement("th");
      thNum.textContent = "#";
      thNum.setAttribute("scope", "col");
      thNum.classList.add("text-right");
      thNum.style.width = "3em";
      headerRow.appendChild(thNum);
    }
    columns.forEach((col) => {
      const th = document.createElement("th");
      th.setAttribute("scope", "col");
      const meta = fieldMeta[col];
      if (meta) {
        // DSD メタデータがある場合: バッジ + 日本語ラベル
        const badge = document.createElement("span");
        badge.className = "field-badge " + (meta.role || "");
        badge.textContent = roleLabel(meta.role);
        th.appendChild(badge);
        th.appendChild(document.createTextNode(" " + (labels[col] || col)));
      } else {
        th.textContent = labels[col] || col;
      }
      if (rightAlign.has(col)) th.classList.add("text-right");
      const icon = document.createElement("span");
      icon.className = "sort-icon";
      if (sortCol === col) {
        icon.textContent = sortAsc ? "▲" : "▼";
        th.setAttribute("aria-sort", sortAsc ? "ascending" : "descending");
      } else {
        icon.textContent = "⇅";
        th.setAttribute("aria-sort", "none");
      }
      th.appendChild(icon);
      if (meta) {
        attachFieldTooltip(th, labels[col] || col, meta);
      } else {
        th.title = "クリックでソート";
      }
      th.addEventListener("click", () => {
        if (sortCol === col) {
          sortAsc = !sortAsc;
        } else {
          sortCol = col;
          sortAsc = true;
        }
        currentRows.sort((a, b) => {
          const va = a[col], vb = b[col];
          if (va == null && vb == null) return 0;
          if (va == null) return 1;
          if (vb == null) return -1;
          if (typeof va === "number" && typeof vb === "number") {
            return sortAsc ? va - vb : vb - va;
          }
          const sa = String(va), sb = String(vb);
          return sortAsc ? sa.localeCompare(sb, "ja") : sb.localeCompare(sa, "ja");
        });
        currentPage = 0;
        render();
      });
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    // tbody (paginated)
    const tbody = document.createElement("tbody");
    const tp = totalPages();
    if (currentPage >= tp) currentPage = tp - 1;
    const start = currentPage * pageSize;
    const end = Math.min(start + pageSize, currentRows.length);
    for (let i = start; i < end; i++) {
      const row = currentRows[i];
      const tr = document.createElement("tr");
      if (onRowClick) {
        tr.style.cursor = "pointer";
        (function(r, ri) {
          tr.addEventListener("click", () => onRowClick(r, ri));
        })(row, i);
      }
      if (showRowNum) {
        const tdNum = document.createElement("td");
        tdNum.classList.add("text-right", "text-muted");
        tdNum.textContent = String(i + 1);
        tr.appendChild(tdNum);
      }
      columns.forEach((col) => {
        const td = document.createElement("td");
        if (rightAlign.has(col)) td.classList.add("text-right");
        const val = row[col];
        const fmt = formatters[col];
        td.textContent = fmt ? fmt(val) : (val != null ? String(val) : "");
        td.title = val != null ? String(val) : "";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);

    // pagination controls
    if (tp > 1) {
      const pager = document.createElement("div");
      pager.className = "table-pager";
      pager.style.cssText = "display:flex;align-items:center;justify-content:center;gap:8px;margin-top:8px;font-size:0.9rem";

      const btnPrev = document.createElement("button");
      btnPrev.textContent = "← 前";
      btnPrev.disabled = currentPage === 0;
      btnPrev.style.cssText = "padding:4px 12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-alt);cursor:pointer";
      if (btnPrev.disabled) btnPrev.style.opacity = "0.4";
      btnPrev.addEventListener("click", () => { currentPage--; render(); });

      const info = document.createElement("span");
      info.style.color = "var(--text-muted)";
      info.textContent = (currentPage + 1) + " / " + tp + " ページ（" + fmtNum(currentRows.length) + " 件）";

      const btnNext = document.createElement("button");
      btnNext.textContent = "次 →";
      btnNext.disabled = currentPage >= tp - 1;
      btnNext.style.cssText = "padding:4px 12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-alt);cursor:pointer";
      if (btnNext.disabled) btnNext.style.opacity = "0.4";
      btnNext.addEventListener("click", () => { currentPage++; render(); });

      pager.appendChild(btnPrev);
      pager.appendChild(info);
      pager.appendChild(btnNext);
      wrap.appendChild(pager);
    }

    container.innerHTML = "";
    container.appendChild(wrap);
  }

  render();
}

/* ── プログレスバー ── */
function createProgressBar(value, max) {
  max = max || 1;
  const pct = Math.min(100, (value / max) * 100);
  const color = pct >= 80 ? "var(--green)" : pct >= 50 ? "var(--yellow)" : "var(--red)";

  const bar = document.createElement("div");
  bar.className = "progress-bar";
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-valuenow", String(Math.round(pct)));
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", "100");

  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = pct + "%";
  fill.style.background = color;

  const label = document.createElement("span");
  label.className = "progress-label";
  label.textContent = fmtPct(value / max);

  bar.appendChild(fill);
  bar.appendChild(label);
  return bar;
}

/* ── サマリーカード ── */
function createCard(label, value) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML =
    '<div class="label">' + escapeHtml(label) + "</div>" +
    '<div class="value">' + escapeHtml(String(value)) + "</div>";
  return card;
}

/* ── 免責事項 ── */
function createDisclaimer(provenance) {
  var el = document.createElement("div");
  el.className = "disclaimer";
  var lines = [];
  if (provenance) {
    var parts = [];
    if (provenance.dataset_title) parts.push(provenance.dataset_title);
    if (provenance.as_of_date) parts.push(provenance.as_of_date + " 時点");
    if (provenance.published_at) parts.push(provenance.published_at + " 公表");
    if (parts.length > 0) lines.push("出典: " + parts.join(" / "));
    if (provenance.publisher) lines.push("公表者: " + provenance.publisher);
    if (provenance.source_url) lines.push("原典: " + provenance.source_url);
    if (provenance.source_note) lines.push("出典メモ: " + provenance.source_note);
  }
  lines.push(
    "本データは調査時点の回答に基づく集計結果であり、最新の状況とは異なる場合があります。" +
    "データの利用に際しては、原典資料を併せてご確認ください。"
  );
  el.textContent = lines.join("\n");
  el.style.whiteSpace = "pre-line";
  return el;
}

/* ── トグルボタン ── */
function createToggle(options, onChange) {
  const group = document.createElement("div");
  group.className = "toggle-group";
  group.setAttribute("role", "tablist");
  options.forEach((opt, i) => {
    const btn = document.createElement("button");
    btn.className = "toggle-btn" + (i === 0 ? " active" : "");
    btn.textContent = opt.label;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", i === 0 ? "true" : "false");
    btn.addEventListener("click", () => {
      group.querySelectorAll(".toggle-btn").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      onChange(opt.value);
    });
    btn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        btn.click();
      }
    });
    group.appendChild(btn);
  });
  return group;
}

/* ── コラプシブルバー ── */
/**
 * コラプシブル UI を生成して container に追加する。
 * @param {HTMLElement} container - 親要素 (#app)
 * @param {Object} opts
 * @param {string} opts.label - バーの左端ラベル (例: "データ検索")
 * @param {string} opts.toggleLabel - 展開ボタンのラベル (例: "テーブルを表示")
 * @param {Array<{text:string}>} opts.info - バーに表示する情報スパン配列
 * @param {boolean} [opts.startOpen=true] - 生成時に展開した状態で表示するか
 * @param {function} buildFn - function(contentEl) — 初回展開時に呼ばれるコンテンツ構築関数
 */
function createCollapsible(container, opts, buildFn) {
  var bar = document.createElement("div");
  bar.className = "collapsible-bar";
  bar.setAttribute("role", "button");
  bar.setAttribute("tabindex", "0");
  bar.setAttribute("aria-expanded", "false");

  var summary = document.createElement("div");
  summary.className = "collapsible-summary";
  var parts = ['<span class="cb-label">' + escapeHtml(opts.label) + "</span>"];
  (opts.info || []).forEach(function(item) {
    parts.push('<span class="cb-sep">|</span>');
    parts.push("<span>" + escapeHtml(item.text) + "</span>");
  });
  summary.innerHTML = parts.join("");
  bar.appendChild(summary);

  var toggleBtn = document.createElement("button");
  toggleBtn.className = "collapsible-toggle";
  var toggleLabel = opts.toggleLabel || "表示";
  toggleBtn.innerHTML = '<span class="arrow">&#9654;</span> ' + escapeHtml(toggleLabel);
  bar.appendChild(toggleBtn);
  container.appendChild(bar);

  var content = document.createElement("div");
  content.className = "collapsible-content";
  container.appendChild(content);

  var built = false;
  function toggle() {
    var expanding = !content.classList.contains("open");
    bar.classList.toggle("expanded");
    content.classList.toggle("open");
    bar.setAttribute("aria-expanded", expanding ? "true" : "false");
    toggleBtn.innerHTML = expanding
      ? '<span class="arrow">&#9654;</span> 閉じる'
      : '<span class="arrow">&#9654;</span> ' + escapeHtml(toggleLabel);
    if (!built) {
      built = true;
      buildFn(content);
    }
    if (_app) {
      var reported = false;
      function reportOnce() {
        if (reported) return;
        reported = true;
        _app._reportSize();
      }
      content.addEventListener("transitionend", function handler(e) {
        if (e.target !== content) return;
        content.removeEventListener("transitionend", handler);
        reportOnce();
      });
      setTimeout(reportOnce, 400);
    }
  }
  bar.addEventListener("click", toggle);
  bar.addEventListener("keydown", function(e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  /* チャート等は畳まれていると見落とされるため、既定で展開して表示する */
  if (opts.startOpen !== false) toggle();

  return content;
}

/* ── 共通エラー表示 ── */
function showError(container, message) {
  var el = document.createElement("div");
  el.setAttribute("role", "alert");
  el.style.cssText = "padding:12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-alt);color:var(--text-muted);font-size:1rem";
  el.textContent = message;
  container.innerHTML = "";
  container.appendChild(el);
}

/* ── クエリ条件表示 (日本語) — テーブル駆動 ── */
var _QC_OP_JA = { "$gte": "≧", "$lte": "≦", "$ne": "≠", "$eq": "=", "$not_empty": "非空", "$contains": "含む" };
var _QC_AGG_JA = { sum: "合計", avg: "平均", min: "最小", max: "最大" };

function buildQueryConditions(params) {
  if (!params || Object.keys(params).length === 0) return null;

  var items = [];
  var explodeInGroupBy = params.explode && params.group_by &&
    params.group_by.indexOf(params.explode) !== -1;

  /* group_by */
  if (params.group_by && params.group_by.length > 0) {
    var gbLabel = params.group_by.map(function(f) {
      return (params.explode === f) ? f + " (展開)" : f;
    }).join(", ");
    items.push({ text: "集計軸: " + gbLabel, cls: "qc-group" });
  }

  /* metrics */
  if (params.metrics && params.metrics.length > 0) {
    var metricLabels = params.metrics.map(function(mk) {
      if (mk === "count") return "件数";
      var parts = mk.split(":");
      if (parts.length === 2) return (_QC_AGG_JA[parts[0]] || parts[0]) + "(" + parts[1] + ")";
      return mk;
    });
    items.push({ text: "指標: " + metricLabels.join(", "), cls: "qc-metric" });
  }

  /* where / having — 共通フィルタ変換 */
  function formatConditions(obj) {
    return Object.keys(obj).map(function(k) {
      var v = obj[k];
      if (Array.isArray(v)) return k + " = " + v.join(", ");
      if (typeof v === "object" && v !== null) {
        var ops = Object.keys(v).map(function(op) { return (_QC_OP_JA[op] || op) + " " + v[op]; });
        return k + " " + ops.join(", ");
      }
      return k + " ⊃ \"" + v + "\"";
    });
  }

  if (params.where && typeof params.where === "object") {
    items.push({ text: "絞り込み: " + formatConditions(params.where).join(" / "), cls: "qc-filter" });
  }

  if (params.having && typeof params.having === "object") {
    var hConds = [];
    Object.keys(params.having).forEach(function(k) {
      var v = params.having[k];
      if (typeof v === "object" && v !== null) {
        Object.keys(v).forEach(function(op) {
          hConds.push(k + " " + (_QC_OP_JA[op] || op) + " " + v[op]);
        });
      }
    });
    if (hConds.length > 0) items.push({ text: "集計後フィルタ: " + hConds.join(" / "), cls: "qc-filter" });
  }

  /* 残りの単純フィールド — テーブル駆動 */
  var _simpleFields = [
    { key: "explode", cond: function() { return params.explode && !explodeInGroupBy; }, fmt: function(v) { return "展開: " + v; } },
    { key: "q", fmt: function(v) { return "検索: \"" + v + "\""; } },
    { key: "order_by", fmt: function(v) { var desc = v.charAt(0) === "-"; return "ソート: " + (desc ? v.slice(1) : v) + (desc ? " (降順)" : " (昇順)"); } },
    { key: "limit", fmt: function(v) { return "上限: " + v + " 件"; } },
  ];
  _simpleFields.forEach(function(sf) {
    var v = params[sf.key];
    if (v && (!sf.cond || sf.cond())) items.push({ text: sf.fmt(v), cls: "" });
  });

  if (items.length === 0) return null;

  var el = document.createElement("div");
  el.className = "query-conditions";
  el.innerHTML = items.map(function(item) {
    var cls = "qc-item" + (item.cls ? " " + item.cls : "");
    return '<span class="' + cls + '">' + escapeHtml(item.text) + '</span>';
  }).join("");
  return el;
}

/* ── デジタル庁デザインシステム準拠 モノクロマティック青パレット ── */
var COLORS = [
  "#0017C1", "#2B4FD8", "#5A7BEE", "#8EA8F5", "#C5D7FB",
  "#0B3AD4", "#4565E3", "#7494F2", "#A7C0F8", "#DDE8FD",
];

function getColor(i) {
  return COLORS[i % COLORS.length];
}

/* ── CSS カスタムプロパティから計算値を取得（ダークモード自動対応） ── */
var _cs = getComputedStyle(document.documentElement);
var _themeText = _cs.getPropertyValue("--text").trim() || "#08131a";
var _themeMuted = _cs.getPropertyValue("--text-muted").trim() || "#5a656b";
var _themeBorder = _cs.getPropertyValue("--border").trim() || "#c5ccd1";
var _themeBg = _cs.getPropertyValue("--bg").trim() || "#ffffff";

/* ── ECharts デジタル庁デザインシステムテーマ ── */
var _DIGITAL_THEME = {
  color: COLORS,
  backgroundColor: "transparent",
  textStyle: {
    fontFamily: '"Noto Sans JP", "Hiragino Sans", "Hiragino Kaku Gothic ProN", system-ui, -apple-system, sans-serif',
    color: _themeText,
  },
  title: {
    textStyle: { fontWeight: 700, color: _themeText, fontSize: 14 },
  },
  bar: {
    itemStyle: { borderRadius: [2, 2, 0, 0] },
  },
  pie: {
    itemStyle: { borderColor: _themeBg, borderWidth: 2 },
  },
  categoryAxis: {
    axisLine: { lineStyle: { color: _themeBorder } },
    axisTick: { show: false },
    axisLabel: { color: _themeMuted, fontSize: 14 },
    splitLine: { show: false },
  },
  valueAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: _themeMuted, fontSize: 14 },
    splitLine: { lineStyle: { color: _themeBorder, type: "dashed" } },
  },
  legend: {
    textStyle: { color: _themeMuted, fontSize: 14 },
  },
  tooltip: {
    backgroundColor: _themeBg,
    borderColor: _themeBorder,
    borderWidth: 1,
    textStyle: { color: _themeText, fontSize: 14 },
    extraCssText: "box-shadow: 0 2px 8px rgba(0,0,0,0.08); border-radius: 4px;",
  },
};

/* テーマ登録 */
if (typeof echarts !== "undefined") {
  echarts.registerTheme("digital-agency", _DIGITAL_THEME);
}

/* ── ECharts 共通ヘルパー ── */

/**
 * チャート描画コンテナを作成し ECharts インスタンスを初期化する。
 * @returns {{ chart: ECharts, wrap: HTMLElement } | null}
 */
function _initChart(container, height, chartOpts) {
  var wrap = document.createElement("div");
  wrap.className = "chart-container";
  wrap.style.width = "100%";
  wrap.style.minWidth = "0";
  wrap.style.height = height + "px";
  container.appendChild(wrap);

  var chart;
  try {
    chart = echarts.init(wrap, "digital-agency", chartOpts || { renderer: "svg" });
  } catch (e) {
    wrap.textContent = "チャートの描画に失敗しました";
    wrap.style.color = "var(--text-muted)";
    return null;
  }

  window.addEventListener("resize", function() { chart.resize(); });
  return { chart: chart, wrap: wrap };
}

/**
 * チャートにクリックイベントを安全にバインドする。
 */
function _bindClick(chart, handler) {
  chart.on("click", function(params) {
    try { handler(params); } catch (e) { console.error("Chart click error:", e); }
  });
}

/**
 * 積上げ棒・棒チャート共通の tooltip formatter。
 * マーカー + 数値 + パーセント + (積上げ時) 合計行。
 */
function _barTooltipFormatter(params, showTotal) {
  if (!params || params.length === 0) return "";
  var name = escapeHtml(params[0].axisValueLabel || params[0].name || "");
  var lines = '<div style="font-weight:700;margin-bottom:6px;max-width:360px;word-break:break-all;font-size:14px">' + name + "</div>";
  var total = 0;
  params.forEach(function(p) { total += (p.value || 0); });
  params.forEach(function(p) {
    var marker = '<span style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;background:' + p.color + '"></span>';
    var pct = total > 0 ? ' <span style="color:' + _themeMuted + '">(' + fmtPct(p.value / total) + ')</span>' : "";
    lines += '<div style="line-height:1.6">' + marker + escapeHtml(p.seriesName) + ": " + fmtNum(p.value) + pct + "</div>";
  });
  if (showTotal && params.length > 1) {
    lines += '<div style="border-top:1px solid rgba(128,128,128,0.3);margin-top:4px;padding-top:4px;font-weight:700">合計: ' + fmtNum(total) + "</div>";
  }
  return lines;
}

/**
 * 棒チャートの水平/垂直軸設定を生成する。
 */
function _buildBarAxes(labels, opts) {
  var isH = !!opts.horizontal;
  var n = labels.length;
  var catAxis = {
    type: "category", data: labels,
    name: "", nameLocation: "middle",
    nameTextStyle: { fontSize: 14, color: _themeMuted },
    axisLabel: { fontSize: 14, interval: 0, hideOverlap: true },
  };
  var valAxis = {
    type: "value", min: 0,
    name: "", nameLocation: "end",
    nameTextStyle: { fontSize: 14, color: _themeMuted, padding: [0, 0, 8, 0] },
    axisLabel: { formatter: function(v) { return fmtNum(v); }, fontSize: 14 },
  };

  if (isH) {
    catAxis.inverse = true;
    catAxis.name = opts.yAxisName || "";
    catAxis.nameLocation = "end";
    catAxis.nameTextStyle.padding = [0, 0, 8, 0];
    catAxis.axisLabel.formatter = function(v) { return v.length > 20 ? v.slice(0, 19) + "…" : v; };
    valAxis.name = opts.xAxisName || "";
    valAxis.nameLocation = "middle";
    valAxis.nameGap = 32;
    delete valAxis.nameTextStyle.padding;
    valAxis.axisLabel.hideOverlap = true;
    return { xAxis: valAxis, yAxis: catAxis };
  } else {
    catAxis.name = opts.xAxisName || "";
    catAxis.nameGap = n > 6 ? 56 : 36;
    catAxis.axisLabel.rotate = n > 6 ? 30 : 0;
    valAxis.name = opts.yAxisName || "";
    return { xAxis: catAxis, yAxis: valAxis };
  }
}

/* ── ECharts チャート関数 ── */

/**
 * バーチャートを生成する (ECharts — 縦棒・横棒・積上げ対応)。
 * @param {HTMLElement} container
 * @param {string[]} labels - カテゴリラベル
 * @param {Array<{label:string, data:number[], backgroundColor:string}>} datasets
 * @param {Object} [opts]
 * @param {boolean} [opts.horizontal] - 横棒
 * @param {boolean} [opts.stacked] - 積上げ
 * @param {string} [opts.xAxisName]
 * @param {string} [opts.yAxisName]
 * @param {function} [opts.onBarClick]
 */
function createBarChart(container, labels, datasets, opts) {
  opts = opts || {};
  var n = labels.length;
  var dLen = datasets.length;
  if (n === 0 || dLen === 0) return;

  var isH = !!opts.horizontal;
  var isStacked = !!opts.stacked;
  var height = isH ? Math.max(300, Math.min(800, n * 32 + 100)) : Math.max(300, Math.min(500, n * 40 + 120));
  var ec = _initChart(container, height, { renderer: "svg" });
  if (!ec) return;
  var chart = ec.chart;

  var series = datasets.map(function(ds, i) {
    var s = {
      name: ds.label || ("series-" + i),
      type: "bar",
      data: ds.data,
      itemStyle: {
        color: ds.backgroundColor || getColor(i),
        borderRadius: isH ? [0, 2, 2, 0] : [2, 2, 0, 0],
      },
      emphasis: {
        itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.15)" },
      },
      barMaxWidth: isH ? 24 : 48,
      barGap: "20%",
    };
    if (isStacked) {
      s.stack = "total";
      s.itemStyle.borderRadius = (i === dLen - 1)
        ? (isH ? [0, 2, 2, 0] : [2, 2, 0, 0])
        : [0, 0, 0, 0];
    }
    return s;
  });

  var axes = _buildBarAxes(labels, opts);
  var option = {
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: "shadow" },
      formatter: function(params) { return _barTooltipFormatter(params, isStacked); },
    },
    legend: { show: true, bottom: 0, textStyle: { fontSize: 14 }, type: "scroll" },
    grid: {
      left: 16, right: 24,
      top: opts.yAxisName && !isH ? 36 : 24,
      bottom: dLen > 1 ? 56 : (opts.xAxisName ? 48 : 40),
      containLabel: true,
    },
    xAxis: axes.xAxis,
    yAxis: axes.yAxis,
    series: series,
    animation: true,
    animationDuration: 600,
  };

  chart.setOption(option);

  if (opts.onBarClick) {
    _bindClick(chart, function(params) {
      if (params.componentType === "series") {
        var idx = params.dataIndex;
        var sv = {};
        datasets.forEach(function(ds) { sv[ds.label] = ds.data[idx]; });
        opts.onBarClick({
          name: labels[idx],
          dataIndex: idx,
          seriesName: params.seriesName,
          value: params.value,
          color: typeof params.color === "string" ? params.color : getColor(params.seriesIndex || 0),
          seriesValues: sv,
        });
      }
    });
  }
}

/**
 * パイチャートを生成する (ECharts)。
 */
function createPieChart(container, labels, values, opts) {
  if (Array.isArray(opts)) opts = { colors: opts };
  opts = opts || {};
  var colors = opts.colors;
  var onSliceClick = opts.onSliceClick;

  var total = 0;
  values.forEach(function(v) { total += (v || 0); });
  if (total === 0) return;

  var ec = _initChart(container, 360, { renderer: "canvas" });
  if (!ec) return;
  var chart = ec.chart;

  var pieData = labels.map(function(lbl, i) {
    return {
      name: lbl,
      value: values[i] || 0,
      itemStyle: (colors && colors[i]) ? { color: colors[i] } : undefined,
    };
  });

  var option = {
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: function(p) {
        var c = typeof p.color === "string" ? p.color : getColor(p.dataIndex);
        return '<div style="font-weight:700;margin-bottom:4px;max-width:300px;word-break:break-all">' + escapeHtml(p.name) + "</div>" +
          '<div style="line-height:1.6">' +
          '<span style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;background:' + c + '"></span>' +
          fmtNum(p.value) + " 件" +
          ' <span style="color:' + _themeMuted + '">(' + p.percent + '%)</span>' +
          "</div>";
      },
    },
    legend: {
      orient: "horizontal",
      bottom: 0,
      textStyle: { fontSize: 14 },
      type: "scroll",
    },
    graphic: [{
      type: "group",
      left: "center",
      top: "38%",
      children: [
        { type: "text", style: { text: fmtNum(total), fontSize: 22, fontWeight: 700, fontFamily: _DIGITAL_THEME.textStyle.fontFamily, fill: _themeText, textAlign: "center", x: 0 }, left: "center" },
      ],
    }],
    series: [{
      type: "pie",
      radius: ["36%", "60%"],
      center: ["50%", "42%"],
      data: pieData,
      cursor: onSliceClick ? "pointer" : "default",
      label: {
        show: true,
        formatter: function(p) {
          var name = p.name.length > 10 ? p.name.slice(0, 9) + "…" : p.name;
          return name + "\n" + p.percent + "%";
        },
        fontSize: 14,
        lineHeight: 20,
        color: _themeMuted,
        minShowLabelAngle: 8,
      },
      labelLine: { length: 14, length2: 10, minTurnAngle: 105 },
      labelLayout: { hideOverlap: true },
      emphasis: {
        scale: true,
        scaleSize: 6,
        itemStyle: { shadowBlur: 10, shadowColor: "rgba(0,0,0,0.15)" },
      },
    }],
    animation: true,
    animationDuration: 600,
  };

  chart.setOption(option);

  if (onSliceClick) {
    _bindClick(chart, function(params) {
      var c = typeof params.color === "string" ? params.color : getColor(params.dataIndex);
      onSliceClick({
        name: params.name,
        value: params.value,
        percent: params.percent,
        color: c,
        dataIndex: params.dataIndex,
      });
    });
  }
}

/* ── 2D ピボット変換 ── */

function pivot2D(groups, groupByKeys, primaryMetric) {
  if (groupByKeys.length !== 2 || groups.length === 0) return null;

  var key0 = groupByKeys[0];
  var key1 = groupByKeys[1];

  var vals0 = [], vals1 = [];
  var set0 = {}, set1 = {};
  groups.forEach(function(g) {
    var v0 = g[key0] != null ? String(g[key0]) : "";
    var v1 = g[key1] != null ? String(g[key1]) : "";
    if (!set0[v0]) { set0[v0] = true; vals0.push(v0); }
    if (!set1[v1]) { set1[v1] = true; vals1.push(v1); }
  });

  if (vals0.length <= 1 || vals1.length <= 1) return null;

  var catKey, serKey, catValsRaw, serValsRaw;
  if (vals0.length >= vals1.length) {
    catKey = key0; serKey = key1;
    catValsRaw = vals0; serValsRaw = vals1;
  } else {
    catKey = key1; serKey = key0;
    catValsRaw = vals1; serValsRaw = vals0;
  }

  /* 各キーの合計を一度だけ計算 */
  var serTotals = {}, catTotals = {};
  groups.forEach(function(g) {
    var sv = g[serKey] != null ? String(g[serKey]) : "";
    var cv = g[catKey] != null ? String(g[catKey]) : "";
    var mv = g[primaryMetric] || 0;
    serTotals[sv] = (serTotals[sv] || 0) + mv;
    catTotals[cv] = (catTotals[cv] || 0) + mv;
  });

  var SER_LIMIT = 12;
  var serCollapsed = false;
  if (serValsRaw.length > SER_LIMIT) {
    serValsRaw.sort(function(a, b) { return (serTotals[b] || 0) - (serTotals[a] || 0); });
    serValsRaw = serValsRaw.slice(0, SER_LIMIT);
    serCollapsed = true;
  }

  /* カテゴリ軸: 合計降順ソート */
  catValsRaw.sort(function(a, b) { return (catTotals[b] || 0) - (catTotals[a] || 0); });

  /* シリーズ軸: 合計降順ソート */
  serValsRaw.sort(function(a, b) { return (serTotals[b] || 0) - (serTotals[a] || 0); });

  var catValues = catValsRaw;
  var serValues = serCollapsed ? serValsRaw.concat(["その他"]) : serValsRaw;

  var catIdx = {};
  catValues.forEach(function(v, i) { catIdx[v] = i; });
  var serIdx = {};
  serValues.forEach(function(v, i) { serIdx[v] = i; });
  var otherIdx = serCollapsed ? serValues.length - 1 : -1;

  var nCat = catValues.length, nSer = serValues.length;
  var matrix = [], indexMap = [];
  for (var ci = 0; ci < nCat; ci++) {
    matrix[ci] = [];
    indexMap[ci] = [];
    for (var si = 0; si < nSer; si++) {
      matrix[ci][si] = 0;
      indexMap[ci][si] = -1;
    }
  }

  groups.forEach(function(g, gi) {
    var cv = g[catKey] != null ? String(g[catKey]) : "";
    var sv = g[serKey] != null ? String(g[serKey]) : "";
    var cI = catIdx[cv];
    if (cI === undefined) return;
    var sI = serIdx[sv];
    if (sI === undefined) {
      if (otherIdx >= 0) {
        matrix[cI][otherIdx] += (g[primaryMetric] || 0);
        if (indexMap[cI][otherIdx] === -1) indexMap[cI][otherIdx] = gi;
      }
      return;
    }
    matrix[cI][sI] = g[primaryMetric] || 0;
    indexMap[cI][sI] = gi;
  });

  return {
    catKey: catKey,
    serKey: serKey,
    catValues: catValues,
    serValues: serValues,
    matrix: matrix,
    indexMap: indexMap,
  };
}

/* ── N次元ピボット (3軸以上) ── */

function pivotND(groups, groupByKeys, primaryMetric) {
  if (groupByKeys.length < 3 || groups.length === 0) return null;

  /* --- 階層ツリー構造 (ツリーマップ用) --- */
  function buildTree(subset, keyIdx) {
    var key = groupByKeys[keyIdx];
    var isLeaf = keyIdx === groupByKeys.length - 1;

    var buckets = {};
    var order = [];
    subset.forEach(function(item) {
      var v = item.group[key] != null ? String(item.group[key]) : "";
      if (!buckets[v]) {
        buckets[v] = [];
        order.push(v);
      }
      buckets[v].push(item);
    });

    order.sort(function(a, b) {
      var ta = 0, tb = 0;
      buckets[a].forEach(function(it) { ta += it.value; });
      buckets[b].forEach(function(it) { tb += it.value; });
      return tb - ta;
    });

    return order.map(function(v) {
      var items = buckets[v];
      var total = 0;
      items.forEach(function(it) { total += it.value; });

      if (isLeaf) {
        var gi = items[0] ? items[0].origIndex : -1;
        return { name: v, value: total, _groupIndex: gi, _level: keyIdx };
      } else {
        var children = buildTree(items, keyIdx + 1);
        return { name: v, value: total, children: children, _level: keyIdx };
      }
    }).filter(function(d) { return d.value > 0; });
  }

  var items = groups.map(function(g, gi) {
    return { group: g, value: g[primaryMetric] || 0, origIndex: gi };
  });
  var tree = buildTree(items, 0);

  /* --- サンキーフロー構造 --- */
  var sankeyNodes = [];
  var sankeyLinks = [];
  var nodeSet = {};

  function addNode(name, level) {
    var id = "L" + level + ":" + name;
    if (!nodeSet[id]) {
      nodeSet[id] = true;
      sankeyNodes.push({ name: id, _displayName: name, _level: level, _key: groupByKeys[level] });
    }
    return id;
  }

  for (var li = 0; li < groupByKeys.length - 1; li++) {
    var fromKey = groupByKeys[li];
    var toKey = groupByKeys[li + 1];
    var flowMap = {};

    groups.forEach(function(g) {
      var fromVal = g[fromKey] != null ? String(g[fromKey]) : "";
      var toVal = g[toKey] != null ? String(g[toKey]) : "";
      var metric = g[primaryMetric] || 0;
      if (metric <= 0) return;

      var fk = fromVal + "\t" + toVal;
      if (!flowMap[fk]) flowMap[fk] = { from: fromVal, to: toVal, value: 0 };
      flowMap[fk].value += metric;
    });

    var flows = Object.keys(flowMap).map(function(k) { return flowMap[k]; });
    flows.sort(function(a, b) { return b.value - a.value; });
    flows = flows.slice(0, 30);

    flows.forEach(function(f) {
      var srcId = addNode(f.from, li);
      var tgtId = addNode(f.to, li + 1);
      sankeyLinks.push({ source: srcId, target: tgtId, value: f.value });
    });
  }

  return {
    keys: groupByKeys,
    tree: tree,
    sankey: { nodes: sankeyNodes, links: sankeyLinks },
  };
}

/* ── 2D 積上げ棒グラフ (createBarChart への薄いラッパー) ── */

function createStackedBar2D(container, pivot, opts) {
  opts = opts || {};
  var nCat = pivot.catValues.length;
  var nSer = pivot.serValues.length;
  if (nCat === 0 || nSer === 0) return;

  /* pivot の matrix → datasets 変換 */
  var datasets = pivot.serValues.map(function(sv, si) {
    var data = [];
    for (var ci = 0; ci < nCat; ci++) data.push(pivot.matrix[ci][si]);
    return { label: sv, data: data, backgroundColor: getColor(si) };
  });

  createBarChart(container, pivot.catValues, datasets, {
    horizontal: nCat > 6,
    stacked: true,
    xAxisName: nCat > 6 ? (opts.valueLabel || "") : (opts.catAxisName || ""),
    yAxisName: nCat > 6 ? (opts.catAxisName || "") : (opts.valueLabel || ""),
    onBarClick: opts.onCellClick ? function(params) {
      opts.onCellClick({
        catIndex: params.dataIndex,
        serIndex: pivot.serValues.indexOf(params.seriesName),
        color: params.color,
      });
    } : undefined,
  });
}

/* ── ヒートマップ (ECharts) ── */

function createHeatmap(container, pivot, opts) {
  opts = opts || {};
  var nCat = pivot.catValues.length;
  var nSer = pivot.serValues.length;
  if (nCat === 0 || nSer === 0) return;

  var height = Math.max(300, Math.min(700, nSer * 36 + 140));
  var ec = _initChart(container, height, { renderer: "canvas" });
  if (!ec) return;
  var chart = ec.chart;

  var heatData = [];
  var maxVal = 0;
  for (var ci = 0; ci < nCat; ci++) {
    for (var si = 0; si < nSer; si++) {
      var v = pivot.matrix[ci][si];
      heatData.push([ci, si, v]);
      if (v > maxVal) maxVal = v;
    }
  }

  var option = {
    tooltip: {
      confine: true,
      formatter: function(params) {
        var d = params.data || params.value;
        var ci = d[0], si = d[1], val = d[2];
        var catLabel = escapeHtml(pivot.catValues[ci] || "");
        var serLabel = escapeHtml(pivot.serValues[si] || "");
        var rowTotal = 0;
        for (var j = 0; j < nSer; j++) { rowTotal += pivot.matrix[ci][j]; }
        var pct = rowTotal > 0 ? ' <span style="color:' + _themeMuted + '">(' + fmtPct(val / rowTotal) + ')</span>' : "";
        return '<div style="font-weight:700;margin-bottom:6px;max-width:360px;word-break:break-all;font-size:14px">' +
          catLabel + " / " + serLabel + "</div>" +
          '<div style="line-height:1.6">' +
          (opts.metricLabel ? escapeHtml(opts.metricLabel) + ": " : "") +
          fmtNum(val) + pct + "</div>" +
          '<div style="border-top:1px solid rgba(128,128,128,0.3);margin-top:4px;padding-top:4px;line-height:1.6">' +
          escapeHtml(catLabel) + " 合計: " + fmtNum(rowTotal) + "</div>";
      },
    },
    grid: {
      left: 16, right: 80, top: 24,
      bottom: nCat > 6 ? 80 : 48,
      containLabel: true,
    },
    xAxis: {
      type: "category", data: pivot.catValues,
      splitArea: { show: true },
      axisLabel: { rotate: nCat > 6 ? 30 : 0, fontSize: 14, interval: 0, hideOverlap: true, color: _themeMuted },
      axisLine: { lineStyle: { color: _themeBorder } },
      axisTick: { show: false },
      name: opts.catAxisName || "", nameLocation: "middle",
      nameGap: nCat > 6 ? 64 : 36, nameTextStyle: { fontSize: 14, color: _themeMuted },
    },
    yAxis: {
      type: "category", data: pivot.serValues,
      splitArea: { show: true },
      axisLabel: { fontSize: 14, color: _themeMuted, formatter: function(v) { return v.length > 20 ? v.slice(0, 19) + "…" : v; } },
      axisLine: { lineStyle: { color: _themeBorder } },
      axisTick: { show: false },
      name: opts.serAxisName || "", nameLocation: "end",
      nameTextStyle: { fontSize: 14, color: _themeMuted, padding: [0, 0, 8, 0] },
    },
    visualMap: {
      min: 0, max: maxVal || 1,
      calculable: true, orient: "vertical", right: 0, top: "center",
      inRange: { color: ["#DDE8FD", "#C5D7FB", "#8EA8F5", "#5A7BEE", "#2B4FD8", "#0017C1"] },
      textStyle: { color: _themeMuted, fontSize: 12 },
      formatter: function(v) { return fmtNum(Math.round(v)); },
    },
    series: [{
      type: "heatmap", data: heatData,
      label: {
        show: nCat * nSer <= 120, fontSize: 11, color: _themeText,
        formatter: function(params) { var v = params.data[2]; return v > 0 ? fmtNum(v) : ""; },
      },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.2)", borderColor: "#000", borderWidth: 1 } },
      itemStyle: { borderColor: _themeBg, borderWidth: 1, borderRadius: 2 },
    }],
    animation: true,
    animationDuration: 600,
  };

  chart.setOption(option);

  if (opts.onCellClick) {
    _bindClick(chart, function(params) {
      if (params.componentType === "series") {
        var d = params.data || params.value;
        opts.onCellClick({
          catIndex: d[0], serIndex: d[1],
          color: typeof params.color === "string" ? params.color : "#5A7BEE",
        });
      }
    });
  }
}

/* ── ツリーマップ (ECharts) ── */

function createTreemap(container, labels, values, opts) {
  opts = opts || {};
  var pivot = opts.pivot;
  var ndPivot = opts.pivotND;
  var isND = !!ndPivot;
  var hasHierarchy = !!pivot || isND;

  var ec = _initChart(container, isND ? 500 : 400, { renderer: "canvas" });
  if (!ec) return;
  var chart = ec.chart;

  var treeData;
  if (isND) {
    treeData = ndPivot.tree;
  } else if (pivot) {
    treeData = pivot.catValues.map(function(cv, ci) {
      var children = [];
      pivot.serValues.forEach(function(sv, si) {
        var v = pivot.matrix[ci][si];
        if (v > 0) children.push({ name: sv, value: v, _catIndex: ci, _serIndex: si });
      });
      var catTotal = 0;
      children.forEach(function(c) { catTotal += c.value; });
      return { name: cv, value: catTotal, children: children, _catIndex: ci };
    }).filter(function(d) { return d.value > 0; });
  } else {
    treeData = labels.map(function(lbl, i) {
      return { name: lbl, value: values[i] || 0, _dataIndex: i };
    }).filter(function(d) { return d.value > 0; });
  }

  /* levels 配列を動的生成 */
  var levels;
  if (isND) {
    var nLevels = ndPivot.keys.length;
    levels = [];
    for (var li = 0; li < nLevels; li++) {
      if (li === 0) {
        levels.push({ itemStyle: { borderColor: _themeBorder, borderWidth: 3, gapWidth: 5 }, upperLabel: { show: false } });
      } else if (li === nLevels - 1) {
        levels.push({ colorSaturation: [0.3, 0.8], itemStyle: { borderColorSaturation: 0.6, gapWidth: 1 } });
      } else {
        var satMin = Math.min(0.25 + 0.1 * li, 0.5);
        var satMax = Math.min(0.5 + 0.1 * li, 0.7);
        levels.push({
          colorSaturation: [satMin, satMax],
          itemStyle: { borderColorSaturation: 0.7, gapWidth: 3 - li, borderWidth: 2 },
        });
      }
    }
  } else if (pivot) {
    levels = [
      { itemStyle: { borderColor: _themeBorder, borderWidth: 2, gapWidth: 4 }, upperLabel: { show: false } },
      { colorSaturation: [0.3, 0.6], itemStyle: { borderColorSaturation: 0.7, gapWidth: 2, borderWidth: 2 } },
      { colorSaturation: [0.3, 0.8], itemStyle: { borderColorSaturation: 0.6, gapWidth: 1 } },
    ];
  } else {
    levels = [
      { colorSaturation: [0.25, 0.8], itemStyle: { borderColorSaturation: 0.7, gapWidth: 2, borderWidth: 2 } },
    ];
  }

  var option = {
    tooltip: {
      confine: true,
      formatter: function(params) {
        var d = params.data || {};
        var name = escapeHtml(params.name || "");
        var val = d.value || 0;
        var grandTotal = 0;
        treeData.forEach(function(n) { grandTotal += n.value; });
        var pct = grandTotal > 0 ? ' <span style="color:' + _themeMuted + '">(' + fmtPct(val / grandTotal) + ')</span>' : "";
        var lines = '<div style="font-weight:700;margin-bottom:6px;max-width:360px;word-break:break-all;font-size:14px">' + name + "</div>";

        if (isND && d._level !== undefined) {
          lines += '<div style="color:' + _themeMuted + ';margin-bottom:4px;font-size:12px">' + escapeHtml(ndPivot.keys[d._level] || "") + '</div>';
        }

        lines += '<div style="line-height:1.6">' +
          (opts.metricLabel ? escapeHtml(opts.metricLabel) + ": " : "") +
          fmtNum(val) + pct + "</div>";

        if (d.children && d.children.length > 0) {
          lines += '<div style="border-top:1px solid rgba(128,128,128,0.3);margin-top:4px;padding-top:4px">';
          var showChildren = d.children.slice(0, 5);
          showChildren.forEach(function(c, i) {
            var cpct = val > 0 ? ' <span style="color:' + _themeMuted + '">(' + fmtPct(c.value / val) + ')</span>' : "";
            var marker = '<span style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;background:' + getColor(i) + '"></span>';
            lines += '<div style="line-height:1.6">' + marker + escapeHtml(c.name) + ": " + fmtNum(c.value) + cpct + "</div>";
          });
          if (d.children.length > 5) {
            lines += '<div style="color:' + _themeMuted + '">… 他 ' + (d.children.length - 5) + ' 件</div>';
          }
          lines += "</div>";
        } else if (pivot && d._catIndex !== undefined && !isND) {
          var parentName = pivot.catValues[d._catIndex];
          if (parentName) {
            lines += '<div style="border-top:1px solid rgba(128,128,128,0.3);margin-top:4px;padding-top:4px;color:' + _themeMuted + '">' +
              escapeHtml(parentName) + '</div>';
          }
        }
        return lines;
      },
    },
    series: [{
      type: "treemap",
      data: treeData,
      roam: false,
      nodeClick: false,
      breadcrumb: { show: hasHierarchy, emptyItemWidth: 25 },
      label: {
        show: true,
        formatter: function(params) {
          var n = params.name || "";
          return n.length > 12 ? n.slice(0, 11) + "…" : n;
        },
        fontSize: 13,
      },
      upperLabel: hasHierarchy ? {
        show: true, height: 24, fontSize: 13, fontWeight: 700, color: "#fff",
      } : undefined,
      itemStyle: {
        borderColor: _themeBg, borderWidth: 2, gapWidth: 2, borderRadius: 3,
      },
      levels: levels,
    }],
    animation: true,
    animationDuration: 600,
  };

  chart.setOption(option);

  if (opts.onNodeClick) {
    _bindClick(chart, function(params) {
      var d = params.data || {};
      if (isND) {
        if (d._groupIndex !== undefined && d._groupIndex >= 0) {
          opts.onNodeClick({ groupIndex: d._groupIndex, name: params.name, color: typeof params.color === "string" ? params.color : "#5A7BEE" });
        }
      } else if (pivot) {
        if (d._serIndex !== undefined) {
          opts.onNodeClick({ catIndex: d._catIndex, serIndex: d._serIndex, color: typeof params.color === "string" ? params.color : "#5A7BEE" });
        }
      } else {
        if (d._dataIndex !== undefined) {
          opts.onNodeClick({ dataIndex: d._dataIndex, name: params.name, color: typeof params.color === "string" ? params.color : getColor(d._dataIndex) });
        }
      }
    });
  }
}

/* ── 力指向グラフ (ECharts) ── */

function createGraph(container, pivot, opts) {
  opts = opts || {};
  var nCat = pivot.catValues.length;
  var nSer = pivot.serValues.length;
  if (nCat === 0 || nSer === 0) return;

  var ec = _initChart(container, 450, { renderer: "canvas" });
  if (!ec) return;
  var chart = ec.chart;

  /* ノードサイズ: 合計値に比例 */
  var catTotals = [], serTotals = [];
  for (var ci = 0; ci < nCat; ci++) {
    var t = 0;
    for (var si = 0; si < nSer; si++) t += pivot.matrix[ci][si];
    catTotals.push(t);
  }
  for (var si2 = 0; si2 < nSer; si2++) {
    var t2 = 0;
    for (var ci2 = 0; ci2 < nCat; ci2++) t2 += pivot.matrix[ci2][si2];
    serTotals.push(t2);
  }
  var maxTotal = Math.max.apply(null, catTotals.concat(serTotals)) || 1;
  function nodeSize(total) { return 12 + 38 * Math.sqrt(total / maxTotal); }

  var catColor = "#0017C1";
  var serColor = "#5A7BEE";

  var nodes = [];
  pivot.catValues.forEach(function(cv, i) {
    nodes.push({ id: "cat_" + i, name: cv, symbolSize: nodeSize(catTotals[i]), category: 0, itemStyle: { color: catColor }, label: { show: true }, _type: "cat", _index: i });
  });
  pivot.serValues.forEach(function(sv, i) {
    nodes.push({ id: "ser_" + i, name: sv, symbolSize: nodeSize(serTotals[i]), category: 1, itemStyle: { color: serColor }, label: { show: true }, _type: "ser", _index: i });
  });

  var links = [];
  var maxEdgeVal = 0;
  for (var ei = 0; ei < nCat; ei++) {
    for (var ej = 0; ej < nSer; ej++) {
      var v = pivot.matrix[ei][ej];
      if (v > 0) {
        links.push({ source: "cat_" + ei, target: "ser_" + ej, value: v, _catIndex: ei, _serIndex: ej, lineStyle: { width: 1 } });
        if (v > maxEdgeVal) maxEdgeVal = v;
      }
    }
  }
  if (maxEdgeVal > 0) {
    links.forEach(function(link) { link.lineStyle.width = 1 + 5 * (link.value / maxEdgeVal); });
  }

  var option = {
    tooltip: {
      confine: true,
      formatter: function(params) {
        if (params.dataType === "edge" && params.data) {
          var srcNode = null, tgtNode = null;
          for (var ni = 0; ni < nodes.length; ni++) {
            if (nodes[ni].id === params.data.source) srcNode = nodes[ni];
            if (nodes[ni].id === params.data.target) tgtNode = nodes[ni];
          }
          return '<div style="font-weight:700;font-size:14px">' +
            escapeHtml(srcNode ? srcNode.name : "") + " ↔ " +
            escapeHtml(tgtNode ? tgtNode.name : "") + "</div>" +
            '<div>' + fmtNum(params.data.value) + "</div>";
        }
        return '<div style="font-weight:700;font-size:14px">' + escapeHtml(params.name) + "</div>";
      },
    },
    legend: {
      data: [
        { name: opts.catLabel || "カテゴリ", icon: "circle" },
        { name: opts.serLabel || "シリーズ", icon: "circle" },
      ],
      bottom: 0,
      textStyle: { fontSize: 14 },
    },
    series: [{
      type: "graph",
      layout: "force",
      data: nodes,
      links: links,
      categories: [
        { name: opts.catLabel || "カテゴリ", itemStyle: { color: catColor } },
        { name: opts.serLabel || "シリーズ", itemStyle: { color: serColor } },
      ],
      roam: true,
      draggable: true,
      label: {
        show: true, position: "right", fontSize: 13, color: _themeText,
        formatter: function(params) {
          var n = params.name || "";
          return n.length > 10 ? n.slice(0, 9) + "…" : n;
        },
      },
      lineStyle: { color: "source", curveness: 0.15 },
      emphasis: { focus: "adjacency", lineStyle: { width: 4 } },
      force: { repulsion: 200, gravity: 0.1, edgeLength: [80, 250], layoutAnimation: true },
    }],
    animation: true,
    animationDuration: 1000,
  };

  chart.setOption(option);

  if (opts.onEdgeClick) {
    _bindClick(chart, function(params) {
      if (params.dataType === "edge" && params.data) {
        opts.onEdgeClick({
          catIndex: params.data._catIndex,
          serIndex: params.data._serIndex,
          color: typeof params.color === "string" ? params.color : "#5A7BEE",
        });
      }
    });
  }
}

/* ── サンキーダイアグラム (ECharts) ── */

function createSankey(container, pivotData, opts) {
  opts = opts || {};
  var sankey = pivotData.sankey;
  if (!sankey || sankey.nodes.length === 0 || sankey.links.length === 0) {
    var empty = document.createElement("p");
    empty.className = "text-muted mt-16";
    empty.textContent = "サンキーダイアグラムを描画するデータがありません";
    container.appendChild(empty);
    return;
  }

  /* 軸ラベルバー */
  var axisBar = document.createElement("div");
  axisBar.style.cssText = "display:flex;justify-content:space-around;padding:4px 0 8px;font-size:12px;color:var(--text-muted)";
  pivotData.keys.forEach(function(k) {
    var span = document.createElement("span");
    span.textContent = k;
    span.style.fontWeight = "600";
    axisBar.appendChild(span);
  });
  container.appendChild(axisBar);

  var nLevels = pivotData.keys.length;
  var hBase = Math.max(400, Math.min(700, sankey.nodes.length * 18 + 100));

  var ec = _initChart(container, hBase, { renderer: "canvas" });
  if (!ec) return;
  var chart = ec.chart;

  var levelColors = [];
  for (var li = 0; li < nLevels; li++) levelColors.push(getColor(li * 2));

  var eNodes = sankey.nodes.map(function(n) {
    return {
      name: n.name,
      itemStyle: { color: levelColors[n._level] || "#5A7BEE" },
      _displayName: n._displayName,
      _level: n._level,
    };
  });

  var eLinks = sankey.links.map(function(lk) {
    return { source: lk.source, target: lk.target, value: lk.value };
  });

  var option = {
    tooltip: {
      confine: true,
      formatter: function(params) {
        if (params.dataType === "edge" && params.data) {
          var srcName = params.data.source.replace(/^L\d+:/, "");
          var tgtName = params.data.target.replace(/^L\d+:/, "");
          return '<div style="font-weight:700;font-size:14px;max-width:360px;word-break:break-all">' +
            escapeHtml(srcName) + ' → ' + escapeHtml(tgtName) + '</div>' +
            '<div style="line-height:1.6">' +
            (opts.metricLabel ? escapeHtml(opts.metricLabel) + ": " : "") +
            fmtNum(params.data.value) + '</div>';
        }
        if (params.data && params.data._displayName) {
          var levelKey = pivotData.keys[params.data._level] || "";
          return '<div style="font-weight:700;font-size:14px">' + escapeHtml(params.data._displayName) + '</div>' +
            '<div style="color:' + _themeMuted + '">' + escapeHtml(levelKey) + '</div>' +
            '<div>' + fmtNum(params.value) + '</div>';
        }
        return escapeHtml(params.name.replace(/^L\d+:/, ""));
      },
    },
    series: [{
      type: "sankey",
      data: eNodes,
      links: eLinks,
      orient: "horizontal",
      layoutIterations: 32,
      nodeAlign: "justify",
      nodeWidth: 20,
      nodeGap: 12,
      draggable: true,
      label: {
        show: true, position: "right", fontSize: 12, color: _themeText,
        formatter: function(params) {
          var n = params.data._displayName || params.name.replace(/^L\d+:/, "");
          return n.length > 14 ? n.slice(0, 13) + "…" : n;
        },
      },
      lineStyle: { color: "gradient", opacity: 0.4 },
      emphasis: { focus: "adjacency", lineStyle: { opacity: 0.7 } },
    }],
    animation: true,
    animationDuration: 600,
  };

  chart.setOption(option);

  if (opts.onLinkClick) {
    _bindClick(chart, function(params) {
      if (params.dataType === "edge" && params.data) {
        var srcMatch = params.data.source.match(/^L(\d+):(.*)/);
        var tgtMatch = params.data.target.match(/^L(\d+):(.*)/);
        if (srcMatch && tgtMatch) {
          opts.onLinkClick({
            sourceLevel: parseInt(srcMatch[1], 10),
            targetLevel: parseInt(tgtMatch[1], 10),
            sourceName: srcMatch[2],
            targetName: tgtMatch[2],
            value: params.data.value,
          });
        }
      }
    });
  }
}
