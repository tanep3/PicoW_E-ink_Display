# AIニュース風刺画端末

毎時、AIニュースから一つ選び、小さな白黒の風刺画にします。Raspberry Pi 5が絵と出典を保存・公開し、Pico Wが新しい絵を電子ペーパーに表示します。ブラウザでは作品をカレンダーから振り返れます。

## できること

- Codexがニュースの出典URLと文章を調べ、その内容から風刺画を生成します。出典や実画像を確認できないときは再試行し、失敗時は前回の絵を残します。
- Waveshare Pico-ePaper-2.13 V4に250×122の白黒画像を表示します。Pico Wは起動直後と毎時20分ごろに母艦から最新版を取得し、新しい絵があれば画面を更新します。
- 画像と出典を履歴に保存し、LAN内のブラウザで月間カレンダーと日別の作品一覧を閲覧できます。

## 必要なもの

- Raspberry Pi 5（Ubuntu）、Python 3.11以降、[Python依存](requirements.txt)のPillow。
- 認証済みのCodex CLIと、ヘッドレス実行で利用できる`gpt-6-luna`のWeb調査・画像生成機能。画像生成機能が使えなければ公開を止めます。外部有料APIへの自動切替はありません。
- MicroPythonを入れたPico W、Waveshare Pico-ePaper-2.13 V4、Wi-Fi、USB給電。確認済みのPico環境はMicroPython v1.22.1です。消費電力とバッテリー寿命は未測定です。

HTTPはLAN内専用で、追加のアプリ認証はありません。現行コードは送信元を`192.168.0.0/24`に制限するため、別のサブネットで使う場合はサーバ側の許可範囲も変更してください。WANには公開しないでください。

## はじめる

1. 母艦でこのリポジトリを用意し、Python依存とCodex CLIを使えるようにします。必要なら[config.sample](config.sample)をルートのGit管理外ファイル`config`へコピーして、ニュース・画像生成の再試行と期限を調整します。
2. 母艦でHTTPサーバとuser systemdの毎時生成timerを有効にします。最初の絵をすぐ作りたい場合は生成ジョブを一度起動します。
3. [pico/config.py](pico/config.py)の`HOST`と`PORT`を母艦のLANアドレス・HTTPポートに合わせます。[pico/secrets.example.py](pico/secrets.example.py)からGit管理外の`pico/secrets.py`を作り、Wi-Fi設定を入力して現行プログラムをPicoへ転送します。Picoは起動時に最新版を取得し、その後は毎時20分ごろに確認します。

各コマンド、Picoへの転送順、unit内のパスとアドレスの変更箇所は[初期導入と運用手順](docs/OPERATIONS.md)にまとめています。systemdの配布unitはこの開発環境のパスとアドレスを含むため、導入先に合わせて編集してください。

## ふだんの使い方

LAN内のブラウザで`http://<母艦のLANアドレス>:16150/`を開きます。毎時の生成結果と出典はギャラリーに残ります。新しい画像ができなかった回は前回の表示を維持します。Picoの電源を入れ直すと、次の定時時刻を待たずに最新版を取得します。

## 詳細

- [運用・障害復旧](docs/OPERATIONS.md)
- [現行の実装仕様](docs/IMPLEMENTED_SPEC.md)と[生成フローの設計改訂](docs/DESIGN_REVISION_2026-10-04.md)
- [ブラウザギャラリー](docs/GALLERY.md)と[検証記録](docs/VALIDATION.md)
- [要件定義・初期設計 v0.1](docs/AIニュース風刺画端末_要件定義とシステム設計_v0.1.md)

## ライセンス

[MIT License](LICENSE) © 2026 Tane Channel Technology
