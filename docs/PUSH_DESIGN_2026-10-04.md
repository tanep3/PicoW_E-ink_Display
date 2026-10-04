# Pico PUSH改訂 — 実装と運用

この文書は2026-10-04に稼働checkoutとPico実機へ適用したPUSH版に対応する。Git管理外の母艦`config`とPico`config.py`はこの環境向けに設定済み。正式な初期設計v0.1と旧PULLの転送記録は履歴として保持する。

## 送信と表示

- 母艦のHTTPブラウザは従来どおり`192.168.0.120:16150`。Pico W / RP2040 / MicroPython v1.22.1は`192.168.0.172:16151`で`POST /v1/frame`を待つ。PicoからのHTTP GET、毎時wake、3分のUSB操作待機、周期的`machine.lightsleep`は新プログラムにない。Picoは接続維持中に`network.WLAN.PM_POWERSAVE`を指定し、`select.poll(1000)`で受信イベントを待つ。`poll`が内部でイベント待機を使うことと、CYW43/lwIP保守経路はMicroPython v1.22.1のソースを根拠とするが、短時間のLAN到達と描画完了ACKは実機で確認済み。長時間の到達性、待機電流・バッテリー寿命は未測定。
- 母艦は画像生成・正規PNG/RAW4000保存・latest切替を完了した後、独立したPUSHキューへフレームを登録する。Web手動生成の成功も同じ`run_once`の公開経路から登録する。PUSHの失敗は生成を失敗に戻さず、画像を再生成しない。公開後のキュー登録自体が失敗した場合は`push_registration_error.json`に公開番号・frame ID・失敗区分を原子的に記録し、`/v1/push/status`とWebで「公開済みだが送信登録失敗」と表示する。毎時timerの起動時刻や履歴slotをWebの作品表示では変更しない。
- カレンダー各作品の「PICOに表示」は、その作品のPUSHだけを登録する。公開済みフレームは保存RAWを使う。検証用作品は保存PNGを検証して一時RAWへ変換する。どちらもlatestや公開履歴を書き換えない。次の毎時生成で新しい作品が公開されたら自動PUSHする。
- 再起動・Wi-Fi再接続ではPicoは自動取得も自動描画もしない。通信不能でPUSHが最終失敗した後も再接続だけでは再送しない。次の生成公開またはWebの明示的な「PICOに表示」を待つ。

## 線上の契約と期限

母艦は`Content-Type: application/octet-stream`、`Content-Length: 4000`で正規RAWそのものをPOSTする。ヘッダーは`X-Push-Seq`（同一要求の再試行中は同じ整数）、`X-Frame-ID`（PNG SHA-256 + `-a1`）、`X-Wire-SHA256`、`X-Format-ID: epd122x250-msb-white1-v1`。Picoは送信元IPを`.120`に限定し、HTTPヘッダー2048バイト、RAW4000バイト、受信・描画全体60秒で打ち切る。全文受信後にハッシュ、形式、各行の白paddingを確認し、そこまでパネルに触らない。通常full更新の完了とパネルsleep後にのみ`200`の`displayed` ACKを返す。同じ送信番号・hashの再送には描画せず同じ成功ACK、古い番号や同じ番号の異なるhashには409を返す。成功番号とhashはPico flashの`push_state.json`へ保存する。

新しいPUSH要求はSQLiteに単調増加の整数番号を付けて保存する。時刻のマイクロ秒値と既存最大番号+1の大きい方を使い、DB復旧後にPicoの保存番号より小さい値へ戻りにくくする。母艦HTTP serviceの単一ワーカーが順に送る。新要求が古い要求の送信中に来ても、その物理描画は中断できない。完了・期限切れ後に新要求を送る。古い要求の次回再試行前には最新番号を検査し、新しい要求があれば古い再試行を中止する。Pico側も番号が古いものを拒否する。生成とPUSHの状態は別に公開し、`queued` / `sending` / `displayed` / `failed` / `superseded`を区別する。

`config.sample`の`[push]`はニュース・画像生成とは別の設定で、初回+2回の計3試行、失敗後30秒、送信開始から表示完了ACKまで1試行60秒、追加の全体期限0（無効）。全試行が上限までかかれば約4分。接続・本文送信・ACK待ちのsocket待機は各試行の残り時間を使い、独立した5秒期限では打ち切らない。Picoの本文受信も残りの60秒予算を使う。Webの表示では接続不可、時間切れ、データ検証、描画失敗、ACK消失など表示結果不明を区別する。詳細な例外は母艦ログに留め、秘密やstacktraceをWebに返さない。ACK消失時は表示済みか不明であり、「未表示」と断定しない。同じ番号の再送で確認する。

## 制約と移行順

- Picoが描画を始めた後のBUSY/SPI故障では、物理的な旧画像保持を保証できない。表示成功後からPicoの番号保存前に電源が落ちた場合も、再送で重複描画し得る。これは物理表示とflash記録を原子的に結合できないため。平常時の同ID再送は重複描画しない。
- 省電力モード中の長時間の受信到達性は未検証。短時間の実TCP接続と表示完了ACKは確認済み。電力0.15–0.3Wは仮試算で保証値ではなく、電流・バッテリー寿命は未測定。LAN内追加認証キーはないため、Picoは送信元IPを検査するが、これは暗号学的な認証ではない。
- このLANでは母艦がルーター`192.168.0.1`からDHCPリースを受け、Picoも試験中に予約先`.172`と異なる`.232`を取得した。Picoの非秘密`pico/config.py`をGit管理外の可変設定にし、`pico/config.sample.py`を見本として置く。PUSH版では`wlan.ifconfig((STATIC_IP, SUBNET_MASK, GATEWAY, DNS_SERVER))`を接続前に適用し、接続後のIPが設定値と違えば待受を開始しない。既定例はIP`.172`、/24、ゲートウェイ`.1`、DNS`.1`。DHCPサーバ`.160`、ゲートウェイ`.1`、DNSは異なる役割であり、`.160`をゲートウェイにしない。ルーターのDHCPを継続する場合は`.172`を配布範囲から除外または同じPico MACへ予約し、重複割当を防ぐ。ルーターとdnsmasqの設定変更は本コードの範囲外。
- 稼働checkoutの実`config`は編集せず、移行時に既存の`[news]`、`[[news.topics]]`、`[image]`と選択題材を保ったまま、以下だけ追記する。Webの題材保存も`[push]`を保持する実装にした。

```toml
[push]
host = "192.168.0.172"
port = 16151
retry_count = 2
interval_seconds = 30
attempt_timeout_seconds = 60
deadline_seconds = 0
```

切替時はPicoの既存プログラムを退避・SHA-256照合し、`protocol.py`、`panel_v4.py`、`config.py`、`push_receiver.py`を転送してから最後に`main.py`を切り替えた。PicoのWi-Fi MAC拒否が原因で初回は接続できず、PULL版へ切り戻した。ユーザーがWi-Fi拒否を解除した後に再転送し、固定IP`.172`への実TCPと描画完了ACKを確認してから母艦を切り替えた。母艦HTTPと毎時timerは稼働中。PM設定のコード経路は通過しているが、消費電流は未測定。

## 通常USB再接続での転送準備

ユーザーが再接続済みと伝えてからUSBシリアルを開く。現在のPUSH版は常時待受であり、USB REPLを使う転送は短時間の接続中断を伴う。作業開始前にポートをUSB VID/PIDでRP2040と照合し、対象が1台だけであることを確認する。BOOTSEL、UF2全体書換、フラッシュ消去は使わない。母艦の既存`pyserial`と過去に同じPicoで使ったraw REPL方式を使い、現行の`pico/transfer_push.py`に転送前バックアップ・ハッシュ検証・最終`main.py`切替・失敗時の切戻しを用意した。スクリプトで実機転送前バックアップ、ファイルごとのSHA-256照合、最後の`main.py`切替を確認済み。`mpremote`は見つからず、パッケージ取得もDNS到達不可だった。

再転送が必要な場合は、ユーザーの再接続連絡後に次の形で起動する。バックアップ先は毎回新しいリポジトリ外の私有ディレクトリにする。`--help`以外の起動は実機への書込みと再起動を伴う。

```bash
python3 pico/transfer_push.py --port /dev/ttyACM0 --backup-dir /home/tane/Documents/Codex/2026-10-03/task/pico-private-backup/push-new-unique-directory
```

1. 転送前にPicoの`/main.py`、`/protocol.py`、`/panel_v4.py`、`/config.py`をリポジトリ外のアクセス制限付きディレクトリに個別保存し、Pico上のSHA-256と退避ファイルのSHA-256を照合する。`/push_receiver.py`が既存なら同様に退避する。非公開`/secrets.py`は読み出し、表示、コピー、上書きしない。既存の`/main_legacy.py`と`/e_paper.py`も触らない。退避先はGit管理外とし、変更前後のファイル名・ハッシュのみ記録する。
2. 現行の`pico/protocol.py`、`pico/panel_v4.py`、`pico/config.py`、`pico/push_receiver.py`を対応するPicoルートへ転送し、端末上のSHA-256と照合する。`panel_v4.py`は現行とバイト単位で同一であり、`Panel.init/display/sleep`はいずれもdeadline引数を受けることを確認した。転送時もハッシュ照合する。転送途中は旧`main.py`を維持する。`pico/main.py`を最後に転送し、そのSHA-256も照合する。
3. Picoを通常再起動し、シリアルログでWi-Fi接続、待受ポート16151、省電力設定の例外なしを確認する。母艦からPicoへの接続を確認し、保存済みの別作品をWebで選んでPUSHする。`displayed` ACK、画面の実変更、同一要求の重複送信で再描画しないこと、次の新作品で更新できることを確認する。長時間のWi-Fi到達性と電力は別途実測する。
4. Picoが起動できない場合はUSB REPLで退避した4ファイルと必要なら`push_receiver.py`を元の状態へ戻し、`main.py`を最後に復旧する。新規作成した`push_receiver.py`だけは退避がなければ除去する。秘密ファイルと既存の無関係なファイルには触れない。母艦をPUSH版へ切り替えた後に戻す場合は生成timerを一時停止し、母艦コード・実`config`・user HTTP serviceも整合する旧PULL版へ戻してからtimerを再開する。保存済みPNG/RAWとlatestは維持する。
