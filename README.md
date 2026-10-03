# OpenFisca-Japan-MCP benchmark

安価なLLMが `OpenFisca-Japan-MCP` のMCPツールを正しく使えるかを測る、小さな再現可能ベンチマークです。

## 評価方法

各ケースについて、まず `openfisca_japan_mcp.sdk.calc()` を直接実行し、期待値（oracle）を作ります。その後、短い相談文だけをLLMへ渡し、実際のMCPサーバーに接続してツールを使わせます。レポートでは以下を分けて記録します。

- `result_match`: MCPの計算結果がSDKの期待値と一致したか
- `input_match`: 世帯の事実、制度名、出力単位、基準日を正しくツールに渡したか（表示名は比較対象外）
- `info_tool_used`: `tax_benefit_info` を使ったか
- `calculate_tool_used`: `calculate_tax_benefit` を成功呼び出ししたか
- `tool_calls` / `final_answer`: 引数、ツール結果、最終回答

基準日と依存バージョンを固定します。通常実行は `uv.lock` のパッケージを使い、サブモジュール実行スクリプトは期待値生成・MCPサーバーの両方で同じ固定コミットのソースを使います。初期ケースは児童手当の3ケースです。制度ロジックの幅を広げる場合は `cases.json` にケースを追加してください。

## 必要環境

- Python 3.11
- `uv`
- Git
- OpenAI互換のチャット補完APIとツール呼び出しに対応したLLM

## MCPサブモジュールで実行

`scripts/run_benchmark.py` はMCPサブモジュールのPythonソースを使い、`uv.lock` に従ってベンチマークを実行します。

```sh
python scripts/run_benchmark.py
```

ルートに `.env` を作り、LLMの設定を書きます（`.env.example` を参照）。OSの環境変数が設定済みの場合はそちらを優先します。ケースを絞る場合は引数をそのまま渡します。

```sh
python scripts/run_benchmark.py --case child-allowance-one-child
```

## 結果

各実行結果は `results/<UTC timestamp>.json` に保存されます。レポートにはプロンプト、期待値、LLMが実際に呼んだツールと引数、ツールの戻り値、最終回答が含まれます。結果ファイルには世帯入力に関する情報が含まれるため、共有前に内容を確認してください。

## ケース追加

`cases.json` の1要素に以下を指定します。

- `id`, `benefit`, `date`, `prompt`
- `household_list`: 期待値計算用の構造化入力（LLMには直接渡しません）
- `targets`: 比較する結果の場所の配列。`scope` は `household` または `member`

`targets` の例：

```json
{"targets": [{"household_index": 0, "scope": "member", "member_index": 0, "name": "住民税"}]}
```

## 注意事項

- このベンチマークの `result_match` はMCPツールの戻り値を検査します。自然言語の最終回答が数値を正しく説明したかは別途採点が必要です。
- WindowsではOpenFisca-Japanの一部パラメータYAMLの文字コード判定に合わせ、実行前に `PYTHONUTF8=1` を設定してください（上記PowerShell例を参照）。MCPサーバーの子プロセスにも引き継がれます。
- `info_tool_used` は呼び出し有無の計測であり、取得した属性を正しく反映したかは `tool_calls` の引数と結果を確認してください。
- 期待値の正しさは、OpenFisca-Japanの実装・バージョンの正しさを超えるものではありません。依存バージョンと基準日を固定してご利用ください。
- 初期ケースだけでモデル全般の能力を断定しないでください。年齢境界、複数人・複数世帯、情報不足、入力値の型・単位などのケースを増やしてください。
