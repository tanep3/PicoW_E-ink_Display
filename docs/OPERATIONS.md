# ローカル運用手順

この文書は現行コードの導入と運用手順です。このraspi5ではHTTP user serviceを `192.168.0.120:16150` で運用し、毎時生成timerが稼働checkoutを読みます。実装前の設計との差分は[実装仕様](IMPLEMENTED_SPEC.md)、Picoの転送履歴は[転送記録](PICO_TRANSFER.md)を参照してください。

## 新しい環境への初期導入

以下は新規導入用です。既に動いている母艦やPicoの設定ファイルを、再導入のために上書きしないでください。母艦にはPython 3.11以降と認証済みのCodex CLIが必要です。Codexのヘッドレス実行で`gpt-6-luna`のWeb調査と画像生成を使えることが前提です。画像生成能力はジョブ自身が確認し、利用不可なら公開しません。

リポジトリ直下でPython依存を用意します。`config`は任意で、初期値を変える場合だけサンプルから作成します。

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
- 生成serviceの`PATH`に認証済み`codex`コマンドのディレクトリを含める。user serviceの実行ユーザーでCodexが使える必要があります。

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

ニュース調査はWeb全体を対象とし、1回目の必須成果は公開出典URLと裏付けのある要約または本文です。過去24時間という条件はCodexの調査指示で判断し、日付の必須出力やコード上の時間判定は行いません。調査成果は `state/news/<UTC slot>.json` に保存され、画像段階だけを再開できます。画像段階はその保存文章と出典URLから風刺画を作ります。画像失敗時に同slotのニュース選定を繰り返しません。

ルート`config`はTOMLです。[config.sample](../config.sample)と同じ値を配置できます。`[news]`と`[image]`の`retry_count=2`は初回を除くため各段階最大3試行です。`interval_seconds=0`は即時再試行、`deadline_seconds=0`は追加の段階上限なしを意味します。`attempt_timeout_seconds`は1試行あたりニュース180秒、画像300秒で、正の整数です。他の項目は負でない整数です。既存の`config`があれば意図しない上書きをしません。ファイルはGit管理外です。すべての試行が制限までかかる場合はニュース9分＋画像15分＝24分で、画像能力確認は最大約45秒です。service全体は30分のままで、その他の処理時間・設定による待機が増えれば最大試行数より前に止まり得ます。処理中断からの再開では消費済み試行回数を使い、設定した段階の経過時間上限は再起動後に計り直します。

`python3 -m ai_news.server` は保存済み画像だけを配信します。GETで生成はしません。配信 `/v1/latest`、不変RAW/PNG、status、閲覧画面 `/` と `/gallery/` は同じ設定ポートです。閲覧とPico取得に追加のアプリ認証はありません。

user unitテンプレートは `systemd/` にあります。HTTP serviceと生成timerはこのraspi5の開発パスと状態ディレクトリで導入済みです。`systemctl --user status ai-news-http.service ai-news-generate.timer` と `systemctl --user list-timers ai-news-generate.timer` で状態を確認できます。timerは毎時00分、`Persistent=false` です。生成サービスは30分の起動期限、各UTC時間スロットの記録、排他ロックを備えます。調査と画像の各段階は、必要な成果物がない場合に既定で最大2回再試行します。実際の定時結果は `journalctl --user -u ai-news-generate.service` と `state/jobs.sqlite3` で確認します。

## Pico

`pico/config.py` のHOST/PORTは母艦と一致させます。`pico/secrets.example.py` の空欄はWi-Fi SSIDとパスワードの2項目だけです。ユーザー設定済みのローカル `pico/secrets.py` は上書きしません。本人が本体へ転送し、秘密値を表示せずWi-Fiと母艦HTTPの疎通を確認しました。新 `main.py` で正規RAWを取得・描画し、ユーザーが実画面で確認しました。旧版は本体内 `main_legacy.py` と非公開バックアップに残しています。

Pico上で確認したMicroPythonはv1.22.1です。SPI1とRST12/DC8/CS9/BUSY13の既存V4表示経路で本番画像が表示されました。通常full更新のみ実装しています。SCK/MOSIの導通、四隅・外周・非対称矢印による画面全域の回転・極性・padding、連続wake、電流、モバイルバッテリー停止時のPC USB給電は引き続き実測します。パネルBUSYには60秒のコード上の期限があります。

## 障害と復旧

ニュースまたは画像の全試行失敗時は前回latestを維持します。同じニュースの再採用は許容し、新たな画像生成を呼びます。Picoは受信・長さ・hash検証失敗時にClearや描画をしません。描画開始後のBUSY/SPI失敗は表示不確定と扱い、連続更新しません。再起動でRAM表示状態が不明なら完全なRAWを再取得してfull更新します。

履歴のPNG/RAW/manifestは自動削除しません。旧版に戻す際は生成タイマーを止め、DBとlatestの整合バックアップを取って、実行物とschemaの互換性を確認してください。作品archiveを上書き・削除しないでください。
