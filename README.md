# Connection Analysis Mapping

Connection Analysis Mappingは、リポジトリ内の関数・クラス・モジュールの関係を解析し、ローカルのWeb画面で探索するためのツールです。

## 最初に行うこと

GitHub Releaseのportable zipを使用してください。

1. zipを任意のフォルダーへ展開します。
2. `launcher/connection-map.cmd`（Windows）または`launcher/connection-map.sh`（Linux/macOS）で解析を実行します。
3. `serve`を起動してブラウザで関係マップを開きます。

```powershell
cd C:\path\to\connection-map-portable
launcher\connection-map.cmd analyze --root C:\path\to\repository
launcher\connection-map.cmd serve
```

Linux/macOSでは次を実行します。

```sh
cd /path/to/connection-map-portable
./launcher/connection-map.sh analyze --root /path/to/repository
./launcher/connection-map.sh serve
```

portable版は複数のリポジトリを一つのデータ領域で管理できます。解析結果やレイアウトを残したままアプリ部分だけ更新できます。

## できること

- 関数、メソッド、クラス、モジュール、HTML要素、CSSルールの表示
- 呼び出し、読み込み、継承、SQL読み書きなどの関係の探索
- 検索結果から対象へ移動
- ズーム、パン、詳細表示、接続先への移動
- 対応言語の解析（詳細は[対応言語一覧](docs/languages.md)を参照してください）
- 複数言語を一度に解析し、画面上で表示言語を切り替える操作

解析は静的に行い、対象リポジトリのコードは実行しません。動的dispatch、実行時型解決、macro展開、bundler alias、動的SQLなどは完全には解決できない場合があります。

Python解析はportable版に同梱されたPythonで実行できます。Tree-sitterやSQLパーサーを使う言語は、Python環境への追加依存が必要です。

portable版で追加依存が必要な言語を解析する場合は、[インストール手順](docs/installation.md)を確認してください。

## ドキュメント

- [インストール手順](docs/installation.md)
- [操作方法](docs/operation.md)
- [コマンドリファレンス](docs/commands.md)
- [対応言語一覧](docs/languages.md)

## ライセンス

MIT Licenseです。詳細は[LICENSE](LICENSE)を参照してください。Portable Releaseの同梱物は、各アーカイブのルートにある`THIRD_PARTY_NOTICES.md`と`licenses/`で確認できます。ソース配布物の依存関係については[第三者通知](THIRD_PARTY_NOTICES.md)を参照してください。
