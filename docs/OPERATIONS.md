# ローカル実行・導入前の手順

この文書は運用手順です。HTTP user serviceは `192.168.0.120:16150` で起動・有効化済みです。後続のユーザー承認で毎時生成timerを有効化し、Pico新 `main.py` に切り替えました。詳細は[転送記録](PICO_TRANSFER.md)を参照してください。

## 依存と単体テスト

Python 3とPillowが必要です。依存は `requirements.txt` を参照してください。リポジトリ直下から次を実行します。

```bash
python3 -m unittest discover -s tests -v
```

## 母艦の設定と能力確認

母艦は `AI_NEWS_STATE` に履歴・DBを保存し、`AI_NEWS_BIND` と `AI_NEWS_PORT` でHTTPのbind先を決めます。既定は `192.168.0.120:16150` です。起動前に既存リスナー、停止中のunit、Nginx設定も確認して競合を避けます。8080は使用しません。

追加のアプリ認証キーは不要です。LAN内からブラウザ閲覧とPico取得ができます。HTTPとSHA-256は真正性を保証せず、LAN内からのアクセスや改ざんに対する保護はありません。WANへ公開せず、既存のOS/firewall/サービスの保護を変更しません。Wi-Fi秘密はGit、ログ、画像、生成指示へ書きません。

生成ジョブ `python3 -m ai_news.generator` は既定でCodexヘッドレスGPT-6-Lunaの画像ツールを使い、実PNGを検証して保存します。Codex CLI 0.160.0で単発画像生成と、一時領域でニュース調査から履歴公開までの通し試験1回を確認しました。継続稼働と利用枠は未検証です。`AI_NEWS_IMAGE_COMMAND` で明示指定する別コマンド方式もあり、`--capabilities` と `--generate PROMPT_JSON OUTPUT_PNG` を要求します。画像が実在し、PNG/RAW検証を通るまで公開しません。外部有料APIへ自動切替しません。

一次出典に公開日しかない場合、`source_published_at` は `YYYY-MM-DD` のまま保存し、`source_date_precision` を `date` とします。時刻とタイムゾーンを推測しません。一次出典とHN投稿日時は別に記録します。

`python3 -m ai_news.server` は保存済み画像だけを配信します。GETで生成はしません。配信 `/v1/latest`、不変RAW/PNG、status、閲覧画面 `/` と `/gallery/` は同じ設定ポートです。閲覧とPico取得に追加のアプリ認証はありません。

user unitテンプレートは `systemd/` にあります。HTTP serviceと生成timerはこのraspi5の開発パスと状態ディレクトリで導入済みです。`systemctl --user status ai-news-http.service ai-news-generate.timer` と `systemctl --user list-timers ai-news-generate.timer` で状態を確認できます。生成サービスは25分の起動期限、各UTC時間スロットの冪等処理、排他ロックを備えます。

## Pico

`pico/config.py` のHOST/PORTは母艦と一致させます。`pico/secrets.example.py` の空欄はWi-Fi SSIDとパスワードの2項目だけです。ユーザー設定済みのローカル `pico/secrets.py` は上書きしません。本人が本体へ転送し、秘密値を表示せずWi-Fiと母艦HTTPの疎通を確認しました。新 `main.py` で正規RAWを取得・描画し、ユーザーが実画面で確認しました。旧版は本体内 `main_legacy.py` と非公開バックアップに残しています。

Pico W/Pico 2 Wの機種・firmware版、SPI1 SCK GP10/MOSI GP11とRST12/DC8/CS9/BUSY13の導通を確認してください。通常full更新のみ実装しています。四隅、外周、非対称矢印で回転・極性・paddingを実機確認するまで表示成立とはしません。BUSY期限、無線停止、時限wake、電流、モバイルバッテリー停止時のPC USB給電も実測します。

## 障害と復旧

生成失敗・候補なし・同一ニュースでは前回latestを維持します。Picoは受信・長さ・hash検証失敗時にClearや描画をしません。描画開始後のBUSY/SPI失敗は表示不確定と扱い、連続更新しません。再起動でRAM表示状態が不明なら完全なRAWを再取得してfull更新します。

履歴のPNG/RAW/manifestは自動削除しません。旧版に戻す際は生成タイマーを止め、DBとlatestの整合バックアップを取って、実行物とschemaの互換性を確認してください。作品archiveを上書き・削除しないでください。
