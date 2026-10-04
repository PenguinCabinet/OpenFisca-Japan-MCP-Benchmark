# OpenFisca-Japan-MCP benchmark (Harbor)

[Harbor](https://www.harborframework.com/) で OpenCode を実行し、
MCPあり／なしでOpenFisca-Japanの計算結果と一致するかを測るベンチマークです。
実行・環境管理・結果収集はHarborに任せます。自作のエージェントループはありません。

## スコア

唯一のスコアは `result_match`（0/1）です。各タスクでエージェントが書いた
`/app/answer.json` が、固定バージョンのOpenFisca SDKから作った期待値と完全一致すれば1です。
MCP使用有無、ツール呼び出し履歴、回答文面は採点しません。

期待値・回答値の各要素は数値、または範囲オブジェクトです。

```json
{"min": 10000, "max": 15000, "min_inclusive": true, "max_inclusive": false}
```

範囲同士は両端の値と端点の含み方が一致したら一致とします。
単点の範囲（`min == max`）はその数値と等しいものとして扱います。単位は無視します。

## 必要環境

- Python 3.11、`uv`、Git、Docker
- `harbor` CLI（`uv tool install harbor`）
- OpenCodeが使えるモデル（例: Ollamaの `qwen2.5:3b`）

## 実行

```sh
cp .env.example .env   # OPENCODE_MODEL などを設定
python scripts/run_benchmark.py --condition both --model ollama/qwen2.5:3b
```

- `--condition both|with-mcp|without-mcp`（既定 `both`）
- `--model`（既定は `.env` の `OPENCODE_MODEL`、なければ `ollama/qwen2.5:3b`）
- `--case ID` でケース絞り込み（複数指定可）
- `--generate-only` でタスク生成のみ
- `--` 以降は `harbor run` への追加引数として渡します

`cases.json` をもとに `harbor/datasets/openfisca-bench/` へHarborタスクを生成し、
`with-mcp`（MCPサーバー `openfisca` をstdioで接続）と
`without-mcp`（MCPなし）の2条件で同じモデル・同じタスクを実行します。
結果は `jobs/` に保存されます。

## ベースライン結果（2026-10-04）

モデル `opencode/muse-spark-1.3-contributor-free`、3ケース：

- `with-mcp`: 3/3（全試行で `tax_benefit_info` → `calculate_tax_benefit` を使用）
- `without-mcp`: 3/3（1試行はDocker側の一時エラーのため再実行）

現行の児童手当3ケースは平易で、MCPなしでも正答できるため条件差が出ません。
MCPの効果を測るには、境界年齢・複数世帯・知名度の低い制度などのケース追加が必要です。

## ケース追加

`cases.json` の各要素に以下を指定します。

- `id`, `benefit`, `date`, `prompt`
- `household_list`: 期待値計算用の構造化入力（エージェントには渡しません）
- `targets`: 比較する結果の場所の配列（`scope` は `household` または `member`）

## 注意事項

- 期待値は固定したOpenFisca-Japan SDKから生成します。正しさはSDKの実装を超えません。
- MCPサーバーはエージェントと同じコンテナ内で `openfisca-japan-mcp` コマンドとして起動します。
  タスク image は `openfisca-japan-mcp` とopencode実行環境（Node 22＋`opencode-ai`）を含みます。
  初回ビルド後はキャッシュが効きます。
