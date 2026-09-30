# 人とAIが接続を調べるための改善方針と使い方

## 改善の順序

| 順序 | 課題 | 実装 | 確認方法 |
| --- | --- | --- | --- |
| 1 | 名前一致を確定した接続と誤認する | lexical profileの呼び出しを未解決の候補として扱い、別ファイルやLuaの引数・代入による隠蔽を除外 | 正例、同名引数、ローカル変数、再代入、importのない別ファイルの回帰テスト |
| 1 | Pythonの公開窓口を経由すると実装への接続が切れる | 明示的なimportの再公開・別名・相対importを追い、import元の位置を根拠に記録 | 再公開の連鎖、循環、再代入、条件付きimport、同名引数 |
| 2 | 古いグラフでも検証に通る | 選択ソースのSHA-256と設定を保存し、ファイル追加・変更・削除を比較 | 同じcommitのままの編集、解析中の編集、旧形式、対象ルート不在 |
| 3 | AIへ全グラフを渡す必要がある | CLIとローカルHTTPで共用する範囲指定の問い合わせ | 方向、深さ、循環、件数上限、未解決フィルター、CLI/HTTP一致 |
| 4 | 全体図から関数の関係を追いづらい | 周辺グラフ、接続元・先一覧、戻る、根拠とソース位置、AI用JSON保存 | 実リポジトリで検索→選択→接続先→戻るを確認 |
| 5 | 注釈が保存で消える | 注釈・追加属性・未読込ノードの座標を保持し、manual注釈も詳細欄へ表示 | 読み込み→座標編集→保存の往復テスト |

Graph Contract v1は維持し、ソース記録と解析範囲は`meta.extensions`に追加する。既存のJSONは表示できるが、ソース記録がない場合の鮮度は`unknown`となる。

## CLIで必要な範囲を取り出す

```powershell
uv run connection-map analyze --root C:\path\to\repository `
  --include-tests --fail-on-error --output C:\analysis\analysis.json
uv run connection-map search C:\analysis\analysis.json main
uv run connection-map context --input C:\analysis\analysis.json `
  --node "python:app.py:main:function" `
  --relation calls --direction both --depth 1 `
  --root C:\path\to\repository --output C:\analysis\context.json
```

例のパス・関数名・ノードIDは対象に合わせて変更し、`search`で得たIDを`context --node`へ渡す。IDの推測や表示名の一致による対象選択は行わない。対象ソースの構文を読めるPythonで実行する。対象アプリケーションやその依存パッケージをimport・実行する必要はない。

- 方向: `in`は呼び出し元など、`out`は呼び出し先など、`both`は両方。
- 関係: `--relation`は複数回指定できる。省略すると`contains`以外。包含を調べる場合は明示する。
- 深さ: 0〜5段、既定1段。
- 件数: `--max-nodes`は1〜500（既定60）、`--max-edges`は1〜1000（既定120）。
- 解決状態: `--resolution resolved`等で絞り込める。既定は未解決や外部も含める。
- 診断とmanual注釈は各50件まで。省略は`truncation`に記録する。

CLIは解析JSONを入力にする。バンドル表示中の画面もサーバーにある同じ解析JSONへ問い合わせる。HTTPは直接モードの`/context`・`/quality`、central modeの`/api/repositories/{id}/context`・`quality`。ソースのルートは起動オプションまたは登録済みリポジトリから決め、HTTPパラメーターから任意のルートを受け取らない。

Pythonでは、たとえば`from services import db as dbsvc`から`services/db/__init__.py`の明示的な再公開をたどり、`dbsvc.get_not_response()`を実装へ結ぶ。`detail.resolution_evidence`に経由したimportのファイルと位置を記録する。循環、再代入、条件付きの再公開は未解決のまま残す。`__all__`や`__getattr__`の実行結果は推測しない。

既定では`.uv-cache`と`.uv-cache-local`も除外する。既存の設定ファイルで`exclude`を独自指定している場合は、その設定が優先されるため必要に応じて追加する。

## 結果を判断する順序

1. `freshness.status`を確認する。`stale`は再解析が必要。`unchecked`はルート未指定、`unknown`は旧データや読み取り不能など。`current`は、問い合わせ時点で、保存された設定により選んだソースの内容とファイル一覧が一致したことを示す。
2. `coverage`を確認する。`partial`、エラーのあるファイル、ノードが得られなかったファイル、テストの包含、parser不在を確認する。`completed`は処理完了を表し、関係の網羅性を保証しない。
3. `truncation`を確認する。`budget_limited`は件数上限、`depth_limited`は指定段数の先にも接続があることを示す。深さを増やす前に対象・方向・関係を絞る。
4. 各接続の`resolution_status`・`provenance`・`source_file`・`source_span`を確認してから、変更するソースを読む。

鮮度確認は選択ソースと、web解析が読み込んだTypeScript設定を対象とする。TypeScript設定の照合有無は`typescript_context_verified`で確認できる。その他のビルド設定、外部classpath、実行時設定、DB、他リポジトリの状態は検証しない。保存された解析設定と異なる設定で使う場合も再解析する。`validate`と画面上部の「ファイル整合性OK」は成果物の整合性だけを意味する。

`confidence`は確率ではない。追加言語のlexical profileでは、同じファイル内の一意な名前一致も`unresolved`とし、`detail.resolution_basis = lexical_name_candidate`と`candidate_target_id`で候補であることを明示する。候補への線は確定した呼び出しの証拠として使わない。Luaの隠蔽検査は候補を除く保守的な検査であり、完全なスコープ解決ではない。golden fixtureでも`expected_edges`と`expected_candidate_edges`を分けて検証する。

## 画面で調べる

関数を検索して選ぶと、その関数の周辺を表示する。左側で方向・関係・深さ・解決状態を変更し、右側の接続元・先から移動する。「接続の根拠」で出典と位置を確認し、「戻る」で直前の対象へ戻れる。「全体を表示」は従来の概要へ戻る。

「AI用JSONを保存」は保存時に改めて問い合わせ、同じ範囲のJSONをダウンロードする。「ソース位置をコピー」はエディターへ渡せる`相対パス:行番号`をコピーする。「ソースの変更を確認」は解析を実行せず、現在のソースとの比較を更新する。

周辺図の座標は一時表示で、全体図の保存座標とは分ける。レイアウト保存はダウンロードであり、サーバーのファイルを上書きしない。注釈と拡張属性を保持し、layoutおよびmanualのノード・接続・全体注釈を詳細欄へ表示する。

問い合わせはブラウザーの未読込チャンクに影響されない。サーバーでは完全な解析JSONを読み、直近2件のグラフをメモリーに保持するため、非常に大きい解析はサーバー側のメモリーが必要となる。単純な静的ホスティングには問い合わせAPIがなく、従来のチャンク表示になる。

診断は全解析結果を対象にエラーを優先し、200件ずつ表示する。重大度とファイル名で絞り込み、前後のページへ移動できる。静的ホスティングでは一度に最大8チャンクを読み、未読込分があれば明示する。全体での絞り込みと並べ替えにはローカルサーバーを使用する。

## Webサンプルの再レビューに基づく改修計画と実装

| 優先 | 問題 | 実装と確認する性質 |
| --- | --- | --- |
| 1 | メソッド名だけで別のメソッドや自分自身へ接続する | JS/TSの字句スコープ、明示的import・再公開、変数初期値とreceiverを追う。同名引数、再代入、計算プロパティの書き換えは確定しない。根拠となる宣言位置を保存する |
| 1 | DOMセレクターが他ページにも接続する | HTMLから読み込むモジュール、Angular ComponentFixtureと宣言済みtemplateUrlで検索範囲を限定する。範囲が不明なものは未解決にする |
| 2 | 診断の後続チャンクにあるエラーを見落とす | 完全な解析JSONを検索する診断API、重大度・ファイル絞り込み、ページ移動を追加する |
| 2 | TypeScript aliasを外部依存と誤分類する | リポジトリ内のtsconfig、相対extends、references、baseUrl、pathsを読み、設定のSHA-256も鮮度照合する。設定循環や対象外パスは診断する |
| 3 | AngularとHTTPの接続、SCSSの欠落が見えない | templateUrl、単純なイベント呼び出し、Angular由来のinject、Expressのリテラルルート登録を抽出する。HTTP照合は候補、SCSS/Sass/Lessは未対応一覧として表示する |

web解析は`tsconfig*.json`を自動発見する。対象を限定する場合は解析用TOMLに次を追加する。

```toml
[context]
tsconfig = "tsconfig.app.json"
```

`context.tsconfig`はリポジトリ内のパスに限る。JSONCのコメントと末尾カンマを読める。複数設定で異なる接続先になる場合は確定しない。package由来のextends、ビルドツール固有のalias、完全なTypeScript型推論は未対応。対象アプリケーションのコード、設定スクリプト、Angularコンパイラーは実行しない。

Angularの`handles`はテンプレート要素からイベントハンドラーへ、`imports`はコンポーネントからHTMLへ接続する。`inject`はAngularからimportした関数であることを確認する。テンプレートはHTML構造と単純なイベント呼び出しだけを抽出し、制御ブロック、式全体、型の正当性は検証しない。このため`angular_template_partial`と`coverage.status = partial`を残す。エラー0件でもAngularの検証成功を意味しない。inline template、動的なメタデータ、テンプレート内の複雑なスコープは完全には扱わない。

HTTPの`registers`はExpressの登録箇所からハンドラーへ接続する。Angular HttpClientまたはfetchのリテラルなメソッド・パスが一致すると`requests`を追加するが、`unresolved`と`candidate_target_id`を維持する。origin、proxy、middleware、Routerのmount先、配備状態は別途確認する。

`coverage.unsupported_source_files`と`coverage.extraction_limitations`はAI用JSONにも入り、画面の解析範囲欄を開くと一覧を確認できる。SCSS/Sass/Lessの内容や依存関係は解析・鮮度照合しない。

## 複数リポジトリでの検証に基づく修正

Dartの定義はシグネチャと本体を同じスコープとして扱い、メソッド・入れ子関数を呼び出し元へ記録する。`async`とソース範囲も本体を含めて判定する。定義名には所属するクラス・関数名が入り、同名メソッドを区別できる。旧Dartグラフは再解析が必要で、旧IDを使うmanual overlayや保存済みレイアウトは新しいIDを確認して更新する。

Dartの呼び出しは大文字小文字を区別し、字句スコープ、`this`、静的メンバー、明示的な相対importとprefix・show・hideに基づいて解決する。引数、ローカル変数、ループ・catch・パターンによる名前の隠蔽を検査する。任意のオブジェクトの型や呼び出し結果の型は推論せず、同名メソッドへ確定接続しない。package URI、part、再公開、条件付き・遅延import、継承先の実装は完全には追わず、根拠のない接続は未解決に残す。`detail.expression`に受け手を含む式を、解決できた接続には`resolution_evidence`を保存する。

HTMLのinline script/styleは引き続きHTML要素として抽出する。内部のJavaScript/CSSを解析していないことを、ファイル・行番号付きの`extraction_limitations`に記録する。複数言語の結果を統合する場合も各解析器の制約を保持するため、エラー0件でも既知の未解析部分があれば`coverage.status = partial`となる。画面とAI用JSONの両方に制約を表示し、`--fail-on-error`指定時は成果物を保存した上で終了コード3を返す。

混在解析で拡張子が重なるファイルは、親の言語選択と内容に基づいて所属言語を決める。Objective-CのヘッダーをC++でも重複解析しない。意図的に複数方言を使うshell・SQLの既存動作は維持する。

既定の除外に`**/.dart_tool/**`と`**/flutter/ephemeral/**`を追加した。Swift/Kotlin/Pythonを含む混在プロジェクトでもFlutterのキャッシュと一時生成物を除外する。これらを明示的に調べる場合は、解析設定の`exclude`を指定してこの2パターンを外す。追跡済みのプラグイン登録コードは引き続き解析対象とする。

## C/C++の接続と名前空間

C/C++の呼び出しは、字句スコープ、引数・変数による名前の隠蔽、選択済みヘッダーの宣言、`static`と匿名名前空間の可視範囲を確認する。修飾名を捨てた末尾名の一致では確定しない。解決済みの辺は`detail.resolution_evidence`に完全修飾名、接続先ファイル、可視な宣言のノード・ファイル・位置を保存する。外部ヘッダーが選択範囲にない場合や、関数ポインター、任意の受け手の型、`using`宣言、引数の型に依存するoverloadなどを確定できない場合は未解決または外部として記録し、プロジェクト内の関数へ推測で接続しない。解析の完了やソースの一致は、実行時の呼び出し先が確定することを意味しない。

C++の値による直接初期化を関数宣言と区別し、曖昧な局所宣言を確定したcallableとして登録しない。`namespace a::b`は`a`と`a::b`の名前空間および包含関係を保持する。修飾定義の親を特定できない場合にも、名前の前半を切り捨てない。旧グラフは再解析し、修正された完全修飾名でIDが変わるノードのmanual overlayと保存済みレイアウトは、新しいIDを照合して更新する。

選択済みの基底クラスは継承を再帰して探索する。自クラスの宣言による隠蔽を優先し、未知の基底クラス、テンプレートの実体化が必要な継承、複数の継承経路で同名の宣言が見つかる場合は、同名のグローバル関数へ推測で接続しない。継承の仮想性や実体化・特殊化を完全には評価せず、確定できない呼び出しは未解決に残す。`inherits`の`detail.source_reference`は型引数を含む表記を保存する。テンプレートの型定義へ結ぶ継承辺も、実体化後のメソッドを確定した証拠にはならない。

`using`による型の別名と`typedef`は宣言位置と字句スコープを確認し、名前探索で型と判定できた`T(value)`は関数の呼び出し辺から除外する。非型テンプレート引数は宣言本体の値として扱い、関数ポインター・`auto`・パラメーターパックの呼び出しを同名の通常関数へ確定接続しない。テンプレートの型引数による型変換も呼び出し辺から除外する。変更前に保存した解析結果は再解析する。

マクロの展開、条件付きコンパイルの評価、プラグインテーブルのコールバック登録は完全には追わない。Tree-sitterの回復診断がある解析は`partial`を維持する。解析対象のプラグインをビルド・起動したことや、音声・映像・保存の実動作を確認したことにはならない。

## 残る範囲

Discordのイベント登録、動的なCog読み込み、オブジェクトへ後から付ける属性、コールバックなどは、静的な呼び出しだけではすべて追えない。根拠のあるmanual overlayで補い、実ソースと実行条件を確認する。各言語の型・スコープ解決の拡張、外部ビルド設定の鮮度、ソース本文プレビューは別の改善範囲とする。
