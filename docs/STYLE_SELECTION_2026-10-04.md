# 題材と作画テイストの独立選択

この文書は、稼働中の母艦へ適用した16画風選択機能を記録する。16種類の初期テイストの表示名と指示全文は[`config.sample`](../config.sample)の`[styles]`を正本とする。Picoのプログラムと正本latestはこの改訂では変更していない。

## 設定とWeb

- `config`の`[news].selected_topic_id` / `[[news.topics]]`と独立して、`[styles].selected_style_id` / `[[styles.items]]`に安定ID・表示名・作画指示を保存する。旧configに`[styles]`がない場合は新聞風刺画1件を既定として動く。初期候補をWebへ表示するには、既存の実configの`[news]`・`[image]`・`[push]`を維持したまま、`config.sample`の`[styles]`節だけを追記する。
- `GET /v1/topics`と`GET /v1/styles`は各リクエストで実configを読み直す。設定画面も開くたびに両APIへ`cache: no-store`でアクセスし、名前・指示・追加・削除・並び順を更新する。表示には`textContent`を使う。題材とテイストは別のradio group・別の保存ボタン・別の未保存表示を持つ。
- `POST /v1/topics`と`POST /v1/styles`はそれぞれ選択IDだけを書き換え、TOML全体の構造を比較して他の設定が変わらないことを確かめてから原子的に保存する。既存のHost・Origin・同一origin・専用ヘッダー・JSONの確認は両経路で同じ。選択IDが候補から消えたらAPIは`selection_valid=false`と選択値`null`を返し、画面は再選択を促す。生成の手動開始は`invalid_topic`または`invalid_style`として止める。

## 生成と保存

- ジョブ開始時、選んだ題材とテイストのID・表示名・指示文を、既存SQLiteの`jobs`開始記録と同じトランザクションで`job_selections`へ保存する。再開時は同じslotの保存値を使うため、configの選択・順序・本文が変わっても進行中ジョブの題材・画風は変わらない。旧版で開始され保存値を持たない未完了ジョブは、改訂版の初回再開時に現在の設定から一度だけ記録する。
- ニュース調査（Codex call 1）は従来どおり題材指示・出典URL・文章のみ。テイストは画像生成（call 2）だけに渡す。画像の共通契約は250×122、1bitに変換して読める大きな白黒形、少ない文字、事実や引用の創作禁止、実PNG必須を維持する。固定の「常に風刺画」指示は置かず、選択テイストに合わせる。既存作品のキャラクターや構図の複製は禁止する。
- Codex画像生成が選択テイストを拒否した場合、別テイストへ黙って切り替えない。同じ保存済みテイストで既存の再試行を行い、失敗が続けばジョブ失敗として旧latestを維持する。正規PNG・RAW・Pico PUSH形式は変更しない。公開メタデータに選択題材・テイストを保存する。

## 本番適用と確認範囲

母艦の生成timerとHTTP serviceを短時間停止し、稼働checkoutのコード、実config、SQLite DBを非公開の`/tmp/ai-news-style-live-backup-20261004T232017Z`へ退避した。実configの既存`[news]`・`[image]`・`[push]`を構造比較で保持し、`[styles]`だけを追記した。本番の選択値は題材`quirky_news`、テイスト`newspaper_cartoon`。最終コードで55件のテストが成功した後、HTTP serviceと毎時timerを再開してactiveを確認した。本番の`/settings/`、`/v1/topics`、`/v1/styles`はHTTP 200を返し、実Chromeでも題材11件・テイスト16件を表示した。

Picoファイルと正本latestには触れていない。隔離で生成した画像は本番履歴に登録していない。実Pico画面への新画風反映は次回以降の本番生成で確認する。

## 隔離での検証結果

- `python3 -m unittest discover -s tests -q`: 55件成功。localhostソケットを使うテストは必要な権限を付けて実行した。
- Web設定画面はブラウザで11題材・16テイストの描画、両選択の未保存表示、片方だけ保存した際の独立性、未保存の題材を再読込して保存値へ戻す操作、両選択の保存、候補の追加・並び替え・指示文変更・選択削除を確認した。新しいページを開いた際も両GETが`cache: no-store`で発行され、編集後の候補と選択削除警告が表示された。ブラウザ側のAPIはモックを使用し、実HTTP APIの動作はlocalhost統合テストで別途確認した。
- ローカルChromeを隔離`FrameServer`の実HTTPページに直接接続する統合試験も成功。11題材・16テイスト表示、両未保存表示、テイストのみ保存して題材の未保存を保持、題材保存、未保存選択の再読込復帰、configへの新候補追加と指示変更の再読込反映、選択済み候補の削除警告を確認した。試験サーバ・Chromeは終了し、一時configのみ変更した。
- ユーザーの明示承認後、保存済みの出典URL・要約を用いたCodex headless `gpt-6-luna`の実画像生成を2画風で実施した。`shadow_play`は1695バイト、SHA-256 `1f58ecfc13f00c40ea2781489b106329c840be5cde9f5c8906ec7b71dfca7a70`、`toriyama_adventure`は2190バイト、SHA-256 `ab5a271ebcc1f22fb6917a56a839012f2f2e56e2fd7a5edd583dc9080a1f193f`。両方ともPillow検証で250×122・mode `1`のPNG。目視で異なる画風を確認した。出力は`/tmp/ai-news-style-actual-output/`だけに置き、本番公開・Pico送信はしていない。
