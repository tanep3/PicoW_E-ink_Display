> 本文に残るPico PULL手順は過去の運用記録です。現在は[PUSH設計・運用](PUSH_DESIGN_2026-10-04.md)に従って母艦からPicoへ送信します。旧転送コマンドをPicoへ再実行しないでください。

# ローカル運用手順

この文書は現行コードの導入と運用手順です。このraspi5ではHTTP user serviceを `192.168.0.120:16150` で運用し、毎時生成timerが稼働checkoutを読みます。実装前の設計との差分は[実装仕様](IMPLEMENTED_SPEC.md)、Picoの転送履歴は[転送記録](PICO_TRANSFER.md)を参照してください。

## 新しい環境への初期導入

以下は新規導入用です。既に動いている母艦やPicoの設定ファイルを、再導入のために上書きしないでください。母艦にはPython 3.11以降と認証済みのCodex CLIが必要です。Codexのヘッドレス実行で`gpt-6-luna`のWeb調査と画像生成を使えることが前提です。画像生成能力はジョブ自身が確認し、利用不可なら公開しません。

リポジトリ直下でPython依存を用意します。`config`は任意です。題材や初期値を変える場合はサンプルから作成します。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n config.sample config
```

母艦とPicoが同じLANにいることを確認し、母艦のLANアドレスを決めます。現行のHTTPコードは送信元を`192.168.0.0/24`に限定します。この範囲以外で使うには`ai_news/server.py`の許可範囲も変更する必要があります。HTTPの既定ポートは16150です。

`systemd/`の3つのunitを`~/.config/systemd/user/`へコピーし、コピー先の次の値を導入先に合わせて編集します。

- 両serviceの`WorkingDirectory`と`AI_NEWS_STATE`を実際のcheckoutと保存先の絶対パスにする。履歴を残す`state/`はGit管理外です。
- 両serviceの`ExecStart`を使用するPythonの絶対パスにする。上記venvを使うなら`<checkout>/.venv/bin/python -m ai_news.server`または`-m ai_news.generator`です。
- HTTP serviceの`AI_NEWS_BIND`を母艦のLANアドレスにする。ポートを変える場合は`AI_NEWS_PORT`も変更する。
- 生成serviceとHTTP serviceの`PATH`に認証済み`codex`コマンドのディレクトリを含める。Webからの手動生成はHTTP serviceの環境を引き継ぎます。user serviceの実行ユーザーでCodexが使える必要があります。

コピー先を編集した後に有効化します。生成timerは毎時00分に起動し、取り逃した回を遡って実行しません。

```bash
mkdir -p "$HOME/.config/systemd/user"
cp systemd/ai-news-http.service systemd/ai-news-generate.service systemd/ai-news-generate.timer "$HOME/.config/systemd/user/"
# コピー先のunitを編集してから:
systemctl --user daemon-reload
systemctl --user enable --now ai-news-http.service ai-news-generate.timer
```

最初の絵を今作りたい場合は`systemctl --user start ai-news-generate.service`を実行します。user systemdがログアウト後も動く設定かも確認してください。稼働と生成結果は後述の`systemctl --user`、`journalctl --user`、ブラウザの`http://<母艦のLANアドレス>:16150/`で確認します。

Pico WにはWaveshare Pico-ePaper-2.13 V4とMicroPythonを用意します。母艦のURLに合わせて`pico/config.py`の`HOST`と`PORT`を編集し、`pico/secrets.example.py`からGit管理外の`pico/secrets.py`を作ってWi-Fiの2項目を入力します。既存のPicoプログラムと秘密ファイルは先に別の場所へ退避してください。転送ツールの一例は`mpremote`です。接続ポートは各環境のUSBシリアルポートに置き換えます。

```bash
.venv/bin/python -m pip install mpremote
PICO_PORT=/dev/ttyACM0
.venv/bin/mpremote connect "$PICO_PORT" fs cp pico/protocol.py :protocol.py
.venv/bin/mpremote connect "$PICO_PORT" fs cp pico/panel_v4.py :panel_v4.py
.venv/bin/mpremote connect "$PICO_PORT" fs cp pico/config.py :config.py
.venv/bin/mpremote connect "$PICO_PORT" fs cp pico/secrets.py :secrets.py
.venv/bin/mpremote connect "$PICO_PORT" fs cp pico/main.py :main.py
```

`main.py`を最後に転送し、Picoを再起動します。起動直後に最新版を取得・検証してから描画し、以後は毎時20分ごろに確認します。描画後はUSB操作ができる状態で約3分待ってから省電力待機へ入ります。Picoへの実際の転送と実機確認の履歴は[転送記録](PICO_TRANSFER.md)を参照してください。

## 依存と単体テスト

Python 3.11以降とPillowが必要です。依存は `requirements.txt` を参照してください。リポジトリ直下から次を実行します。

```bash
python3 -m unittest discover -s tests -v
```

## 母艦の設定と能力確認

母艦は `AI_NEWS_STATE` に履歴・DBを保存し、`AI_NEWS_BIND` と `AI_NEWS_PORT` でHTTPのbind先を決めます。既定は `192.168.0.120:16150` です。起動前に既存リスナー、停止中のunit、Nginx設定も確認して競合を避けます。8080は使用しません。

追加のアプリ認証キーは不要です。LAN内からブラウザ閲覧とPico取得ができます。HTTPとSHA-256は真正性を保証せず、LAN内からのアクセスや改ざんに対する保護はありません。WANへ公開せず、既存のOS/firewall/サービスの保護を変更しません。Wi-Fi秘密はGit、ログ、画像、生成指示へ書きません。

生成ジョブ `python3 -m ai_news.generator` は既定でCodexヘッドレスGPT-6-Lunaの画像ツールを使い、実PNGを検証して保存します。Codex CLI 0.160.0で単発画像生成と、一時領域でニュース調査から履歴公開までの通し試験1回を確認しました。継続稼働と利用枠は未検証です。`AI_NEWS_IMAGE_COMMAND` で明示指定する別コマンド方式もあり、`--capabilities` と `--generate PROMPT_JSON OUTPUT_PNG` を要求します。画像が実在し、PNG/RAW検証を通るまで公開しません。外部有料APIへ自動切替しません。

Codexへ渡すニュース・画像プロンプトの全文は、新しい通常ジョブから`state/prompt_evidence/<slot>/<stage>-<試行番号>.json`に試行ごとに保存します。記録は権限制限付きでWebには公開せず、結果状態・時刻・モデル・成果物ハッシュも含みます。画風の表示だけでなく、実際のCLI入力を確認するときは[プロンプト証拠の手順](PROMPT_EVIDENCE_2026-10-05.md)を参照してください。Codex内部の画像生成ツールへ渡った最終指示とは区別します。

Web調査の題材は`config`の`[[news.topics]]`リストで定義します。各項目は変更しない安定した`id`、画面に出す`label`、Codexへの`prompt`を持ちます。初期データはAIニュース、発明、科学、文化、技術、個人工作、昔の未来予想、意外な事実、ライフハック、B級ニュース、観光地・絶景地の11件です。`config`を編集すれば候補の追加・削除・順序変更、名前と本文の修正ができ、次の設定画面読込と生成ジョブから反映されます。選択は`[news].selected_topic_id`にIDで保存し、初期値は`ai_news`です。リストの番号や表示名では識別しません。自動ローテーションはありません。実行中ジョブは開始時に読み込んだ題材を最後まで使います。

Webの「題材とテイストを選ぶ」画面は、`config`の`[[news.topics]]`と`[[styles.items]]`を開くたびに読み直して表示します。作画テイストの初期候補は16件です。題材の`selected_topic_id`と画風の`selected_style_id`は別々に保存し、候補や再試行値など他の設定を維持します。`config.sample`は初期例です。Webと直接編集の優先階層はなく、実`config`の最後の保存内容を使います。選択中IDの項目を削除した場合、勝手に別候補へ切り替えず、再選択を促します。ID重複、空リスト、不正TOMLも拒否します。ジョブ開始時に両選択のID・表示名・指示文をSQLiteへ保存するため、再試行や途中再開では同じ指示を使います。詳細は[作画テイスト選択](STYLE_SELECTION_2026-10-04.md)を参照してください。

1回目の必須成果は、実際に確認した公開出典URLと裏付けのある要約または本文です。題材に指定された時期はCodexが出典で確認しますが、日付の必須出力やコード上の公開日時判定はありません。公開成功時刻から遡る24時間の出典URLを公開履歴から調べ、Codexの調査指示に除外一覧として渡します。返されたURLもコードで比較し、重複なら理由を渡してニュース段階の既定最大3試行内で再試行します。時刻の基準は日本時間の暦日ではありません。画像失敗などで未公開のURLは除外せず、同じ話題でも別URLは許します。URLの比較は既存の形式正規化に従い、別URLを同一話題と推定しません。24時間ちょうど経過したURLは再採用できます。

調査成果は `state/news/<UTC slot>.json` に保存され、画像段階だけを再開できます。保存済みURLも再開時と公開直前に24時間重複を確認し、別のジョブが画像生成中に同じURLを公開した場合は今回の公開を止めて前回latestを維持します。このときニュースの追加試行は始めません。画像段階は保存した文章と出典URLから題材に合う絵を作ります。風刺が合わない題材は穏やかなユーモアや説明画とし、画像失敗時に同slotの題材選定を繰り返しません。

ルート`config`はTOMLです。[config.sample](../config.sample)と同じ形式を使います。`config`自体がない既存環境ではAIニュースの単一候補を既定値として扱います。`[news]`と`[image]`の`retry_count=2`は初回を除くため各段階最大3試行です。`interval_seconds=0`は即時再試行、`deadline_seconds=0`は追加の段階上限なしを意味します。`attempt_timeout_seconds`は1試行あたりニュース180秒、画像300秒で、正の整数です。他の数値項目は負でない整数です。既存の`config`があれば意図しない上書きをしません。ファイルはGit管理外です。すべての試行が制限までかかる場合はニュース9分＋画像15分＝24分で、画像能力確認は最大約45秒です。service全体は30分のままで、その他の処理時間・設定による待機が増えれば最大試行数より前に止まり得ます。処理中断からの再開では消費済み試行回数を使い、設定した段階の経過時間上限は再起動後に計り直します。

`python3 -m ai_news.server` は保存済み画像だけを配信します。GETで生成はしません。配信 `/v1/latest`、不変RAW/PNG、status、閲覧画面 `/` と `/gallery/` は同じ設定ポートです。閲覧とPico取得に追加のアプリ認証はありません。

user unitテンプレートは `systemd/` にあります。HTTP serviceと生成timerはこのraspi5の開発パスと状態ディレクトリで導入済みです。`systemctl --user status ai-news-http.service ai-news-generate.timer` と `systemctl --user list-timers ai-news-generate.timer` で状態を確認できます。timerは毎時00分、`Persistent=false` です。生成サービスは30分の起動期限、各UTC時間スロットの記録、排他ロックを備えます。調査と画像の各段階は、必要な成果物がない場合に既定で最大2回再試行します。実際の定時結果は `journalctl --user -u ai-news-generate.service` と `state/jobs.sqlite3` で確認します。

## Webからの手動生成

ブラウザの「新しい絵を生成」は空JSON本文の`POST /v1/generate`で受付け、別プロセスで既存の調査→画像→公開処理を実行します。「文章から直接作画」は同じPOSTへ`{"custom_text":"..."}`を送り、ニュース調査を省いて画像→公開処理を行います。カスタム文章は4000文字以内で、最後に受理した1件だけを`state/last_custom.json`へ保存し、`GET /v1/generate/custom-text`で次回の入力欄へ復元します。`GET /v1/generate/status`は進行・完了・失敗に加え、選択の欠落や不正configを示す安全な状態だけを返し、生成中もHTTPを占有しません。終了後、画面は最新画像とカレンダーを読み直します。秘密値やCodexの内部ログ・失敗詳細は返しません。手動処理の状態は`state/manual_status.json`に原子的に保存します。

ニュース手動ジョブは毎時ジョブと同じ題材設定とrolling24時間のURL除外を使います。カスタム手動ジョブは題材設定を書き換えず、URL除外を使いません。両方とも同じ排他ロック、画像再試行、公開処理を使い、毎回専用の負のslotを採番するため毎時slotを消費しません。受理時に選択した指示を固定します。別の生成が進行中なら手動要求を待ち行列に入れず`busy`とし、連打や複数タブからの起動も1件に抑えます。失敗時は旧latestを維持します。公開後のPUSH送信も共通です。

書込みAPIはLAN送信元制限に加え、母艦のHostと完全一致するOrigin、同一originのFetch Metadata、専用リクエストヘッダー、JSON本文を要求します。クロスoriginのWebページや単純なフォームPOSTからの意図しない開始・設定変更を拒否します。アプリの追加認証キーはありません。設定保存は`POST /v1/topics`、候補・現在値の確認は読取専用の`GET /v1/topics`です。既存の画像・履歴GETには生成副作用を加えていません。

## Pico

`pico/config.py` のHOST/PORTは母艦と一致させます。`pico/secrets.example.py` の空欄はWi-Fi SSIDとパスワードの2項目だけです。ユーザー設定済みのローカル `pico/secrets.py` は上書きしません。本人が本体へ転送し、秘密値を表示せずWi-Fiと母艦HTTPの疎通を確認しました。新 `main.py` で正規RAWを取得・描画し、ユーザーが実画面で確認しました。旧版は本体内 `main_legacy.py` と非公開バックアップに残しています。

Pico上で確認したMicroPythonはv1.22.1です。SPI1とRST12/DC8/CS9/BUSY13の既存V4表示経路で本番画像が表示されました。通常full更新のみ実装しています。SCK/MOSIの導通、四隅・外周・非対称矢印による画面全域の回転・極性・padding、連続wake、電流、モバイルバッテリー停止時のPC USB給電は引き続き実測します。パネルBUSYには60秒のコード上の期限があります。

## 障害と復旧

題材選定または画像の全試行失敗時は前回latestを維持します。過去24時間内の公開出典URLは再採用しません。Picoは受信・長さ・hash検証失敗時にClearや描画をしません。描画開始後のBUSY/SPI失敗は表示不確定と扱い、連続更新しません。現行PUSH版は再起動だけでは画像を取得・描画せず、次に母艦から届いた新しい送信を検証してfull更新します。

履歴のPNG/RAW/manifestは自動削除しません。旧版に戻す際は生成タイマーを止め、DBとlatestの整合バックアップを取って、実行物とschemaの互換性を確認してください。作品archiveを上書き・削除しないでください。

## 現行PUSH版

Picoは`192.168.0.172:16151`で待受し、母艦は履歴公開後にPOSTでRAW4000を送る。Git管理外の`pico/config.py`は`pico/config.sample.py`を見本に作成し、待受ポート、許可する母艦IP、固定IP、マスク、ゲートウェイ、DNSを定義する。固定IPはWi-Fi接続前に`wlan.ifconfig()`へ適用する。既存の`pico/secrets.py`は本人設定済みのWi-Fi情報として保護し、読み出し・上書きしない。Pico本体・母艦HTTPサービス・毎時生成timerはPUSH版へ切替済みで、Web送信から描画完了ACKまで実機確認した。母艦の実`config`には既存節を維持したまま画風の`[styles]`も追加済みで、Pico側の転送設定は変更していない。
