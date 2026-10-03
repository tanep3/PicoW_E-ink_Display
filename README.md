# AIニュース風刺画端末

Raspberry Pi 5（Ubuntu）でAIニュースを題材に風刺画を生成し、Pico W と Waveshare Pico-ePaper-2.13 V4 に表示する実装です。別クライアント向けの月間ブラウザアーカイブも備えます。

## 現在の状態

母艦、Pico、閲覧画面と単体テストを実装しました。**現行動作は[実装仕様](docs/IMPLEMENTED_SPEC.md)とコードを正とします。** [要件定義・設計書v0.1](docs/AIニュース風刺画端末_要件定義とシステム設計_v0.1.md)は実装前の原本であり、変更前の案も含みます。追加閲覧機能は[ギャラリー仕様](docs/GALLERY.md)、Picoへの転送状況は[転送記録](docs/PICO_TRANSFER.md)を参照してください。HTTP user serviceと毎時生成timerは起動・有効化済みで、検証済み実画像を本番latestへ公開しました。Pico新プログラムでの実画面表示も確認しました。定時生成から次回Pico描画までの連続運転は未確認です。

## 稼働構成

- 母艦: raspi5 / Ubuntu / `192.168.0.120`。毎時の user systemd timerがCodexヘッドレス（GPT-6-Luna）の生成ジョブを起動します。新ネタなし・重複・失敗時は画像を変更しません。
- 表示端末: Pico W / DHCP 予約 `192.168.0.172`。母艦 HTTP `192.168.0.120:16150` から最新版を取得します。
- DHCP サーバ: `192.168.0.160`。
- パネル: Waveshare Pico-ePaper-2.13 V4。横長 `250×122` の1bit PNGを母艦の履歴正本とし、表示転送には `4000` バイトの RAW を使います。

ブラウザ閲覧は `http://192.168.0.120:16150/` です。保存画像をJSTの月間カレンダーと日別時系列で表示します。検証用画像は正規latestと分離し、画面に明示します。250×122の画像をpixelatedで拡大します。`16150` はユーザー指定で、母艦の `AI_NEWS_PORT` と [`pico/config.py`](pico/config.py)で変更できます。8080はNginx用のため使用しません。

画像生成バックエンドは分離しており、利用不可ならジョブはFAILEDとなり公開しません。Codex CLI 0.160.0／GPT-6-Lunaのヘッドレス画像ツールを単発検証し、ニュース調査から実画像・履歴公開まで一時領域で通し試験を1回成功させました。継続運転は未検証です。外部有料APIへの自動切替はありません。

## 秘密情報

Wi-Fi 認証情報を Git に保存しないでください。Pico 側の空欄サンプルは [`pico/secrets.example.py`](pico/secrets.example.py) です。必要項目は `WIFI_SSID` と `WIFI_PASSWORD` だけで、接続先は [`pico/config.py`](pico/config.py) にあります。ユーザーが置いた `pico/secrets.py` の内容は表示・上書きしていません。

アプリはユーザー指定によりLAN内専用・追加認証キーなしです。母艦は `192.168.0.120` にbindし、送信元を `192.168.0.0/24` に制限します。HTTPの通信内容とSHA-256は認証されません。LAN内の他端末から閲覧・改ざんされ得るため、WANへ公開しないでください。OS、firewall、既存サービスの設定は変更していません。

## 実行と検証

`requirements.txt` のPillowを使います。単体テストは `python3 -m unittest discover -s tests -v` です。母艦とPicoの設定、画像生成能力ゲート、稼働確認は[運用手順](docs/OPERATIONS.md)を参照してください。user systemdのunitは `systemd/` にあります。Picoの新 `main.py` は自動起動中で、元の無関係なプログラムは本体内 `main_legacy.py` とリポジトリ外の非公開バックアップに保持しています。Wi-Fi設定は本人が転送し、Codexは秘密値を読み出していません。

ロボットの風刺画の実画面表示と、修正したV4ドライバーのinit/display/sleepを確認しました。非対称テストパターンによる全画面方向・端の画素検証、次回以降の毎時wake、消費電力、バッテリー寿命、長期運転は未測定です。モバイルバッテリーが停止する場合は PC USB 給電での確認が必要です。

## ライセンス

[MIT License](LICENSE) © 2026 Tane Channel Technology
