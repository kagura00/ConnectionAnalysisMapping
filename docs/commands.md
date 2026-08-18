# コマンドリファレンス

## 呼び出し方

portable版は、portableフォルダーをカレントディレクトリにしてlauncherを実行する。

```powershell
cd C:\path\to\connection-map-portable
launcher\connection-map.cmd <command> [options]
```

Linux/macOS:

```sh
cd /path/to/connection-map-portable
./launcher/connection-map.sh <command> [options]
```

ソースアーカイブのPython環境では、ソースアーカイブを展開したディレクトリで`uv run connection-map`を使う。local modeで導入済みの対象リポジトリでは、対象リポジトリの`.connection-map/analyzer/run.py`を使う。

```powershell
uv run connection-map <command> [options]
uv run python .connection-map\analyzer\run.py <command> [options]
```

`uv run connection-map <command> --help`または`uv run python .connection-map/analyzer/run.py <command> --help`で、その環境の全オプションを表示できる。以下の例はソースアーカイブの`uv run connection-map`で示す。portable版では同じ引数をlauncherへ渡し、local modeでは`uv run python .connection-map/analyzer/run.py`へ渡す。

## 言語設定キー

設定ファイルの`language`と`languages`では、次のキーを使う。

| 表示名 | 設定キー | 表示名 | 設定キー |
| --- | --- | --- | --- |
| Python | `python` | HTML | `html` |
| CSS | `css` | JavaScript | `javascript` |
| TypeScript | `typescript` | C | `c` |
| C++ | `cpp` | Java | `java` |
| C# | `csharp` | Go | `go` |
| Rust | `rust` | PHP | `php` |
| Ruby | `ruby` | Kotlin | `kotlin` |
| Swift | `swift` | Bash | `bash` |
| POSIX Shell | `posix-shell` | PowerShell | `powershell` |
| Dart | `dart` | Scala | `scala` |
| MySQL | `mysql` | PostgreSQL | `postgresql` |
| SQLite | `sqlite` | SQL Server / T-SQL | `sqlserver` |
| Oracle | `oracle` | VB.NET | `vbnet` |
| VBA | `vba` | Lua | `lua` |
| Haskell | `haskell` | Perl | `perl` |
| MATLAB | `matlab` | COBOL | `cobol` |
| FORTRAN | `fortran` | R | `r` |
| Objective-C | `objective-c` | CUDA C/C++ | `cuda` |
| Groovy | `groovy` | F# | `fsharp` |
| Assembly | `assembly` | HCL | `hcl` |
| GDScript | `gdscript` | Elixir | `elixir` |
| Zig | `zig` | Julia | `julia` |
| Delphi / Object Pascal | `pascal` | Erlang | `erlang` |

`web`、`c-family`、`shell`、`sql`、`mixed`、`all`はプリセットである。`mixed`は`languages`に個別キーを列挙し、`all`は全言語を選択する。

SQLの`language = "sql"`は、選択される5製品の方言で同じ`.sql`ファイルを解析する。特定製品だけを対象にする場合は`mysql`、`postgresql`、`sqlite`、`sqlserver`、`oracle`のいずれかを指定する。製品名を含む拡張子（例: `.mysql.sql`）も対象を絞る。

追加言語群では、構文木の利用可否と関係抽出の診断を解析結果に記録する。構文解析器が利用できない環境や構文エラーがあるファイルは、抽出できた範囲だけを表示する。

## 解析・表示

### `analyze`

リポジトリを解析して解析結果JSON（Graph Contract v1）を作成する。

```powershell
uv run connection-map analyze `
  --root C:\path\to\repository `
  --config C:\path\to\config.toml
```

portable版での同じ操作:

```powershell
launcher\connection-map.cmd analyze `
  --root C:\path\to\repository `
  --config C:\path\to\config.toml
```

Linux/macOSのportable版:

```sh
./launcher/connection-map.sh analyze \
  --root /path/to/repository \
  --config /path/to/config.toml
```

| オプション | 内容 |
| --- | --- |
| `--root PATH` | 解析対象。省略時は現在のフォルダー |
| `--config PATH` | TOML設定。省略時は既定設定 |
| `--output PATH` | 出力JSON。central workspaceでも指定可能 |
| `--workspace PATH` | central workspaceの保存先 |
| `--deterministic` | 時刻など変動するmetadataを省略 |
| `--allow-empty` | ノード0件の解析を許可 |
| `--include-tests` | 設定のtest_patternsを含める |
| `--exclude-tests` | 設定のtest_patternsを除外する |

`CONNECTION_MAP_WORKSPACE`を設定している場合もcentral workspaceになる。portable launcherは自動で`data/`を指定する。

既定では生成物、カバレッジ出力、テスト用フォルダー、`*.test.*`、`*.spec.*`を解析対象から除外する。設定ファイルで変更できる。

### `serve`

解析結果またはバンドルをローカルWebサーバーで公開する。

```powershell
uv run connection-map serve --input C:\path\to\repository\.connection-map\snapshots\analysis.json `
  --bundle C:\path\to\repository\.connection-map\snapshots\graph-bundle
```

| オプション | 内容 |
| --- | --- |
| `--input PATH` | 解析JSON |
| `--bundle PATH` | 静的バンドルのディレクトリ |
| `--layout PATH` | レイアウトJSON |
| `--host HOST` | bind先。既定は`127.0.0.1` |
| `--port PORT` | ポート。既定は`8765` |
| `--workspace PATH` | central workspaceの保存先 |

直接出力モードでは、`analyze`、`validate`、`split`、`validate-bundle`の順に実行してから`serve`を実行する。central workspace（複数リポジトリ用のデータ領域）では`analyze`時にbundleが作成されるため、`--input`と`--bundle`を省略して`serve --workspace PATH`を実行できる。

## 検証・分割

### `validate`

```powershell
uv run connection-map validate C:\path\to\repository\.connection-map\snapshots\analysis.json
```

解析JSONの形式、node、edge、識別子、関係の整合性を検証する。

### `split`

解析JSONを遅延読み込み用バンドルへ分割する。

```powershell
uv run connection-map split C:\path\to\repository\.connection-map\snapshots\analysis.json `
  --output C:\path\to\repository\.connection-map\snapshots\graph-bundle
```

| オプション | 既定値 | 内容 |
| --- | ---: | --- |
| `--node-chunk-size` | 2000 | ノードチャンクの件数 |
| `--edge-chunk-size` | 5000 | 接続チャンクの件数 |
| `--diagnostic-chunk-size` | 2000 | 診断チャンクの件数 |
| `--search-chunk-size` | 5000 | 検索チャンクの件数 |
| `--force` | 無効 | 既存の非空バンドルを更新する |

### `validate-bundle`

```powershell
uv run connection-map validate-bundle C:\path\to\repository\.connection-map\snapshots\graph-bundle
```

bundleのindex、chunk、参照、digestを検証する。

### `search`

解析JSONまたはbundleを検索する。

```powershell
uv run connection-map search C:\path\to\repository\.connection-map\snapshots\analysis.json service --limit 20
```

`--limit`で結果数を制限する。

### `report`

解析結果の件数と解決状況をJSONで出力する。

```powershell
uv run connection-map report --input C:\path\to\repository\.connection-map\snapshots\analysis.json
uv run connection-map report --input C:\path\to\repository\.connection-map\snapshots\analysis.json `
  --output C:\path\to\repository\report.json
```

## 手動情報

### `validate-manual`

```powershell
uv run connection-map validate-manual `
  --input C:\path\to\repository\.connection-map\layout\manual-v1.json `
  --analysis C:\path\to\repository\.connection-map\snapshots\analysis.json
```

manual overlayの形式を検証する。`--analysis`を指定すると対象解析との参照整合性も確認する。

### `merge`

manual overlayを解析結果へ適用した派生JSONを作成する。

```powershell
uv run connection-map merge `
  --analysis C:\path\to\repository\.connection-map\snapshots\analysis.json `
  --manual C:\path\to\repository\.connection-map\layout\manual-v1.json `
  --output C:\path\to\repository\.connection-map\snapshots\analysis-with-manual.json
```

解析結果のhashが合わない場合は通常エラーになる。意図的に再適用する場合だけ`--ignore-analysis-hash`を使う。

## local modeの導入・更新

以下の`init`、`install-core`、`rollback-core`は、ソースアーカイブを展開したディレクトリで実行する。

### `init`

対象リポジトリへ`.connection-map/`の雛形を作成する。

```powershell
uv run connection-map init --root C:\path\to\repository
```

`--install-dir`でディレクトリ名を変更できる。通常の再実行は既存ファイルを保持する。`--force`は生成用`.gitignore`と`layout/.gitkeep`だけを更新し、`config.toml`、`analyzer/`、`layout/`の利用者ファイルは保持する。すべての雛形を置き換える必要がある場合だけ、`--force --force-all`を指定する。`--force-all`は自動バックアップを作成しないため、実行前に必要なファイルを手動で保存する。

### `install-core`

ソースアーカイブを対象リポジトリへ導入または更新する。

```powershell
uv run connection-map install-core --root C:\path\to\repository `
  --archive C:\path\to\connection_analysis_mapping-<version>.tar.gz
```

`core/`だけを更新し、解析器、設定、レイアウト、解析結果を保持する。`--install-dir`で対象ディレクトリを変更できる。

### `rollback-core`

直前のcoreのバックアップへ戻す。

```powershell
uv run connection-map rollback-core --root C:\path\to\repository
```

`--install-dir NAME`で対象ディレクトリを変更できる。`--backup NAME`でバックアップディレクトリを指定でき、指定しない場合は最新のバックアップを使う。

local modeの導入後は、対象リポジトリで`uv run python .connection-map/analyzer/run.py`を実行入口にする。`--install-dir NAME`を初回導入で指定した場合は、以降のパス中の`.connection-map`を`NAME`に読み替える。

## 関連文書

- [インストール手順](installation.md)
- [操作方法](operation.md)
- [対応言語一覧](languages.md)
