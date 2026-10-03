# Pico 転送記録（2026-10-03）

ユーザーが接続済みPicoへのプログラム転送を承認した範囲で実施しました。

## 接続と保護

- USBで識別された対象は1台のRP2040。最初はBOOTSEL状態で、既存フラッシュのMicroPython v1.22.1を確認しました。
- UF2書込みやフラッシュ消去は行わず、既存フラッシュ全体をリポジトリ外のアクセス制限付き場所へ退避し、読出し検証しました。
- MicroPython通常起動後、既存 `main.py` と `e_paper.py` を個別に非公開退避し、端末上のハッシュと照合しました。ファイル内容、Wi-Fi認証情報はログ・文書・Gitへ出していません。

## 転送したファイル

| リポジトリ | Pico上 | 状態 |
| --- | --- | --- |
| `pico/protocol.py` | `/protocol.py` | 転送・再起動後SHA-256照合済み |
| `pico/panel_v4.py` | `/panel_v4.py` | 転送・再起動後SHA-256照合済み |
| `pico/config.py` | `/config.py` | 転送・再起動後SHA-256照合済み。母艦ポート16150 |
| `pico/main.py` | `/news_main.py` | 転送・再起動後SHA-256照合済み。自動起動しない別名 |

既存 `/main.py` と `/e_paper.py` は転送前後で一致しています。Pico上で新4ファイルの構文検査、`protocol` のRAW4000配列自己テストを通過しました。

追加認証キー廃止に伴い、更新した `protocol.py` と `news_main.py` を再転送しました。以前の転送版はリポジトリ外に非公開退避し、更新版をPico上のSHA-256で照合し、構文検査を通しました。既存 `/main.py` と `/e_paper.py` は再転送後も退避物とハッシュ一致しました。`secrets.py` は読み取り・転送していません。

## 起動切替と秘密情報

ユーザー指定でアプリ認証キーは不要となり、残るPico側の秘密設定はWi-Fi SSIDとパスワードだけです。ユーザーが本人操作で `pico/secrets.py` をPico本体へ転送し、SHA-256照合の成功を報告しました。Codexは秘密値を読み出し・表示・転送していません。新プログラムを本体上で手動実行し、Wi-Fi接続と母艦HTTP、正規RAW取得を確認しました。新 `main.py` へ安全に切り替えた後、ユーザーが実画面で風刺画の表示を確認しました。旧 `main.py` は本体内 `main_legacy.py` と非公開バックアップに保持しています。

最初の新ドライバーは表示指示後にBUSY期限切れとなりました。ユーザー指定の `/home/tane/plutopg/Raspberry_Pi_PICO/picoW/e-paper/e-paper_monitor/pico/e_paper.py` とPicoから退避した既存ドライバーはSHA-256一致し、既存V4ドライバーで同じRAWの表示成功を確認しました。新ドライバーのSPI1初期化とGP8 DC設定順を既存実装と一致させ、1バイトごとのCS送信で再試験したところ `init`、`display`、`sleep` がすべて成功しました。

MicroPython通常USB接続（BOOTSELボタンなし）で、本人がWi-Fi設定を転送するにはプロジェクト直下で `python3 pico/upload_secrets.py --port /dev/ttyACM0` を実行します。ツールはPicoのUSB IDを照合し、既存の本体秘密ファイルがあれば上書きを拒否します。転送時に秘密値を表示せず、SHA-256を照合します。本人がこのコマンドを実行済みです。
