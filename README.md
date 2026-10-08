> [!WARNING]
> 本リポジトリは開発中であり、テストデータの品質は保証できません。     
> ハーネスのログで、MCPをカンニングをしていないことを確認できたモデルのみ記載しています。MCPをカンニングできてしまうバグは修正予定です。     

# OpenFisca-Japan-MCP benchmark

[OpenFisca-Japan-MCP](https://github.com/project-inclusive/OpenFisca-Japan-MCP)の使用有無で、推論に差が出るか検証するベンチマークです。

また安価なモデルでOpenFisca-Japan-MCPを正しく使えるかの検証も兼ねています。

## スコア表

|モデル名|MCP使用の有無|正答数|正答率|
|---|---|---|---|
|gpt-6-astra|No|11|55%|
|gpt-6-sol|Yes|20|100%|
|gpt-6-luna|Yes|20|100%|

[詳しいベンチマークのログはこちら](./bench_history)

## 検証方法
1. 入力パラメータを事前に作成する(ベンチ対象LLMから独立したLLMを使用)
2. 対応する相談文を事前に作成する(ベンチ対象LLMから独立したLLMを使用)
3. 相談文を入力し、OpenCodeで数値を推論してもらう(MCPの使用ありなしの2パターンを試す)
4. 入力パラメータでOpenFiscaを実行し、実際の数値を取得する(これならば、数値から直接OpenFisca推論で実行されるので正しい値になる)
5. 3,4を比較し完全一致なら正解。それ以外は不正解とする

## 実行方法
```
python scripts/run_benchmark.py --condition both --model openai/gpt-6-sol 
```
- `--condition both|with-mcp|without-mcp`（既定 `both`）
- `--model`（既定は `openai/gpt-5.6-luna`）
- `--case ID` でケース絞り込み（複数指定可）


## 必要環境

- Python 3.11、`uv`、Git
- ホストのopencode v2（モデル利用可能な状態。例: `openai/gpt-5.6-luna`）

CLIの探索順は `OPENCODE_CLI` 環境変数 → PATH上のv2 → デスクトップ同梱版の最新です。
PATHの編集は不要です。

## ケース追加

`cases.json` の各要素に以下を指定します。

- `id`, `benefit`, `date`, `prompt`
- `household_list`: 期待値計算用の構造化入力（エージェントには渡しません）
- `targets`: 比較する結果の場所の配列（`scope` は `household` または `member`）

## 注意事項

- 期待値は固定したOpenFisca-Japanから生成します。OpenFisca-Japanが正しいことを前提としています
- 20ケースのうち児童手当の基本3件は平易です。境界年齢・賞与合算・個人事業主・障害者控除・生活保護などの難問17件を含みます。
