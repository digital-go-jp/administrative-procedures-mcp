# Administrative Procedures MCP Server

[日本語](README.md) | English

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.2%2B-green.svg)](https://gofastmcp.com)

> This is an English translation of [README.md](README.md). The Japanese version is authoritative; when the two differ, follow the Japanese text. Command examples and configuration snippets are kept identical in both versions.

An MCP server for analyzing the results of the Digital Agency of Japan's inventory survey of administrative procedures (about 75,000 procedure types). Connect it from an MCP-capable chat client such as Claude Desktop or ChatGPT and search and aggregate the data in natural language. Clients that support MCP Apps can render charts and tables directly inside the chat.

The data itself is in Japanese, but you do not need to read Japanese to use it. Ask in English and the LLM maps your question onto the Japanese field names through the dataset definition, then answers in English with the source cited. See [Working in English](#working-in-english).

A dedicated CLI (`apcli`) gives the same access from the command line without a chat client or an LLM.

This implementation is sample code for technical evaluation (see [Disclaimer](#disclaimer)).

## Features

- **Meaning made explicit through a data definition (dataset.yaml)** — field roles, code lists, and caveats are defined on the server side, which leaves the AI less room to fill gaps by guessing
- **Aggregation completed on the server** — group-by, metrics, and computed measures are calculated on the server, avoiding the errors that come from handing raw data to an AI and letting it do the arithmetic
- **Provenance and quality information attached** — tool responses carry the data source (`provenance`), field caveats (`notes`), and fill rates (`quality_summary`) so that an answer can show its grounds
- **MCP Apps support** — an interactive UI renders charts and tables inside the chat UI (ui resource)
- **Datasets added with YAML alone** — a new dataset can be added without changing any code

## How It Works

```text
question in any language
   → the LLM calls inspect_dataset
        learns field roles, code lists, caveats, and fill rates from dataset.yaml
   → the LLM calls summarize_records / query_records using the Japanese field names
        the server filters, groups, and computes; the response carries provenance, notes, quality_summary
   → the LLM answers in the user's language, quoting the returned figures and citing the source
```

Only four tools are exposed (`list_datasets`, `inspect_dataset`, `query_records`, `summarize_records`). The metadata in `dataset.yaml` does the work of guiding the model, and the same logic layer serves the MCP server, the CLI, and the tests. The architecture is described in [docs/development.en.md](docs/development.en.md).

## Installation

```bash
git clone https://github.com/digital-go-jp/administrative-procedures-mcp.git
cd administrative-procedures-mcp
./setup.sh
```

`setup.sh` installs the dependencies, fetches the data, and shows how to connect a client.
It asks which survey year to fetch (`--dataset all|r7|r6`; the default is both FY2025 and FY2024). The prompts of `setup.sh` are in Japanese.
To do the steps by hand:

```bash
uv sync --extra excel              # when using uv (recommended)
apcli fetch procedures-survey-r7   # fetch the FY2025 survey results from the publisher
apcli fetch procedures-survey-r6   # the FY2024 survey (for year-over-year comparison)
```

### About the Data

**The survey data is not bundled in the repository**, because the published files are sometimes corrected or updated.
`apcli fetch` downloads the latest version from the Digital Agency's distribution page and converts it to Parquet.

> Run `apcli fetch` only against a bundled `dataset.yaml` or one whose contents you have reviewed.

The fetch date is recorded in `.fetch.json` and reported as `provenance.fetched_at` in tool responses
(this is separate from `as_of_date`, which is the reference date of the data itself).

If automatic fetching fails, for example because the distribution page changed, save the file from the page and import it with:

```bash
apcli add procedures-survey-r6 --csv <saved file>
```

### MCP Specification Version

The default installation runs on MCP specification `2025-11-25`. To run on the latest `2026-07-28` (stateless core and response caching), additionally install the FastMCP 4 series:

```bash
pip install --pre "fastmcp>=4.0.0b1"
```

The server supports both series. The version actually negotiated can be read from `mcp_protocol_version` at `/health`. FastMCP 4 is a pre-release at the time of writing, so the default dependency stays on the 3 series.

**Details of the implementation work for MCP 2026-07-28 are in [docs/development.en.md](docs/development.en.md#51-mcp-specification-and-fastmcp-compatibility).**

## Configuration

### Claude Code

The repository ships with `.mcp.json`. **No additional configuration is needed.**
Start Claude Code in the cloned directory and it connects.

### Claude Desktop

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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

> Set `command` to the full path of `fastmcp` inside the virtual environment.
> To disable the MCP Apps UI, add `"env": { "MCP_NO_UI": "1" }`.
> When starting from outside the repository, point to `datasets/` with `"env": { "ADMIN_PROCEDURES_DATA_DIR": "/path/to/repo" }`.

Instead of editing the configuration file by hand, you can register the server with:

```bash
apcli install desktop     # Claude Desktop
apcli install json        # print the configuration only (no file is changed)
```

### ChatGPT

Start the MCP server in HTTP mode, place it where it can be reached over HTTPS, then register the server URL (`https://<your-domain>/mcp`) under Settings > Connectors > Create and enable it in a chat. See MCP server (HTTP mode) under [Running](#running) for details.

## Running

### CLI (`apcli`)

A lightweight command-line tool that works without an LLM. It builds the registry in-process and reads the data directly, so no MCP server is needed.

```bash
apcli list                                    # list datasets
apcli list -q 棚卸                             # filter by keyword
apcli inspect procedures-survey-r6            # structure and quality
apcli query procedures-survey-r6 -q 相続 --limit 5  # search records
apcli summarize procedures-survey-r6 -g 所管府省庁 -m count  # aggregate (short form)
apcli summarize procedures-survey-r6 \
  --group-by '["所管府省庁"]' --metrics '["count"]'  # aggregate (JSON array form)
apcli describe                                # print all tool definitions (for agents)
apcli describe query_records                  # print one tool definition
apcli --quiet query procedures-survey-r6      # suppress diagnostics on stderr
```

Options that take several values accept either repeated flags (`-g` and so on) or a JSON array:

```bash
apcli summarize procedures-survey-r6 -g 所管府省庁 -g 手続類型 -m count  # cross-tabulation (recommended)
apcli query procedures-survey-r6 -w '{"所管府省庁":["厚生労働省"]}' -s 手続名  # filter and select
```

The location of `datasets/` can be overridden with the environment variable `ADMIN_PROCEDURES_DATA_DIR` (the repository root is detected automatically when unset).

Field names such as 所管府省庁 (responsible ministry) and 手続類型 (procedure type) are the survey's own Japanese labels. `apcli inspect` lists them together with their descriptions, and `apcli describe` prints the tool definitions, so an agent can pick them up without reading Japanese. CLI messages are in Japanese.

#### HTML Output

The `--html` flag or the `-o` option emits a self-contained HTML file.

```bash
apcli inspect procedures-survey-r6 --html           # print HTML to stdout
apcli inspect procedures-survey-r6 -o report.html   # save to a file
apcli summarize procedures-survey-r6 -g 所管府省庁 -m count -o result.html
```

#### Preview

`apcli preview` starts the MCP Apps UI locally so that you can try the application with the browser's built-in AI (Prompt API / Gemini Nano).

```bash
apcli preview            # opens the browser (default: http://127.0.0.1:8765/)
apcli preview --port 9000 --no-open
```

Please note:
- **Limits of the built-in browser AI** — simple queries work, but combinations of several conditions and complex filters may not behave as expected. Use a full LLM such as Claude for serious analysis.
- **Platform support** — the Prompt API is available in Chrome (138+, verified with 151) and Microsoft Edge Canary/Dev (138.0.3309.2+, with the "Prompt API for on-device language model" flag enabled in `edge://flags`). Dataset exploration at startup works in any browser.
- **Progress display** — while a question is being processed, each step (session preparation, tool selection, argument generation, execution) is shown with elapsed seconds and the model's partial output. If a response stalls, the "中止" (cancel) button aborts it.

### MCP Server (HTTP Mode)

To connect from an external client such as ChatGPT, start the server with the HTTP transport and place it where it can be reached over HTTPS.

```bash
fastmcp run -m admin_procedures --transport streamable-http --port 8000
```

Or through environment variables:

```bash
ADMIN_PROCEDURES_PORT=8000 python -m admin_procedures
```

> **By default the server binds to `127.0.0.1` only and cannot be reached from outside.** This is fine when a reverse proxy on the same host (nginx and so on) terminates HTTPS and forwards to this port.
>
> To expose the process itself, for example from a container, set the bind address explicitly:
> - With `fastmcp run`: add `--host 0.0.0.0`
> - With `python -m admin_procedures`: set `ADMIN_PROCEDURES_HOST=0.0.0.0` (or `ADMIN_PROCEDURES_PUBLIC=1`)
>
> When exposing the server, put a reverse proxy with authentication and rate limiting in front of it (see [Notes on Use](#notes-on-use)).

Register the server URL (`https://<your-domain>/mcp`) on the client side.

## Tools

| Tool | Description |
|-------|------|
| `list_datasets` | Return the list of available datasets |
| `inspect_dataset` | Return a dataset's structure and quality overview |
| `query_records` | Fetch records with filters, full-text search, sorting, and pagination |
| `summarize_records` | Compute grouped aggregates (count/sum/avg/min/max) on the server |

Tool responses carry the data source (`provenance`), field caveats (`notes`), and a quality summary (`quality_summary`) to reduce misreading of the data and inappropriate aggregation by the AI. When an input field name is corrected automatically (normalizing notation variants or matching a similar name), the correction is reported in the response as `resolved_fields`.

<details>
<summary>Parameter details</summary>

#### `inspect_dataset`

- `dataset_id` (str): dataset ID

#### `query_records`

- `dataset_id` (str): dataset ID
- `where` (dict, optional): filter conditions (string = partial match, array = IN, `$gte`/`$lte` = range, `$ne` = not equal, `$not_contains` = partial mismatch, `$not_empty` = non-empty; its value is always `null`, written as `{"$not_empty": null}`)
- `q` (str, optional): full-text search keyword
- `search_fields` (list, optional): restrict the full-text search scope
- `select` (list, optional): fields to return
- `order_by` (str, optional): sort key (prefix `-` for descending)
- `limit` (int, optional): number of records (default 50, maximum 5,000)
- `cursor` (str, optional): pagination cursor

#### `summarize_records`

- `dataset_id` (str): dataset ID
- `group_by` (list, optional): grouping fields
- `metrics` (list): aggregation metrics (for example `["count", "sum:総手続件数", "avg:オンライン率"]`)
- `where` (dict, optional): filter conditions
- `explode` (str, optional): expand a semicolon-separated field
- `having` (dict, optional): post-aggregation filter (for example `{"count": {"$gte": 10}}`)
- `limit` (int, optional): maximum number of groups (default 200, maximum 10,000)

</details>

## Usage Examples

From a chat client connected to the MCP server, such as Claude Desktop or ChatGPT, you can type prompts like the following in English.
(The built-in browser AI in `apcli preview` is meant for simple queries. Use the CLI or a full LLM such as Claude for anything complex.)

```text
Rank the ministries by the number of administrative procedure types they are responsible for (top 10).
Inspect the dataset structure first, and include the data source and quality information.
```

```text
What patterns do the procedures that are not yet available online show?
Aggregate the whole dataset first, then show me a few concrete examples.
```

```text
Find administrative procedures that look like good candidates for review.
Restrict to applications (申請等) and pick candidates among those not yet available online.
Keep facts and suggestions separate.
```

```text
Compare the online rate by ministry between the FY2024 and FY2025 datasets.
Aggregate each year separately, line the results up, and point out the ministries with the largest change.
Note where the set of procedures or the definitions of survey items differ between years.
```

## Working in English

Everything in the data is Japanese: field names, code values, and free-text entries are the survey's own labels, and so are the descriptions in `dataset.yaml`. The server does not translate them. What makes English use possible is the metadata-driven design: the LLM reads the field list, roles, code lists, and caveats through `inspect_dataset`, maps an English question onto the Japanese fields, calls the aggregation tools with the Japanese names, and reports the returned figures in English with the Japanese terms glossed.

### Example

The following is a real run. An LLM agent was given only the `apcli` CLI and the bundled dataset definition, with no translation layer and no English metadata, and asked in English:

```text
In the FY2025 dataset, what share of procedures are already available online, and how does the online
rate differ between procedures initiated by individuals (国民等) and by businesses (民間事業者等)?
```

What the agent did:

1. Ran `inspect` on `procedures-survey-r7` and found the computed measure オンライン率 (online rate) and the dimension 手続主体 (applicant type) together with its code list.
2. Ran `summarize procedures-survey-r7 -m count -m avg:オンライン率` for the overall rate.
3. Ran `summarize procedures-survey-r7 -g 手続主体 -m count -m avg:オンライン率` for the breakdown.

What it answered (excerpt, lightly condensed):

> The overall online rate across all 76,275 procedure types is 0.5366 (53.66%). Procedures initiated by 民間事業者等 (private businesses) have an online rate of 0.6850 (68.50%; 23,629 procedure types), versus 0.3637 (36.37%; 5,106 procedure types) for 国民等 (individuals). The combined category 7 国民等、民間事業者等 (either individuals or businesses; 6,164 procedure types; 62.33%) is reported separately rather than folded into either side.
>
> Provenance: dataset_id `procedures-survey-r7`, publisher デジタル庁 (Digital Agency of Japan), published 2026-08-26, data as of 2025-11-01, source https://www.digital.go.jp/resources/procedures-survey-results.
>
> Note: this survey has no category equivalent to "5 一部実施済" (partially online) of the FY2024 survey, so counts of "1 実施済" (online) are not directly comparable across years.
>
> This is an analytical output derived from the published dataset. It is not an official position of the Government of Japan.

A similar English question about the ministries with the most procedure types returned 国土交通省 (MLIT) 13,406, 厚生労働省 (MHLW) 10,636, 経済産業省 (METI) 8,783, 農林水産省 (MAFF) 8,758, and 財務省 (MOF) 7,022, with the same provenance block.

Two things the agent noted about working in English are worth knowing. Code values carry their numeric prefix as part of the string (`5 国民等`, not `国民等`), so values must be copied from `inspect` output rather than guessed from an English gloss. And 所管府省庁 (the ministry with jurisdiction over the underlying law) is a different field from 実施府省庁 (the ministry that operates the procedure); the field descriptions make the distinction, but the English glosses sound alike.

Known limitations:

- Server error messages, CLI messages, and the tool descriptions are in Japanese. A full LLM handles them; a small model may not.
- The built-in browser AI used by `apcli preview` is a small on-device model and works best with simple Japanese queries.
- Full-text search (`q`) matches the Japanese text, so search terms must be Japanese. The LLM can translate a term before searching, but results depend on the exact wording used in the survey.

## Datasets

The repository bundles dataset definitions (`dataset.yaml`) for the FY2024 and FY2025 inventory surveys of administrative procedures as samples. The data itself is not included; `apcli fetch` downloads it from the [distribution page](https://www.digital.go.jp/resources/procedures-survey-results) and converts it to Parquet (see [About the Data](#about-the-data)).

| Dataset ID | Title | Publisher |
|----------------|---------|--------|
| `procedures-survey-r7` | Inventory survey of administrative procedures, FY2025 (Reiwa 7; data as of 1 November 2025) | Digital Agency of Japan |
| `procedures-survey-r6` | Inventory survey of administrative procedures, FY2024 (Reiwa 6; complete survey, data as of 31 March 2024) | Digital Agency of Japan |

The `r6` / `r7` in the dataset IDs are the Japanese era years Reiwa 6 and Reiwa 7. The two surveys differ in column layout and in some code values: the FY2025 survey has no 法令番号 (law number) field and no "5 一部実施済" (partially online) category in オンライン化の実施状況 (online implementation status), and it adds several free-text supplementary fields. Check each dataset's definition with `inspect_dataset` before comparing them.

See [docs/development.en.md](docs/development.en.md) for how to add a dataset and [docs/dataset-yaml-guide.en.md](docs/dataset-yaml-guide.en.md) for how to write `dataset.yaml`. A JSON Schema for completion and validation is bundled at [datasets/dataset-v1.schema.json](datasets/dataset-v1.schema.json).

For AI agents, the list of documents in the repository is published as [llms.txt](llms.txt).

## Notes on Use

This repository is an experimental sample for a local or single-user trial of MCP-based search and aggregation and MCP Apps rendering on public data. Production services that host many users and operation as a platform are out of scope.

- The CLI, stdio, and `apcli preview` are meant for local trial. The preview binds to `127.0.0.1` only by default.
- **When deploying to production**: the HTTP transport itself implements no user authentication, authorization, rate limiting, or audit logging. If the server must be reachable from outside, put a reverse proxy with authentication and traffic controls in front of it.
- `dataset.yaml` is treated as a trusted configuration file. Do not run `apcli fetch` or `apcli add` against a YAML file of unknown origin.

The trust boundary of the implementation and the considerations for public exposure are described in the [development guide](docs/development.en.md#11-scope-and-trust-boundary).

## Development

See [docs/development.en.md](docs/development.en.md).

## License

[MIT License](LICENSE)

## Disclaimer

This implementation is sample code for technical evaluation.

- Stability of operation and continued maintenance are not guaranteed
- The accuracy and currency of the loaded data are not guaranteed
- The output of this implementation is not an official position of the Government of Japan
- The data reflects what each ministry reported as of the survey date and may differ from the current situation
- When using the data, also consult the [original source](https://www.digital.go.jp/resources/procedures-survey-results)
