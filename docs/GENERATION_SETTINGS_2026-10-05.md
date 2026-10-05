# 生成設定と画像正本化

ニュースモデル、画像モデル、DPID λ、二値化閾値を `/settings/` で個別に保存する。画面を開くたびに実 `config` と `state/jobs.sqlite3` の値を読み直す。`config.sample` は初期値の見本であり、Git管理外の実 `config` を勝手に書き換えない。

| 項目 | config初期値 | 既定値 | 有効値 |
| --- | --- | --- | --- |
| ニュースモデル | `[news].model` | `gpt-6-luna` | Codex CLIのモデルID、空白なし、最大128文字 |
| 作画モデル | `[image].model` | `gpt-6-luna` | 同上 |
| DPID λ | `[image].dpid_lambda` | `0.75` | 有限の0〜1 |
| 二値化閾値 | `[image].threshold` | `128` | 整数1〜254 |

Webで保存した項目だけSQLiteの値を優先する。他の項目は、その時点の実 `config` を読む。`GET /v1/settings` は有効値・取得元・revisionを返す。`POST /v1/settings` は `{ "key": "threshold", "value": 140, "revision": 0 }` のように1項目を保存する。別画面の更新によってrevisionが変われば409を返し、画面は入力途中の値を保持して再読込を促す。無効値は400。POSTには他の設定POSTと同じLAN/Origin/専用ヘッダー制約がある。

λの0〜1は[pepedpid公式README](https://github.com/umzi2/pepedpid#%EF%B8%8F-arguments-for-dpid_resize)が示す「0付近で平滑、1付近で細部保持」という意味の範囲に基づく、このアプリの操作範囲である。推奨値としてREADMEが示すのは0.5で、アプリの既定0.75は6枚の比較から選んだ運用値。pepedpidの関数シグネチャは`l: float`であり、ライブラリ自体が0〜1を入力条件として強制しているという意味ではない。

新規ジョブは開始時に4値と題材・テイストをSQLiteへ固定し、再試行・再開時はその値を使う。手動生成は受付時に固定する。Webの変更は次のジョブから効き、公開済みPNGとRAWは再生成しない。旧SQLiteジョブは移行時にLANCZOS・閾値128・両モデル`gpt-6-luna`として保持する。

新規PNGは入力画像をグレースケール化し、縦横比を保って250×122以内へDPIDでサイズ変更し、残白の余白を付け、閾値で1bit化する。算出した縦横は最小1画素に固定する。既存のLANCZOS経路は小さい入力を拡大する契約であり、DPID経路もそれに合わせる。隔離環境のpepedpid 0.1.2で1×1、20×20からの拡大、1×10000・10000×1の細長い入力を別プロセスで試し、処理が終了して妥当な形の画像を返すことを確認した。λを上げると細線を残しやすいが、背景線や黒画素も増え得る。`pepedpid` 0.1.2は単一チャネル入力で失敗するため、同じ明度を3チャネルに複製して処理し、出力3チャネルの一致を確認する。結果のPNG・RAW4000の形式とPicoへの送信契約は従来どおり。DPIDまたはNumPyがない場合はニュース調査・画像生成前に明確に失敗し、LANCZOSへ自動切替しない。旧ジョブのLANCZOS再開にはDPID依存は不要。

公開metadataの`normalizer`には新ジョブの`method=dpid`、λ、閾値、`version=2`を記録する。旧ジョブは従来の`version=1`、閾値128の記録を保つ。モデルは`news_model`と`image_model`に記録し、各Codex CLIの実指定は`state/prompt_evidence/`で照合できる。

検証範囲: 隔離コピーで旧DB移行、DB競合、設定HTTP、毎時/手動のsnapshot、欠けた依存の生成前停止、RAW4000を単体テストした。gpt-6.1-sol実験原画6枚を使ったDPID λ0.75の最終PNGは比較実験とバイト一致した。実ブラウザーでも4項目の表示・保存・再読込・無効モデル・409競合を確認した。

2026-10-05、本番raspi5の専用venvへPillow・NumPy・pepedpid・pyserialを導入し、両user serviceをそのPythonへ切り替えた。実configの既存題材11件・画風16件・再試行値を保持し、不足していた4項目だけ追加した。既存ジョブ30行のSQLite移行後も行数は30で、`PRAGMA quick_check`は`ok`。移行直後のWeb/APIは両モデル`gpt-6-luna`・λ0.75・閾値128を表示。新venvで全93テストが成功した。ニュース→画像の実手動ジョブslot `-24`は両段階1試行でフレーム`f5d2f857b8f8d0be9e4ef32666c0a0b253e16068c29bcf5a65a274a57792143d-a1`を公開し、metadataに`method=dpid`、λ0.75、閾値128、両モデル`gpt-6-luna`を記録した。PNGは250×122の1bit、RAWは4000バイトでPNGからの変換結果と一致。Picoは`displayed` ACKを返した。今回の絵がパネルに見えることはまだ目視していない。以降のWeb保存値は次のジョブから適用され、過去のジョブmetadataは変わらない。
