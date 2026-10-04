# OpenFisca-Japan-MCP benchmark

ホストのopencodeでモデルを実行し、MCPあり／なしでpenFisca-Japanの計算結果と一致するかを測ります。

## スコア

唯一のスコアは `result_match` です。各ケースでエージェントが書いた `answer.json` が、
固定バージョンのOpenFisca SDKから作った期待値と完全一致すれば1です。
MCP使用有無、ツール呼び出し履歴、回答文面は採点しません。

期待値・回答値の各要素は数値、または範囲オブジェクトです。

```json
{"min": 10000, "max": 15000, "min_inclusive": true, "max_inclusive": false}
```

範囲同士は両端の値と端点の含み方が一致したら一致とします。
単点の範囲（`min == max`）はその数値と等しいものとして扱います。単位は無視します。

## 必要環境

- Python 3.11、`uv`、Git
- ホストのopencode v2（モデル利用可能な状態。例: `openai/gpt-5.6-luna`）

CLIの探索順は `OPENCODE_CLI` 環境変数 → PATH上のv2 → デスクトップ同梱版の最新です。
PATHの編集は不要です。

## 実行

```sh
cp .env.example .env   # OPENCODE_MODEL を設定
python scripts/run_benchmark.py --condition both
```

- `--condition both|with-mcp|without-mcp`（既定 `both`）
- `--model`（既定は `.env` の `OPENCODE_MODEL`、なければ `openai/gpt-5.6-luna`）
- `--case ID` でケース絞り込み（複数指定可）

`with-mcp` ではOpenFisca MCPサーバーをstdioで接続し、`without-mcp` では接続しません。
実行記録は `host-runs/<timestamp>/` に保存されます。

## ケース追加

`cases.json` の各要素に以下を指定します。

- `id`, `benefit`, `date`, `prompt`
- `household_list`: 期待値計算用の構造化入力（エージェントには渡しません）
- `targets`: 比較する結果の場所の配列（`scope` は `household` または `member`）

## 注意事項

- 期待値は固定したOpenFisca-Japan SDKから生成します。正しさはSDKの実装を超えません。
- 20ケースのうち児童手当の基本3件は平易です。境界年齢・賞与合算・個人事業主・障害者控除・生活保護などの難問17件を含みます。
