# 開発・検証手順

## ソースの構成

| パス | 内容 |
| --- | --- |
| `src/connection_map/` | CLI、言語ごとの解析器、グラフ契約、保存・問い合わせ・鮮度確認 |
| `src/connection_map/web/` | ローカルWeb画面と周辺図・診断・注釈の表示 |
| `tests/` | Pythonの回帰テストと画面用JavaScriptのテスト |
| `tests/fixtures/`・`tests/completeness/` | 解析用ソースと、宣言・確定辺・候補辺を検証するgolden fixture |
| `scripts/` | 完全性検証、性能fixtureの生成、配布物・インストーラーの作成 |
| `schemas/` | 解析JSON、レイアウト、manual注釈の契約 |
| `docs/`・`examples/` | 利用手順、解析制約、設定・JSONの例 |

解析対象のアプリケーションを起動する必要はありません。解析結果、キャッシュ、配布物、外部リポジトリでの検証ログはソースと分けて保管します。既定の生成先は`.tmp/`、`dist/`、`release-artifacts/`などのGit除外対象です。

## 開発環境

リポジトリのルートで実行します。Python 3.11以上、uv、Node.jsが必要です。

```sh
uv sync --locked --extra test --extra web --extra sql --extra lint
```

`web`のTree-sitter依存はC/C++などの解析テストでも共用します。VB.NETの専用grammarを検証するWindows環境では`--extra visual-basic`も追加します。対象ソースが新しいPython構文を使う場合は、その構文を読めるPythonを選択してください。

## 通常の検証

```sh
uv run pytest
uv run ruff check src tests scripts
node --check src/connection_map/web/app.js
node --check src/connection_map/web/exploration.js
node --test tests/viewer.test.cjs
```

テストは解析対象ソースを読み取って関係を確認します。未解決・外部・候補の接続を、同名関数への確定接続として扱わないことも検証します。Windowsでsymlinkの作成権限がない場合、該当するテストは理由を表示してスキップします。

CIはPython 3.11・3.12・3.13でテストとlintを実行し、WindowsでVB.NET grammarを別途検証します。配布物とインストーラーの検証もCIに含みます。

## CLIと配布物の検証

```sh
uv run python scripts/generate_synthetic_graph.py --nodes 1000 --output .tmp/performance-1k.json
uv run connection-map validate .tmp/performance-1k.json
uv run connection-map split .tmp/performance-1k.json --output .tmp/bundle-1k
uv run connection-map validate-bundle .tmp/bundle-1k
uv run connection-map search .tmp/bundle-1k function_959
uv build
uv run python scripts/verify_distribution.py --dist dist
```

配布物には解析器とWeb資材の両方が必要です。インストーラーを作る場合の追加依存・手順は[インストール手順](installation.md)と`scripts/build_installers.py --help`を確認してください。

## 実リポジトリで確認する項目

解析JSONの整合性だけでなく、`freshness`、`coverage`、`truncation`と各接続の宣言根拠を確認します。構文回復診断、動的処理、マクロ、外部依存、テンプレート実体化などの制約は[人とAIの接続探索](ai-context.md)に記載しています。

対応する範囲では、呼び出し元・先を実ソースと照合してください。`investigate`では既定予算に対象・呼び出し元の本文・関連テストが入り、キャッシュを使う場合もソースが一致することを確認します。`tests/test_investigation.py`は誤った型推定、サイズ省略、古いソース、設定・コード更新、Git差分と外部コマンドの抑止、CLI/HTTPを検証します。画面のAI保存は`investigate` APIのJSONを再整形せず保存し、`tests/viewer.test.cjs`で確認します。保存済みグラフはコード変更後に再解析してください。
