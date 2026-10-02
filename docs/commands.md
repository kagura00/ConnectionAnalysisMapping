# コマンドリファレンス

`connection-map --version`で使用中のツールのバージョンを表示する。サブコマンドの指定は不要。

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
| `--fail-on-error` | 部分的な解析はJSON保存後に終了コード3。未指定時も警告を表示 |
| `--include-tests` | 設定のtest_patternsを含める |
| `--exclude-tests` | 設定のtest_patternsを除外する |

`CONNECTION_MAP_WORKSPACE`を設定している場合もcentral workspaceになる。portable launcherは自動で`data/`を指定する。

既定では生成物、カバレッジ出力、テスト用フォルダー、`*.test.*`、`*.spec.*`を解析対象から除外する。設定ファイルで変更できる。

### 外部利用の出力先

`analyze`・`investigate`・`context`は`--external-dir DIR`を指定できる。`context`では`--root PATH`も指定する。

| 指定 | 保存先と優先順 |
| --- | --- |
| `--external-dir DIR` | analyzeのworkspace、investigateのcache、grammar cacheをDIR配下へまとめる。個別CLI指定が優先し、このprofileは既存の環境変数・既定値より優先 |
| `--grammar-cache DIR` | analyze/investigateでTree-sitterのgrammar保存先を指定 |
| `analyze --workspace DIR` | profileのworkspace指定を上書き |
| `investigate --cache-dir DIR` | profileのinvestigation-cache指定を上書き |

実際の保存先と選択理由はstderrへ表示する。profileでは対象内・対象と同じ場所・対象を含む上位フォルダー・既存のsymlink/reparse経由の保存先を事前に拒否する。`analyze`の相対`--output`は従来どおり対象root基準なので、profileと併用する場合は対象外の絶対パスを使う。不正な個別指定はエラーになる。外部profileでは保存領域同士の重なりも拒否する。追加出力は、個別指定した保存領域とprofile配下のworkspace・investigation-cache・grammar-cacheの外へ置く。台帳や既存cacheの上書きを防ぐためで、profile直下のpacket.json等は利用できる。[外部利用の例](ai-context.md#対象の外へ結果とキャッシュをまとめる)も参照。

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
| `--root PATH` | 直接モードでソースの鮮度を比較するルート。central modeは登録先を使用 |
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

`report --root PATH`を指定すると、成果物の件数・解析範囲に加えて、現在の選択ソースとの追加・変更・削除を比較する。ルートを省略した場合は現在のソースを確認しない。

### `investigate`

現在のソースを解析・照合してから、対象と周辺の接続、ソース抜粋、関連テスト、未解決呼び出しの候補を小さなJSONで返す。解析JSONやノードIDの事前準備は不要。

```powershell
uv run connection-map investigate --root C:\path\to\repository --symbol Service.handle
uv run connection-map investigate --root C:\path\to\repository --file src/service.py --line 42
uv run connection-map investigate --root C:\path\to\repository --changed --base HEAD `
  --output C:\analysis\investigation.json
```

| オプション | 内容 |
| --- | --- |
| `--symbol NAME` | 完全修飾名、または一意な表示名。曖昧な場合はエラー |
| `--file PATH --line N` | リポジトリ相対パスと正の行番号。最小の包含宣言を選ぶ。行省略時はファイル内のcallableを対象とする |
| `--node ID` | 既知のノードID。他の選択方法と併用不可 |
| `--changed --base REF` | Gitルートで、commit/ref（既定HEAD）と現在のファイルを比較。staged・unstaged・untrackedを含む。他の選択方法と併用不可 |
| `--config PATH` | 明示的TOML。省略時は対象の`.connection-map/config.toml`、それもなければ拡張子から言語を選択 |
| `--language KEY` | 言語・プリセットを明示。対象のローカル設定を使わない。明示的`--config`と併用不可 |
| `--include-tests / --exclude-tests` | テストの包含を上書き。設定がなければ既定で含む。設定がある場合はその値を尊重 |
| `--cache-dir PATH` | 対象リポジトリ外のキャッシュ。既定はOS一時領域の`connection-map-investigations` |
| `--refresh` | キャッシュの照合結果にかかわらず再解析 |
| `--max-chars N` | 最後の改行を含むJSON文字数。2,048〜200,000、既定12,000。バイト数・トークン数ではない |
| `--max-targets N` | 複数宣言の対象数1〜20、既定8。省略数を返す |
| `--snippet-lines N / --no-snippets` | 宣言当たりの抜粋行数1〜80（既定24）、または抜粋を省略 |
| `--output PATH` | 保存先。省略時は標準出力へJSONのみ |

`--direction`、`--relation`、`--resolution`、`--depth`、`--max-nodes`、`--max-edges`も`context`と同じ範囲で使える。複数宣言は既定8件まで対象にし、省略数を返す。Git差分では各巡回で本番ファイルを先にし、ファイルごとに1宣言ずつ選ぶ。変更が内部のメソッドだけに収まる場合は、包含するクラスを重ねて選ばない。対象と品質情報だけでも指定文字数を超える場合はエラーとなる。

既定出力は問い合わせ結果全体のグラフではない。`counts`と`truncation`で省略を確認し、必要に応じて対象・方向を絞るか予算を増やす。削除だけの変更や未対応ファイルは現在の宣言へ対応づけられない。対象0件なら理由付きのエラーとなり、一部を対応づけられた場合も削除・未対応部分は`changes`と`selection`に残す。削除前の依存関係を復元する機能はない。

### `context`

`search`で得たノードIDの周辺を、根拠・未解決情報・解析範囲・鮮度付きJSONとして返す。画面も同じ問い合わせを使う。

```powershell
uv run connection-map context --input C:\analysis\analysis.json `
  --node "python:app.py:main:function" `
  --direction both --relation calls --depth 1 --max-nodes 60 --max-edges 120 `
  --root C:\path\to\repository --output C:\analysis\context.json
```

例のパス・ノードIDは対象に合わせて変更し、IDは`search`の結果から取得する。`--relation`は繰り返し指定できる。省略時は包含を除く。`--resolution`の既定は`all`、ほかに`resolved`・`external`・`unresolved`・`unsupported`を指定できる。深さは0〜5、ノード上限は1〜500、接続上限は1〜1000。上限や指定深さによる省略は`truncation`に記録する。詳細は[人とAIの接続探索](ai-context.md)を参照。

既存解析を小さな調査JSONへ変換する場合は`--compact --max-chars 12000`を追加する。`--snippets`も付ける場合は`--root`を渡す。古い・未照合の解析に現在のソース本文は付けない。自動的な再解析には`investigate`を使う。従来の`context`出力形式は維持する。

### 調査HTTP API

直接モードは`/investigate`、central modeは`/api/repositories/{id}/investigate`。例: `/investigate?symbol=Service.handle&max_chars=12000`。`node`または`symbol`または`file`・`line`で選択する。方向・関係・解決状態・件数・深さに加え、`max_chars`、`max_targets`、`snippet_lines`、`snippets=true/false`を指定できる。

HTTPは既存解析を使い、対象ルートは起動設定・登録先から決める。再解析やGit差分の取得は行わない。古い解析は`freshness.status=stale`として返し、ソース抜粋を付けない。未知・重複・範囲外の引数は400で拒否する。

### 診断HTTP API

`serve`の直接モードでは`/diagnostics`、central modeでは`/api/repositories/{id}/diagnostics`を使用する。例: `/diagnostics?severity=error&file=login&offset=0&limit=200`。

- `severity`: `all`（既定）、`error`、`warning`、`info`。
- `file`: ファイル名の部分一致。大文字・小文字を区別しない。
- `offset`: 0以上の整数。`limit`: 1〜200、既定200。
- 応答: `analysis_sha256`、`diagnostics`、`total`（絞り込み後）、`total_all`、`next_offset`、`severity_counts`。エラー、警告、情報の順に並べる。

ブラウザーに読み込んだチャンク数にかかわらず全診断を対象とする。未知・重複パラメーターや範囲外の値は400で拒否する。

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
