# 実装仕様（2026-10-03）

この文書は、[実装前の設計書v0.1](AIニュース風刺画端末_要件定義とシステム設計_v0.1.md)から変更した点を、現在のコードに合わせて記録するものです。挙動の最終的な根拠は `ai_news/`、`pico/`、`systemd/` の実装です。実機や定時運転で未確認の項目は下記に明記します。

## 母艦と公開

- `systemd/ai-news-generate.timer` は `OnCalendar=hourly`、`Persistent=false` です。毎時00分に生成serviceを起動し、停止中の回を後から自動実行しません。serviceは30分で打ち切ります。`Archive` のUTC時間slot一意制約と排他lockで、同一slotの再生成を防ぎます。
- 生成ジョブはまず画像バックエンドの利用能力を確認し、Hacker Newsの新着最大24件を読み、48時間以内の候補を最大12件に絞ります。Codex headless `gpt-6-luna` が一次出典の公開日と内容を検証し、風刺の構想をJSONで返します。公開日が日付のみなら時刻を作らず日付精度を記録します。候補なし、適切な新ネタなし、既出の `event_key` は `SKIPPED`、生成・検証失敗は `FAILED` とし、どちらも既存latestを維持します。毎時必ず別画像に変わる仕様ではありません。
- 標準の画像バックエンドはCodexの内蔵画像生成ツールです。CLI機能とログインを検査し、Codex自身の最終メッセージまたはツールログに示された新規PNGを照合します。生成物を確認できない場合は同じ選定ニュース・同じ時間slot内で1回だけ再試行し、2回とも失敗すれば `FAILED` としてlatestを維持します。別コマンドは `AI_NEWS_IMAGE_COMMAND` で明示設定した場合のみ使い、有料APIへの自動切替はありません。
- 生成画像をPillowで250×122の1bit PNGに正本化し、物理122×250、stride16、MSB先、白1・黒0、行末padding白のRAW4000へ変換します。PNG、RAW、メタデータを `state/archive/<frame_id>/` に永続保存し、`state/published/latest.json` をatomic writeで切り替えます。出典、事実、風刺構想、モデルとバックエンド情報もフレームのJSONに残します。`state/` はGit管理外です。
- HTTPは `ai-news-http.service` が `192.168.0.120:16150` にbindし、LAN `192.168.0.0/24` の送信元だけを受け付けます。`GET /v1/latest`、`/v1/frames/<frame_id>.raw`、`.png`、`/v1/status`、ギャラリーを配信します。`/v1/status` は `has_frame`、`frame_id`、`server_time` だけを返し、生成ジョブの状態は返しません。POSTは405です。GETで生成は開始しません。
- アプリの追加認証キー、HMAC署名、telemetry APIは実装していません。HTTPとSHA-256は通信相手の真正性を保証しません。Picoは受信長・形式・SHA-256・RAW paddingを検証します。母艦はLAN専用の構成ですが、OS firewall等の設定変更は行っていません。

## Pico Wと表示

- Picoは起動直後に180秒 `lightsleep` してから、Wi-Fi接続と `/v1/latest` の取得を始めます。manifest内のUTC `server_time` を時刻合わせのヒントに使い、通常は毎時20分付近に次回wakeを設定します。時刻が使えなければ3600秒を使います。待機は最大60秒ずつの `lightsleep` で、早期復帰しても予定時刻まで再待機します。再起動後の表示状態は不明として、最初の正常取得時に全画面を描画します。
- Wi-Fi接続上限20秒、TCP connect 3秒、socket readごとに最大10秒、wake全体120秒、BUSY最大60秒です。通信上の `OSError` とtimeoutは同一要求を1回再試行します。HTTP 404、形式・長さ・hashの不正は描画せず次のwakeへ進みます。認証、`Retry-After` による待機、404後のmanifest再照会はありません。
- 最新manifestの `wire_sha256` がRAM内の前回表示hashと同じならRAW取得とパネル初期化を省きます。ただし前回描画から24時間以上経つと保守全画面更新を行います。描画間隔180秒のガードがあります。Picoのflashへ表示状態のjournalは保存していないため、再起動後は再取得・再描画します。
- 完全なRAWを取得・検証した後にWi-Fiを停止し、そこで初めてV4パネルを初期化します。通常の全画面更新だけを使い、BUSY解除を待ってパネルをsleepさせます。取得失敗時にClearしません。BUSY/SPI失敗後の画面は不確定です。
- `pico/secrets.py` はGit対象外で、SSIDとパスワードは本人が本体へ転送しました。旧Pico `main.py` は本体の `main_legacy.py` とリポジトリ外の非公開バックアップに退避しました。リポジトリ内の `pico/main.py` は現行プログラムです。

## 実証と未確認

Codex headlessの画像生成を一時領域で通し、実PNGから正本PNG・RAWを作りました。承認後にその作品を本番latestへ公開し、HTTP取得とhashを確認しました。Picoは新プログラムで取得・描画し、ユーザーがロボットの風刺画の実画面表示を確認しました。22:00 JSTの初回定時ジョブは画像ログのツール名検査で失敗しました。同時刻に新しいPNGファイルは作られましたが、当時のコードはCodex最終メッセージを保存せず、ジョブとの確実な対応付けはできません。以後の誤判定を防ぐため、上記の照合と再試行を追加しました。単体テストは16件成功しています。

毎時timerによる初回の定時生成から次回Pico更新までの一連の動作、非対称パターンによる画面全域の方向・端画素、低電力時の電流とバッテリー寿命、72時間の継続運転は未確認です。時刻や稼働状況の最新値は `systemctl --user`、HTTP `/v1/latest` と `state/` で確認してください。
