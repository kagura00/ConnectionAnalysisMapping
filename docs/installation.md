# インストール手順

## portable版（推奨）

portable版は、対象リポジトリに本システムのファイルや設定を追加せず、portableフォルダー内の`data/`に複数リポジトリの解析結果・設定・レイアウトを保存する方式。

### Windows

1. GitHub Releaseから`connection-map-portable-<version>.zip`を取得する。
2. 任意のフォルダーへ展開する。

```powershell
cd C:\path\to\connection-map-portable
launcher\connection-map.cmd analyze --root C:\path\to\repository
launcher\connection-map.cmd serve
```

### Linux/macOS

1. GitHub Releaseからportable zipを取得する。
2. 任意のフォルダーへ展開する。

```sh
cd /path/to/connection-map-portable
./launcher/connection-map.sh analyze --root /path/to/repository
./launcher/connection-map.sh serve
```

展開ツールが実行権限を保持しなかった場合は、`./launcher/connection-map.sh`の代わりに実行前に`chmod +x launcher/connection-map.sh`を一度実行する。

portable版の構成は次のとおり。

```text
connection-map-portable/
├── app/       # アプリ本体。更新時に置き換える
├── launcher/  # 起動スクリプト。更新時に置き換える
├── runtime/   # 同梱Python。更新時に置き換える
└── data/      # 解析結果・設定・レイアウト。更新時も保持する
```

portable版は同梱Pythonを優先して使用する。初回解析時に`data/registry.json`が作成され、解析したリポジトリが登録される。

## local mode

local modeは、対象リポジトリへ本システムの共通CLI・解析器・Web UIを`.connection-map/`として組み込む方式。対象リポジトリと一緒に設定や解析器を管理したい場合に使う。

必要なものはPython 3.11以上とuvである。次の例で`source`はソースアーカイブを展開したディレクトリ、`repository`は解析対象リポジトリを表す。`--archive`には取得したソースアーカイブ（`.tar.gz`）を指定する。portable zipは指定しない。

```powershell
cd C:\path\to\source
uv sync
uv run connection-map init --root C:\path\to\repository
uv run connection-map install-core --root C:\path\to\repository `
  --archive C:\path\to\connection_analysis_mapping-<version>.tar.gz

cd C:\path\to\repository
uv run python .connection-map\analyzer\run.py analyze `
  --root . --config .connection-map\config.toml
uv run python .connection-map\analyzer\run.py validate `
  .connection-map\snapshots\analysis.json
uv run python .connection-map\analyzer\run.py split `
  .connection-map\snapshots\analysis.json `
  --output .connection-map\snapshots\graph-bundle
uv run python .connection-map\analyzer\run.py serve `
  --input .connection-map\snapshots\analysis.json `
  --bundle .connection-map\snapshots\graph-bundle
```

Linux/macOSではパス区切りと行継続を置き換えて実行する。

```sh
cd /path/to/source
uv sync
uv run connection-map init --root /path/to/repository
uv run connection-map install-core --root /path/to/repository \
  --archive /path/to/connection_analysis_mapping-<version>.tar.gz

cd /path/to/repository
uv run python .connection-map/analyzer/run.py analyze \
  --root . --config .connection-map/config.toml
uv run python .connection-map/analyzer/run.py validate \
  .connection-map/snapshots/analysis.json
uv run python .connection-map/analyzer/run.py split \
  .connection-map/snapshots/analysis.json \
  --output .connection-map/snapshots/graph-bundle
uv run python .connection-map/analyzer/run.py serve \
  --input .connection-map/snapshots/analysis.json \
  --bundle .connection-map/snapshots/graph-bundle
```

初期化後は対象リポジトリの`.connection-map/`に`config.toml`と解析器の雛形が作成される。設定を確認してから解析する。

## 解析言語の指定

解析対象は設定ファイルの`language`で指定する。v1.0の言語も個別に指定できる。

```toml
[analysis]
language = "html"
```

複数言語を解析する場合は`mixed`と`languages`を使う。`web`、`c-family`、`shell`、`sql`は、関連する言語をまとめて選ぶためのプリセットである。

```toml
[analysis]
language = "mixed"
languages = ["python", "html", "typescript"]
```

対応言語をすべて解析する場合は`all`を指定する。

```toml
[analysis]
language = "all"
```

設定値は英小文字のキーで指定する。表示名と設定キーの全対応は[コマンドリファレンスの言語設定キー](commands.md#言語設定キー)を参照する。

portable版では、解析時に`--config PATH`でTOML設定を渡す。設定は初回解析後に`data/repositories/<repository-id>/config.toml`へ保存されるため、以後は登録済みリポジトリに対して`--config`を省略できる。`data/registry.json`は直接編集しない。

## 解析に必要な依存

Python解析は追加依存なしで動作する。HTML、CSS、JavaScript、TypeScriptなどの解析器はTree-sitter（ソースコードの構文を読み取るライブラリ）、SQL製品の解析器はSQLGlot（SQLを読み取るライブラリ）を使う。

`extra`は、uvで追加依存をまとめて導入するための名前であり、解析言語を指定する設定ではない。ソースアーカイブのPython環境では`uv sync --extra ...`で導入する。local modeで対象リポジトリ自身のPython環境を使う場合は、対象環境へ同じ依存を導入するか、生成された`analyzer/README.md`の`uv run --with ...`例を使う。

Java/C#/Kotlinの`classpath`・`source_roots`、C/C++の`compile_commands`、参照先の`references`は指定先を読み取る。対象リポジトリ外のパスも指定できるため、信頼できるファイルだけを設定する。解析器は参照先のコードを実行しない。

| 対象 | extra |
| --- | --- |
| HTML/CSS/JavaScript/TypeScript | `web` |
| C/C++ | `native` |
| Java/Kotlin | `jvm` |
| C# | `dotnet` |
| Go | `go` |
| Rust | `rust` |
| PHP | `php` |
| Ruby | `ruby` |
| Swift | `swift` |
| Bash/POSIX Shell | `shell` |
| PowerShell | `powershell` |
| Dart | `dart` |
| Scala | `scala` |
| VBA/Lua/Haskell/Perl/MATLAB/COBOL/FORTRAN/R/Objective-C/CUDA C/C++/Groovy/F#/Assembly/HCL/GDScript/Elixir/Zig/Julia/Delphi/Object Pascal/Erlang | `tree-sitter` |
| VB.NET | `visual-basic` |
| SQL製品解析 | `sql` |

例:

```powershell
uv sync --extra web --extra native --extra tree-sitter --extra sql
```

SQLだけを解析する場合は`uv sync --extra sql`を使う。

`all`を指定する場合は、対象言語に必要なextraをすべて追加する。`all`専用のextraはない。

VB.NETの専用構文解析器はWindows x64でだけ導入される。Linux、macOS、Windows ARM64では専用依存を使わず、正規表現によるフォールバック解析を行う。

portable版の同梱ランタイムには追加パーサーを含めていない。portable版の`runtime/`へ直接依存を追加することは想定しない。portable版の`data/`を引き継ぎながら追加言語を解析する場合は、ソースアーカイブのPython環境からcentral workspaceを指定する。

次の設定ファイルを作成し、`language`と`languages`を対象に合わせて編集する。`include`と`exclude`を省略すると、選択した言語の既定パターンが使われる。

`C:\path\to\config.toml`を作成し、次の内容を保存してからコマンドを実行する。

```toml
[analysis]
language = "all"
```

```powershell
cd C:\path\to\source
uv sync --extra web --extra native --extra jvm --extra dotnet --extra go --extra rust --extra php --extra ruby --extra swift --extra shell --extra powershell --extra dart --extra scala --extra tree-sitter --extra visual-basic --extra sql
uv run connection-map analyze `
  --root C:\path\to\repository `
  --workspace C:\path\to\connection-map-portable\data `
  --config C:\path\to\config.toml
uv run connection-map serve `
  --workspace C:\path\to\connection-map-portable\data
```

Linux/macOSでは次のように実行する。extra名はOSによらず同じである。

```sh
cd /path/to/source
uv sync --extra web --extra native --extra jvm --extra dotnet --extra go --extra rust --extra php --extra ruby --extra swift --extra shell --extra powershell --extra dart --extra scala --extra tree-sitter --extra visual-basic --extra sql
uv run connection-map analyze \
  --root /path/to/repository \
  --workspace /path/to/connection-map-portable/data \
  --config /path/to/config.toml
uv run connection-map serve \
  --workspace /path/to/connection-map-portable/data
```

この方法で生成したデータはportable版のlauncherからも表示できる。Pythonだけならportable版のlauncherをそのまま使える。

## 更新方法

### portable版

1. `serve`を停止する。
2. 新しいzipの`app/`、`launcher/`、`runtime/`を既存フォルダーへ置き換える。
3. 既存の`data/`は削除・置換しない。

`data/registry.json`の旧メタデータは起動時に自動更新される。更新前のレジストリは`data/backups/`へ保存される。

### local mode

```powershell
cd C:\path\to\source
uv run connection-map install-core --root C:\path\to\repository `
  --archive C:\path\to\connection_analysis_mapping-<version>.tar.gz
```

Linux/macOS:

```sh
cd /path/to/source
uv run connection-map install-core --root /path/to/repository \
  --archive /path/to/connection_analysis_mapping-<version>.tar.gz
```

更新対象は`.connection-map/core/`だけである。自動バックアップの対象も更新前の`core/`だけであり、解析器、設定、レイアウト、解析結果は保持される。設定や解析結果を別の場所へ複製する場合は手動で行う。

この節の例は既定の`.connection-map`を使う。初回導入時に`--install-dir NAME`を指定した場合は、解析・表示・更新・ロールバック・アンインストールでも`.connection-map`を`NAME`に読み替える。

## ロールバック

local modeの直前のcoreへ戻す場合:

```powershell
cd C:\path\to\source
uv run connection-map rollback-core --root C:\path\to\repository
```

Linux/macOS:

```sh
cd /path/to/source
uv run connection-map rollback-core --root /path/to/repository
```

portable版は、更新前のportableフォルダーをバックアップしてから置き換える。`data/`だけを残してアプリ側を戻す場合は、対応するランタイムとappの組み合わせを確認してから行う。

## アンインストール

- portable版: `serve`を停止し、portableフォルダーを削除する。解析結果を残す場合は先に`data/`を別の場所へコピーする。
- local mode: 対象リポジトリの`.connection-map/`を削除する。削除前に`config.toml`、`analyzer/`、`layout/`、`snapshots/`が必要か確認し、残す場合は別の場所へコピーする。`--install-dir NAME`を使った場合は`NAME/`を削除する。

## 関連文書

- [操作方法](operation.md)
- [コマンドリファレンス](commands.md)
- [対応言語一覧](languages.md)
