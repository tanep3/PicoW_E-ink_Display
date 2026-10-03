# AIニュース風刺画端末

要件定義とシステム設計

**v0.1  |  2026年10月3日**

Raspberry Pi 5上の常駐処理が、1時間ごとにAI関連ニュースを調査し、独自の風刺画を生成する。確定した250 × 122画素の白黒1 bit PNGを履歴に残し、毎時起床するPico WがLANから最新の完成RAWフレームを取得して、Waveshare 2.13インチ電子ペーパーV4に表示する。

本書は、実装着手前に要件、責務、状態遷移、通信契約、障害時の整合性および受入条件を一括してレビューするための設計書である。インストール、実装、サービス登録、機器設定の変更は対象に含めない。

### 採用する基本方針

| 対象 | v0.1の方針 |
| --- | --- |
| 母艦 | Raspberry Pi 5／Ubuntu。HTTP配信はuser service常駐、生成はuser timer＋oneshot |
| 調査と生成 | Codex headlessを第一選択。調査・構成のモデル指定はGPT-6-Luna。画像生成機能は別の能力として検証する |
| 通信と表示 | Picoが毎時wakeしてPi 5からpull。完全検証後だけ更新し、パネルsleepとWi-Fi停止後に休眠する |
| 正常性の基準 | 母艦の公開、端末の取得、端末の表示を分ける。PNGと出典・生成履歴は自動削除しない |
| 導入の境界 | 同一LANのみ。WAN公開なし。外部の有料Image API等への自動切替なし |

### 実装開始前に通すゲート

- G1  Ubuntu／aarch64でCodex headless、指定モデル、Web調査、無人画像出力と利用条件を確認する

- G2  Pico機種・GPIO・V4描画を確認し、毎時wakeと実測待機電力が要件を満たす方式を決定する

- G3  RAW pull、応答認証、同一画像skip、電源断復旧と起床時間上限を検証する

本書の「確定」はユーザー指定または公式資料に基づく事項、「設計」は本版で選択した方式、「要検証」は導入前の受入条件を意味する。設計値は実測後に変更管理する。

## 1 要件とスコープ

| ID | 区分 | 要件 |
| --- | --- | --- |
| R01 | 確定 | 母艦はRaspberry Pi 5／Ubuntu。ユーザーsystemdでHTTP配信を常駐させ、毎時画像を事前生成する |
| R02 | 確定 | Picoは原則休眠し、1時間ごとにwake、LAN接続、最新画像の取得を行う |
| R03 | 確定 | WebとHacker NewsからAIニュースを選び、事実と風刺の創作を分離する |
| R04 | 確定 | Codex headless／GPT-6-Lunaを第一選択。画像生成能力は実装前に成立確認する |
| R05 | 確定 | 250 × 122画素の白黒1 bit PNGを正本とし、画像履歴として残す |
| R06 | 確定 | Pi 5は192.168.0.120。PicoはDHCPサーバー192.168.0.160の予約により192.168.0.172を取得する |
| R07 | 確定 | 電子ペーパーはWaveshare 2.13インチV4。Pico W／Pico 2 Wの機種差を切り分ける |
| R08 | 確定 | LAN内pull方式。端末の常時HTTP待受とWAN公開を設けない |
| R09 | 設計 | 画像が同一なら描画せず休眠。新規題材がなければ母艦はSKIPPEDとする |
| R10 | 設計 | 公開を原子的に切替え、端末は最新の完成画像だけを取得する |
| R11 | 設計 | 取得・検証失敗でClearやSPI更新をしない。描画開始後の障害は表示不確定とする |
| R12 | 確定 | PNG、生成時刻、出典URL、生成指示等の画像履歴を自動削除しない |
| R13 | 確定 | 開発は~/dev/システム概念/プロジェクト名、本番は~/tools/システム名の方針に従う |
| R14 | 確定 | プロジェクト開始時のgitlinkはユーザーが実行する。本書作成では実行しない |

### 対象外と未測定事項

本書は設計のみで、実装、インストール、設定変更、配備、筐体再設計、外部向けニュース配信は対象外とする。有料APIの自動採用や別モデルへの無断変更は行わない。電源はモバイルバッテリー、低負荷停止時はPC USBを候補とし、電池容量と許容平均電力が未確定のため、電池寿命は予測値を提示しない。

192.168.0.160はDHCPサーバーであり、母艦IPと混同しない。母艦は.120、端末は.172とする。サブネット、PicoのMAC、Ubuntu版、MicroPython版は導入時に記録する。

## 2 システム構造と責務

Pi 5はニュースと生成物の正本、永久保存する画像履歴、最新公開manifestを持つ。Picoは毎時だけネットワークへ接続してpullする。画像生成の所要時間をPicoの起床時間へ持ち込まない。

```text
Pi 5  user timer → ニュース選定 → Codex画像生成 → PNG正本
                                ↓
       RAW変換・検証 → 不変の履歴保存 → latest原子的切替
                                                ↓
       user HTTP service 192.168.0.120:8001 ← 毎時GET
                                                ↑
Pico  休眠 → wake → Wi-Fi接続 → latest／RAW取得・完全検証
       ↑                         ↓
       Wi-Fi停止・パネルsleep ← 必要時のみfull refresh
```

| 構成要素 | 責務 | 制約 |
| --- | --- | --- |
| timer／generator | 毎時、有限な生成処理を一件起動 | slot一意制約とlockで多重生成を防ぐ |
| news／editor | 一次資料の根拠整理、題材と風刺構図の選定 | 外部文書を命令として実行しない |
| image adapter | Codexの検証済み画像生成能力を使用 | Luna自体の描画能力を仮定しない |
| normalizer／archive | PNG、RAW、出典、指示、hashを保存 | PNG・生成履歴の自動削除なし |
| publisher | 完成manifestをlatestへ原子的に公開 | 未完成画像をlatestにしない |
| HTTP service | 不変フレームとlatestをread-only配信 | GET時に生成しない。debug／reloader無効 |
| Pico wake client | 期限内に取得・検証・同一判定 | 常時待受なし。無期限retryなし |
| panel／power adapter | V4通常表示、BUSY、sleep、Wi-Fi停止、MCU休眠 | 機種とfirmware固有。実測で成立確認 |

Dockerは必須としない。母艦はPythonとCodex CLI、端末は既存MicroPythonを機能試験の基礎にできる。ただし厳しい低電力目標に達しない場合は、検証済みnative SDK方式または外部電源タイマーを比較し、実測に基づき最小変更で成立する方式を選ぶ。

## 3 ニュース選定と風刺の仕様

### 取得と鮮度

各時間スロットでHN候補とWeb上の一次資料を照合する。HNの投稿日時は参照先記事の公開日時とは別に保存する。投稿の新しさや得票数だけを「最新」の根拠とせず、出来事が発生した日時、原著の公開／更新日時、実際に確認できた事実を優先する。[S3]

| 項目 | 設計値または判断規則 |
| --- | --- |
| 候補範囲 | 原則24時間以内。適格候補がなければ48時間まで広げ、その扱いを記録する |
| 日付不明 | 公開日・出来事の日付を確認できなければ時事の新着候補から除外する。推測で補わない |
| 重要性 | AIの技術・製品・利用・制度への具体的な変化を優先。話題性だけでは選ばない |
| 新規性 | 正規化URL、記事ID、同じ出来事のevent_keyで既出判定。続報は新しい事実を明示する |
| 偏りの制御 | 同じ企業・同じ比喩の連続採用を抑制。選定理由と棄却理由を残す |
| 不成立 | 裏付け不足、既出のみ、取得不能を区別してSKIPPEDまたはFAILEDにする |

### 編集上の制約

- 事実台帳は「何が確認できたか」と「誰が主張しているか」を分ける。HNコメントは関心や論点の補助資料であり、事実の確定根拠にはしない

- 風刺は過剰な期待、現実とのずれ、トレードオフ等を一つの視覚的比喩にする。架空の会話や構図を実際の発言・写真と誤認させない

- 無根拠な不正の断定、個人情報、実在人物の虚偽発言、原画像・既存漫画の複製を生成指示に含めない

- 「新しいニュースがない」を正常な結果として認める。毎時描画のために新事実を創作しない

### 生成指示の構造

入力はfacts、source_refs、satirical_metaphor、visual_composition、forbidden_claimsの独立フィールドで構成する。画面は原則として一場面、一つの焦点、太い輪郭、広い白地とする。細かい模様や長い文章に説明を依存させない。必要な短文と表示日付は母艦の既知フォントで合成し、文字品質をモデル任せにしない。

### モデルと生成手段の分離

GPT-6-Lunaは要求された調査・構成用モデルの指定として扱う。画像バイトを出す機能はCodexの利用可能ツール／バックエンドとして別途成立確認する。指定モデル名、CLI引数、画像出力、認証、無人実行、利用上限のいずれかが成立しなければG1不合格とし、代替手段と費用は改めて決定する。

## 4 画像と電子ペーパーの仕様

| 属性 | 正本PNGの契約 |
| --- | --- |
| 画素 | 幅250、高さ122。横長をアプリケーションの正規座標とする |
| 形式 | PNG、bit depth 1、grayscale color type 0、非interlace。白と黒のみ |
| 極性 | PNG sample 0＝黒、1＝白。転送／ドライバー極性との対応をアダプターで定義する |
| 不可条件 | パレット、アルファ、アニメーション、寸法違い、複数フレーム、壊れたチャンクは不可 |
| 整合性 | 標準PNGデコーダーで再読込し、モード・寸法・CRC・画素数を確認する |
| 不変性 | 表示対象確定後のPNGと転送バイト列はハッシュで固定し、再送時に再生成しない |

PNGの規格要件と装置の受入プロファイルは分けて管理する。[S4] 画像生成バックエンドが任意サイズやRGBを返すことは許容するが、正本化を通過するまでは表示対象にしない。

### 母艦の正本化パイプライン

```text
生成元画像 → 内容／構図確認 → 余白確保とcrop／resize
           → 明度・コントラスト調整 → 二値化 → 文字合成
           → 250 × 122 PNG保存 → 再decode検証 → SHA-256確定
```

二値化は固定閾値を初期値とし、面積表現が必要な場合だけ弱いordered ditherを選ぶ。変換アルゴリズム、閾値、フォント、余白、ソフトウェア版を保存する。全白／全黒、極端な黒占有率、主要線の潰れは検知し、原寸の視認試験で基準を調整する。自動検査だけで風刺の意味や可読性の完全保証とはしない。

### V4パネルの駆動

V4の物理解像度は122(H) × 250(V)であり、横長PNGとは座標系が異なる。[S1 p.5] パネルアダプターが回転、ビット順、行境界、未使用ビットを一元管理する。角の識別子と縦横パターンで対応を固定し、見た目の手修正を通信層へ混入させない。

毎時の更新は全画面更新を標準とする。BUSYがHIGHの間はコマンドを発行せず、解除を待ってからsleepする。[S1 pp.8–9,29] partial／fast更新は初期版で使用しない。partial更新回数カウンターは不要とし、24時間の保守full更新は別途管理する。

### 電源断と画面の意味

電子ペーパーに残った表示と、RAM／ドライバー状態が保持されることは別である。再起動時の表示状態はUNKNOWNとする。母艦から正本に対応するRAWを再取得して全更新を完了するまで、表示確認済みと扱わない。画面上の取得日付は古い画像が残り続ける場合の目印になる。

## 5 参照実装からの継承と修正

提供されたserver側2本、Pico側main／ドライバー、画像2点、フォント2点を静的に確認した。既存構成はPicoが母艦の/render_textへHTTP GETするpull方式であり、省電力pullの基礎として使える。ニュース取得やCodexによる風刺画生成は既存機能とは別に構築する。[L1–L5]

| 観点 | 静的確認結果 | v0.1の扱い |
| --- | --- | --- |
| 表示ドライバー | Landscape通常init／display／sleepは公式V4と正常系が一致 | この経路に限定して流用。V3記載のヘッダーだけで判定しない |
| 高速経路 | SetWindow／send_data2等の未定義参照が残る | fast／partialは流用対象外 |
| フレーム長 | 4096 byteを確保し実処理は4000 byte。末尾96 byteが余分 | wireと受信長を4000 byteに固定する |
| 極性と余白 | app.pyとzukyun.pyで白黒極性が異なる。未使用bitも未統一 | 白1黒0、余白白を統一してgolden vector化する |
| 画像寸法 | 参照画像のresize後は250 × 125。表示範囲から3行が欠落 | 正本化時に250 × 122へ確定し切落しを禁止 |
| 取得と更新 | 取得前にClear、取得後に表示。表示確認／ID／digestなし | 全検証後のみ表示。ID、hash、更新済み画像判定を追加 |
| 待機と切断 | Wi-Fi／BUSYに無期限待ち。毎回Wi-Fi停止とreset | 上限付きwake周期へ修正。resetを低電力休眠と同一視しない |
| 母艦配信 | Flask debugモード、要求時生成、可変debug画像 | 生成と不変画像の配信を分離。debugとreloaderを無効化 |

### GPIOと機種の適合

参照コードではRST=12、DC=8、CS=9、BUSY=13、SPI(1)、4 MHzを使用している。SCK／MOSIは明示されずファームウェア既定に依存するため、新実装では基板と導通を確認して明示する。[L3] Pico W／Pico 2 Wの実機型番とMicroPython版は、ソース名から断定しない。

### 流用の条件

ソース全体を検証済みとは扱わない。通常表示経路のタイムアウト化、finallyでの資源解放、例外処理、メモリ試験、非対称マーカー試験を流用条件とする。既存Wi-Fi認証情報の値を設計書・ログ・生成指示へ転載しない。添付Meiryoフォントの再配布・Ubuntu配置の権利は別途確認し、許諾を確認した日本語フォントを採用する。

## 6 RAW転送フレームの厳密な契約

母艦に250 × 122のPNGを必ず保存し、LAN転送には固定長RAWを使用する。端末にPNGやDEFLATEの展開器を追加せず、既存の画素アダプターとV4通常表示経路を活かす。PNGの保存要件とwire形式は別の契約である。

| 属性 | epd122x250-msb-white1-v1 |
| --- | --- |
| 座標 | 物理向きの幅122、高さ250。左上原点、右向きu、下向きv |
| 格納 | v=0から249へ行優先、1行16 byte。各byteは左の画素からbit7→bit0 |
| 極性 | 白=1、黒=0。各行のu=122..127の6bitは必ず白 |
| 長さ | 16 × 250 = 4000 byte。前後ヘッダー、base64、行区切りなし |
| ハッシュ | wire_sha256は4000 byte全体。png_sha256は保存したPNGファイル全体 |
| 転送形式 | Content-Type: application/octet-stream。Content-Length: 4000。chunked不可 |

### PNG座標との対応

```text
P(x, y): PNGの画素値 0=黒 1=白
R(u, v) = P(v, 121-u)       0≤u<122, 0≤v<250
R(u, v) = 1                 122≤u<128
W[16*v + floor(u/8)] のbit (7-u%8) = R(u,v)
```

上記はPNGを時計回り90度回転させた候補対応であり、既存zukyun.pyの意図に合わせた設計である。筐体の向きを含む実機試験で固定する。回転の修正が必要ならformat_idまたはadapter_versionを更新し、黙って意味を変えない。

### 既存Landscape配列への変換

```text
i=0..249, j=0..15について
B[i + (15-j)*250] = W[i*16 + j]
V4 Landscapeドライバーに4000 byteのBを渡す
```

既存displayはjを15から0へ、iを0から249へ進めて配列を読む。[L3 579–584行] この配列順をwireへ露出させず、Pico側panel adapterが変換する。PNGの250画素行を32 byteで表す3904 byte、PNGフィルター等を含む圧縮データ、ドライバーの4000 byteは相互に同一ではない。

### 受入用golden vector

全白、全黒、1画素線、四隅に異なる印、左右非対称矢印、250 × 122の外周を使用する。PNG→RAW→論理画素の逆変換が一致し、最終byte下位6bitが常に1であることを単体試験する。さらに実機で反転、回転、6画素ずれ、切落しがないことを確認する。

## 7 母艦の生成と公開の状態遷移

```text
DUE → COLLECTING → SELECTED → GENERATING → VALIDATED
VALIDATED → ARCHIVED → PUBLISHED
候補なし → SKIPPED    各段階の失敗 → FAILED
SKIPPED／FAILEDではlatestは前回完成画像を維持
```

| 境界 | 完了条件 |
| --- | --- |
| DUE | UTC epoch hourのslot_keyを一意登録。再起動・時計補正で二重生成しない |
| SELECTED | 出典、事実、選定理由、dedup_keyを確定 |
| GENERATING | 子プロセス開始を記録。1 slotで新規生成は最大一回 |
| VALIDATED | PNG再decode、RAW逆変換、4000 byte、padding、hashを検証済み |
| ARCHIVED | PNG、manifest、生成指示を履歴へ永続保存。RAWも不変IDで配信可能 |
| PUBLISHED | 完成したlatest manifestの参照を原子的に切替えた |
| SKIPPED／FAILED | 理由と段階を記録。前回latestと画像履歴を変更しない |

### 生成時刻と起床時刻

初期案はPi 5が毎時00分に生成開始、15分以内の公開を目標とし、Picoは毎時20分に取得する。生成処理は20分を上限とする。遅延・生成失敗時はPicoが最新の完成画像を取得して直ちに休眠し、その場で新画像の完成を待たない。時刻は設定可能にし、両装置の位相を独立に管理する。

Pi 5の停止後は過去全slotを再生成せず、最新slot一件に集約する。Picoは初回起動で一度取得し、応答の署名済みserver_timeから次回の20分を求める。継続sleepの計測はmonotonic deadlineを使い、早期wake時は残り時間のsleepへ戻る。時計情報が使えないときは直前wakeから3600秒を暫定周期とする。

### 公開の一貫性

frame_idはPNG hashと変換版に結びつく不変IDとし、同じIDの内容を変更しない。新しいRAWとmanifestの保存を完了させてから、latestだけを原子的に差替える。Picoがmanifest取得後にlatestが進んでも、記載された不変frameをそのまま取得できる。PNGと履歴は維持するため、この競合で旧参照が消えない。

母艦のPUBLISHEDは表示成功ではない。Picoの取得／表示結果は次回wakeの短いtelemetryに載せて送れるが、端末への表示到達をリアルタイムに保証しない。最新の完成画像の時刻と、端末から最後に報告された表示時刻を別項目で管理する。

## 8 HTTP pullインターフェース

母艦endpointはhttp://192.168.0.120:8001。Picoはclientであり、192.168.0.172で待受しない。HTTPはConnection: closeを基本とし、redirectを追跡しない。GETを契機に画像を生成しない。

| メソッドとpath | 契約 |
| --- | --- |
| GET /v1/latest | 200 JSON。schema_version、publish_seq、frame_id、created_at、published_at、server_time、png_sha256、wire_sha256、format_id、length=4000、raw_pathを返す |
| GET /v1/frames/{frame_id}.raw | 200 application/octet-stream、Content-Length:4000。不変のRAW本体を返す |
| GET /v1/frames/{frame_id}.png | 履歴の正本PNG。通常のPico取得には使わない |
| GET /v1/status | 母艦の生成・公開状態。健康確認用で秘密情報を含めない |
| POST /v1/telemetry | 任意の短い前回表示結果。端末起床中だけ送信し、失敗しても取得を妨げない |

### manifestと取得の整合性

Picoはschema、format、length、frame_id、hashを検証する。raw_pathは固定prefixと厳格なID形式から組み立て、任意のURLや他hostへ接続しない。最新manifestが同じwire_sha256ならRAW取得・パネル初期化・描画を省略する。保守更新期限を迎えた場合だけ同一画像のfull更新を許す。

RAWは4000 byteを完全受信してからhashと応答MACを検証する。Content-Lengthと実長の不一致、余分なbody、chunked、圧縮HTTP、format不一致、padding不正は拒否する。最大ヘッダー4 KiB、manifestとtelemetryは各2 KiBとする。

### 応答とclient動作

| 応答 | 処置 |
| --- | --- |
| 200 | 型・サイズ・MAC・hashを全検証して採用 |
| 404 | 初回画像未公開または不明ID。最新を一度だけ再照会し、なお不可なら休眠 |
| 401／403 | 認証失敗を記録し、そのwake中の反復を止める |
| 429／503 | Retry-Afterがwake予算内なら一回だけ待つ。それ以外は次回wakeへ |
| timeout／5xx | 同じ不変URLを一回だけ再取得できる。期限で打切り、旧表示を維持 |

telemetryはdevice_id、boot_id、frame_id、result、displayed_atまたはuptime、error_codeを含む。母艦は報告時刻と装置側時刻を分ける。表示成功を報告するためだけにWi-Fi接続を延長・再開せず、原則として次回wakeに送る。

## 9 LAN認証と信頼境界

HTTP bodyの機密性は保護しないが、HMAC-SHA256で要求と応答を認証する。共有鍵はdeviceごとに分ける。常時待受するPi 5ではLAN interfaceへのbind、送信元範囲制限、rate limitを併用する。WAN公開は行わない。鍵の新規生成・配置は導入時に別途承認を得る。

### 最小往復の認証契約

Picoは各要求に、再利用しないrequest_id（安全な乱数16 byteの小文字hex）、X-Device-Id、X-Key-Id、X-Request-Id、X-Request-Macを付ける。challengeの追加往復は設けない。device_idとkey_idは事前登録済みASCII識別子とする。

```text
要求MACフィールド順（各項目をLFで連結し末尾もLF）
request-v1, key_id, device_id, request_id, METHOD, path,
content_length, content_type, sha256(body)
```

METHODは大文字、pathはqueryなしの固定path、GETはbody空、content_length=0、content_typeは空文字。MACとhashは小文字hex64文字。必須ヘッダーの重複を拒否する。母艦はtelemetryのrequest_idを一時間保持し、同一要求の重複登録を防ぐ。GET再生自体は読取りであるが、rate limitの対象とする。

```text
応答MACフィールド順（各項目をLFで連結し末尾もLF）
response-v1, request_mac, HTTP status十進表記,
content_length, content_type, sha256(実際の応答body)
```

X-Response-Macを付けて返す。端末は自分のrequest_macに束縛された応答だけを受け入れる。過去のlatest応答を録音して差替えても、現在のrequest_idには一致しない。MAC検証前にserver_timeやraw_pathを採用しない。

### 安全な実装の成立条件

Pico側で安全な乱数、HMAC、定数時間比較が使えることをG3で確認する。再起動でrequest_id列が再現する実装を認めない。利用できない場合は認証を外さず、実装方式またはTLS等を改めて選定する。Wi-Fi情報、共有鍵、MAC、Codex認証情報を画像、生成指示、ログ、Gitへ含めない。

### 外部記事とLANの分離

記事・HN本文・モデル出力は不信データとする。固定プログラムがschemaと長さを検証し、任意shell、実行path、設定変更、install指示を採用しない。記事fetchはHTTP(S)の公開アドレスに限り、localhost、private、link-local、metadata宛とそのredirectを拒否する。LAN配信と記事fetchの経路を分離する。

残留リスク：HTTP盗聴、LAN内DoS、端末への物理アクセスによる鍵窃取。これらまで保護する必要があれば、TLS・network分離・鍵保護を追加設計する。

## 10 Picoの休眠と状態遷移

```text
WAIT → WAKE → WIFI_CONNECTING → FETCH_MANIFEST
新画像／保守期限 → FETCH_RAW → VERIFY → WIFI_OFF → DISPLAY
DISPLAY → BUSY解除 → PANEL_SLEEP → MCU_SLEEP → WAIT
同一画像 → SKIP → WIFI_OFF → MCU_SLEEP → WAIT
取得／検証失敗 → CLEANUP → WIFI_OFF → MCU_SLEEP → WAIT
```

先に新フレームをRAMへ完全取得し、検証後にWi-Fi接続を閉じてからパネルを初期化する。参照ドライバーはconstructorがinitを行うため、オブジェクト生成も採用画像が確定するまで遅らせる。失敗時はClearしない。更新済みhashと前回結果を保持し、同じ画像の描画を省く。

### 起床予算

Wi-Fi接続20秒、HTTP connect 3秒／read 10秒、通信retry一回、BUSY 60秒、wake全体120秒を初期上限とする。全てmonotonic deadlineで制御し、finallyでHTTPを閉じ、Wi-Fiを停止する。新画像がまだなければ待ち続けない。早期wakeやUSB割込みで再取得を繰り返さない。

### 省電力方式のゲート

stock MicroPython rp2のdeepsleepはlightsleep後にresetする実装であり、名称だけでより深い省電力状態とは扱えない。時限lightsleepは一時間を表現できるが、割込みやCYW43処理で早期復帰し得る。[S11] WLAN.active(False)だけを無線電源の完全遮断と同一視せず、採用firmwareのdeinit挙動と電流を確認する。[S12–S13]

初期の機能確認は無線停止＋時限休眠で行う。十分な低電力にならない場合、選定SDKのnative low-power機能、Pico 2 W固有の低電力状態、外部RTC／電源タイマーを比較する。RP2040でも使用するclock sourceとSDKで時限wake条件が変わるため、「常に外部RTCが必要」とは決めつけない。[S14]

### 保持状態と再起動

RAM保持sleepではlast hash／last-good RAW／次回deadlineを保持する。resetを伴う方式ではRAM保持を仮定しない。最終表示hash・表示時刻の小さなjournalを、実際に画像を変更したときだけ永続化する方式を候補とし、flash寿命と原子性をG2で確認する。journal不明なら最新を再取得・全更新する。

物理更新は180秒以上あけ、起動直後はWi-Fi停止のまま180秒のguard休眠を経て通常wakeへ入る。この休眠は120秒の起床予算に含めない。24時間の保守full更新はwake時に判断し、RAMに画像がなければ再取得する。[S2] 母艦・LAN不通かつRAMにframeがなければ保守更新は保証できない。BUSY timeout後は画面不確定とし、連続reset・再駆動しない。

## 11 画像履歴と永続データ

最終250 × 122 PNGと、その生成日時・ニュース出典・生成指示・変換条件を画像作品の履歴として保存する。自動削除を行わない。最新表示用のlatest参照は履歴から独立させ、将来、選んだ過去画像を再表示できるデータ構造とする。

| データ | 保存方針と主要項目 |
| --- | --- |
| 正本PNG | 永久保持。image_id、created_at、png_sha256、不変path |
| 画像manifest | 永久保持。source URLs、公開／取得日時、fact_summary、satire_concept、生成指示、model／backend版、変換設定 |
| RAWフレーム | format_id、adapter_version、wire_sha256と対応。小容量のため初期版はPNGと併せて保持 |
| latest | publish_seq、frame_id、created_at、published_at。不変artifactへの原子的な参照 |
| 実行履歴 | SQLiteでslot、state、attempt、error、各時刻を記録。作品に紐づく結果は画像履歴と共に保持 |
| 端末報告 | 最後の取得／表示報告、boot_id、error。母艦公開状態とは別管理 |
| 高解像度原画像 | 最終PNGと別扱い。保存期間と容量上限は未確定。無断の自動削除・永久保持を仮定しない |

### 容量計画

例として最終PNGが平均5 KB、毎時24枚を365日保存すると約44 MB／年となる。実サイズ・skip頻度に依存する試算である。RAW 4000 byteも毎時保存すれば約35 MB／年。JSON、DB、ログ、高解像度原画像は別途加算する。原画像の実サイズを測り、容量警告閾値に達したら生成を止める設計とする。

### 配置案

```text
開発  ~/dev/電子ペーパー/ai-news-satire-display/
本番  ~/tools/ai-news-satire-display/
      releases/<version>/       実行物とschema
      current -> releases/...   採用版
      config/                   非秘密設定
      state/jobs.sqlite3        生成と公開状態
      archive/<image_id>/       PNG RAW manifest
      published/latest.json     完成画像への参照
      tmp/                      未コミット出力
      backups/                  整合バックアップ
```

tmpへ書込み、検証、flush／fsync、同一filesystem内renameとdirectory同期を行ってからDBを確定する。公開は最後にlatestを切替える。起動時に孤児ファイルと未完了recordを照合する。容量不足では新規生成を止め、過去作品とlatestを保全する。履歴の削除はユーザーの明示的操作のみとする。

## 12 systemdと実行設定

HTTPの常駐と生成の周期処理を別unitにする。配信は画像生成の遅延やCLI失敗に影響されず、前回完成画像を返し続ける。全て利用者のユーザー権限で実行する。[S5 S7 S8 S15]

| unit | 設計 |
| --- | --- |
| ai-news-http.service | Type=simple。192.168.0.120:8001にbind。read-only artifact配信と短いtelemetry受付。Restart=on-failure、RestartSec=30s |
| ai-news-generate.service | Type=oneshot。固定Python entrypointからCodex execを起動。TimeoutStartSec=20min。自動Restartなし、排他lockあり |
| ai-news-generate.timer | 毎時00分、Persistent=true。停止中の全slotを埋め戻さず最新一件に集約 |
| 共通 | ExecStart／WorkingDirectory／Python／Codexは絶対path。PATH等は明示し.bashrcに依存しない |
| 権限と起動 | UMask=0077。logout／boot後のuser managerにはlingerを検討し、設定変更は承認後に実施 |

### 主要設定の初期案

```text
host_url = http://192.168.0.120:8001
model = GPT-6-Luna              # 正確なCLI識別子はG1で確定
generate_minute = 0             pico_fetch_minute = 20
slot_budget_seconds = 1200      generation_attempts_per_slot = 1
news_lookback_hours = 24         fallback_lookback_hours = 48
wifi_connect_timeout = 20       wake_budget_seconds = 120
http_connect_timeout = 3        http_read_timeout = 10
busy_timeout = 60               min_refresh_interval = 180
maintenance_interval = 86400    archive_auto_delete = false
```

これは設計例であり、投入用のunitや実装設定ではない。時刻表示はAsia/Tokyo、保存はUTC。設定schemaで単位と範囲を検証する。起床時刻の補正を行う場合も、早期wakeを新周期と誤認しない。

### Codexの成立条件

非対話モードのexec、schema出力、保存済みCLI認証は公式に説明されている。[S5] 画像生成ページは、headless execでの利用条件を確約するものとして扱わない。[S6] 実機でCLI版、model ID、画像tool、artifact保存、無人権限、利用上限を確認する。認証切れや承認待ちはtimeoutで停止し、有料APIへ自動切替しない。

### 母艦の安全な配信

Flaskを使う場合もdebugとreloaderを無効にし、常駐用途のserver構成を用いる。GETは固定のartifactとstatusのみを扱い、任意ファイルpath、生成起動、shell実行、設定書換えの機能を公開しない。公開schemaとadapter版は互換性管理する。

## 13 障害処理と省電力運用

| 障害 | 処置 | 状態 |
| --- | --- | --- |
| ニュース取得一時失敗 | slot予算内で最大3回。資料不足なら生成しない | FAILED。latest維持 |
| 候補なし／既出のみ | 生成なし。最新完成画像は配信可能 | SKIPPED |
| Codex認証／tool不可 | 同slotの自動生成retryなし | 要対応。代替APIへ切替しない |
| Pi 5不通／Wi-Fi失敗 | 通信retry一回、wake期限で打切り | 旧画面維持、次回wake |
| 不正manifest／RAW | 完全棄却、Clear・SPI操作なし | 検証失敗を保持 |
| latestが生成中に更新 | manifestに示された不変frameを取得 | 次回にさらに新しい版を取得 |
| Pico reset | journalを検証。状態不明なら再取得 | 回復表示は一度、guard遵守 |
| BUSY固定／SPI例外 | timeoutで打切り、描画連打を止める | PANEL_ERROR、画面不確定 |
| 容量不足 | 生成停止。画像履歴とlatestを削除しない | STORAGE_FULL |
| MAC不正 | 処理打切り。無認証へ降格しない | SECURITY_ERROR |

### 状態の観測

母艦はjournaldへslot_key、job_id、stage、duration、errorを記録する。最終生成成功、最終公開、端末の最終取得報告、端末の最終表示報告を別指標とする。Picoは省電力優先のため常時ping応答を期待しない。2周期以上報告がない場合に端末未確認とするが、画像が消えたとは断定しない。外部通知先は別途決定する。

### 電源と電力の受入指標

モバイルバッテリーを候補とし、低負荷の自動停止で使えない場合はPCのUSB給電へ切り替える。自動停止回避のダミー負荷は初期案に含めない。電源断後はbootで最新画像を取得し、完全検証前にClearしない。PCのsleep／shutdownでUSB給電が止まるかも実機確認する。

電源入力で、休眠電流、Wi-Fi接続時間と電力、受信、full refresh、sleep移行を測定する。一周期の積算エネルギーを求め、平均電力は一周期エネルギー÷3600秒として比較する。USB接続やdebug、LED、電源回路、キャリア基板の消費を含める。電池容量と変換効率が決まるまでは稼働日数を断定しない。

正常で同一画像の場合はmanifest取得だけで終了し、RAW転送とパネルinitを省く。新画像時もWi-Fiを描画待ち中に保持しない。120秒の上限は通常目標ではなく故障時の上限であり、正常起床時間を測って短縮する。

## 14 機能と省電力の受入試験

以下は未実施の受入条件であり、静的ソース確認から実機動作済みとは扱わない。

| ID | 対象 | 合格条件 |
| --- | --- | --- |
| T01 | G1／R04 | aarch64・非対話環境で指定モデル、調査、schema出力、画像保存が承認待ちなく完了 |
| T02 | R01／R10 | 毎時生成が重複せず、未完成画像がlatestから取得されない。GETは生成を起動しない |
| T03 | R03／R12 | 事実・出典・公開／取得／生成時刻・生成指示がPNG履歴と対応し、履歴が自動削除されない |
| T04 | R05 | PNG250 ×122・1bit・color type0。RAW4000 byte、逆変換一致、padding白 |
| T05 | G2／R07 | 実機で四隅・外周・非対称矢印が正位置。反転、6pxずれ、3px切落しがない |
| T06 | R02／R09 | 1時間ごとにwake。同一hashではRAW取得・panel init・更新が省かれる |
| T07 | R06／R08 | .172の端末が.120:8001から取得。.160を配信先と誤用しない。WAN公開なし |
| T08 | 休眠 | 早期wake・USB割込み・無線処理後も期限を再評価し、過剰な取得をしない |
| T09 | 保守更新 | 正常運用で24時間以内のfull保守更新と180秒最小間隔を両立 |
| T10 | 電力 | 休眠電流・周期エネルギーを測定。電源の低負荷停止とPC sleep時USB給電を確認し、推奨方式を確定 |
| T11 | 継続性 | 72時間で重複生成、無期限wake、RAM増大、履歴欠損、frame破損がない |

### 比較する実行方式

実機がPico WかPico 2 Wかを確定し、stock MicroPython方式の機能・電力を測る。必要ならnative低電力方式や外部power gateを同じ測定条件で比較し、実装側で推奨方式をまとめる。機種変更を単なる高速化として扱わず、wake、clock、Wi-Fi復帰、GPIOと状態保持を再試験する。

### 画質

原寸と実機の双方で主題、輪郭、余白、文字と日付を確認する。初期の基準画像をPNG履歴に残し、変換設定やdriver変更時の回帰試験に使う。

## 15 障害と整合性の受入試験

| ID | 故障注入または境界 | 合格条件 |
| --- | --- | --- |
| F01 | 途中切断／長さ違い／bit改ざん | 棄却してClear・SPI更新なし。期限内に休眠 |
| F02 | manifest取得後にlatest更新 | 不変URLから旧manifestと整合するframeを取得 |
| F03 | 起床中・描画中のPico電源断 | RAM保持を仮定せず回復。画面不確定を正常表示扱いしない |
| F04 | 保存・DB・latest切替境界で母艦停止 | 旧latestまたは新latestの完成版のみ返し、未完了artifactを配信しない |
| F05 | BUSY HIGH固定 | 上限でPANEL_ERROR。無限待ち・連続reset・描画連打なし |
| F06 | 偽MAC／古い応答の差替え | 現在のrequest_idに対する応答のみ受入れ、認証を降格しない |
| F07 | Codex認証／モデル／画像tool不可 | 原因記録。有料API・別モデルへの無断切替なし |
| F08 | 容量・権限不足／DB破損 | 新規生成停止。作品履歴とlatestを保全 |
| F09 | 時計補正／早期wake／二重generator | slot一意制約とdeadlineで重複・過剰wakeを防ぐ |
| F10 | 悪意ある記事／URL／巨大本文 | shellや設定変更を誘発せず、内部アドレスへfetchしない |
| F11 | 母艦停止かつ端末reset | 取得失敗後に休眠。frame不在時の保守更新不能を正しく記録 |

### 証跡

試験ごとにfirmware／SDK／CLI版、設定、対象PNG hash、RAW hash、ログ、実測時間・電力、実機表示写真を保存する。成立していない項目を既存コードの名称や公式サンプルとの一致だけで合格にしない。

### 秘密と操作の扱い

認証ファイル、Wi-Fi設定、共有鍵は本文・ログ・生成プロンプトへ入れない。キー生成／配置、linger、firewall等の変更は導入段階で承認範囲を確認する。SSHは接続確認・読み取り・別タスク経由も含め、毎回、接続先・目的・操作・変更範囲について事前承認を得る。本書作成ではSSHを使用しない。

## 16 導入とロールバック

| 段階 | 成果と進行条件 |
| --- | --- |
| 0 設計確定 | 毎時wake・最新取得・画像履歴を前提に実装範囲を確定。電力方式は実装側で調査・検証する |
| 1 能力確認 | G1でCodex画像生成を、G2でwake・Wi-Fi停止・電力を確認。不成立のまま本実装へ進まない |
| 2 固定画像試験 | PNG→RAW→実機。四隅・極性・padding・timeoutを合格させる |
| 3 pull統合 | 原子的latest、不変配信、応答認証、同一skip、通信故障を試験 |
| 4 母艦統合 | ニュースから画像履歴までdry run。事実、風刺、画質、保存性を確認 |
| 5 限定稼働 | 手動生成からuser timer／serviceへ移行。72時間と電力測定を実施 |
| 6 本番採用 | 正常版、設定、golden vector、状態バックアップ、復旧手順を固定 |

### 戻し方

generator timerを止めて新しい公開を停止し、HTTPは前回完成画像を配信する。実行物とDBを整合バックアップし、互換性確認後にcurrentを正常releaseへ戻す。DB schemaが非互換なら対のバックアップを復元するが、作品archiveは上書き・削除しない。

Pico firmwareはUSB等の承認された手順で戻す。旧/render_text形式へ戻す場合は母艦APIも対で戻す。過去作品の再表示は、元ファイルを変更せずlatest参照を選び直しpublish_seqを進める運用として設計可能である。初期版の自動操作には含めず、ユーザーが選ぶ手段を別途実装する。

### 未確定事項

| 項目 | 解消条件 |
| --- | --- |
| Codexと画像出力 | CLI版、正確なmodel ID、headless画像tool、利用条件と枠をG1で確認 |
| 端末と低電力 | Pico W／Pico 2 Wの実機、firmware版、許容平均電力、電源条件、sleep方式を測定で確定 |
| GPIOと保持 | SCK／MOSI、表示向き、reset時journal、flash寿命・原子性を確認 |
| LANと認証 | subnet、DHCP予約MAC、key provision／更新方法、nonce乱数品質を確認 |
| 画像履歴 | 最終PNG・生成履歴は自動削除なし。高解像度原画像の保持方針と容量警告閾値を決定 |
| 運用名とフォント | 開発分類、システム名、許諾確認済み日本語フォントを確定 |

## 17 根拠資料と参照箇所

外部仕様は2026年10月3日確認。仕様本文と実機の結果が異なる場合は、使用版・差分・採否を記録する。以下のリンクは仕様根拠であり、実装や配備が済んだことを示すものではない。

[S1  Waveshare 2.13inch e-Paper V4仕様書  pp.5 8 9 29](https://files.waveshare.com/upload/4/4e/2.13inch_e-Paper_V4_Specification.pdf)

[S2  Waveshare Pico-ePaper-2.13  更新間隔とsleepの注意](https://www.waveshare.com/wiki/Pico-ePaper-2.13)

[S3  Hacker News公式API  itemと日時の定義](https://github.com/HackerNews/API)

[S4  W3C PNG仕様 Third Edition](https://www.w3.org/TR/png-3/)

[S5  Codex Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

[S6  Codex Image generation](https://learn.chatgpt.com/docs/image-generation)

[S7  systemd.service  プロセス管理](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml)

[S8  loginctl  user managerとlinger](https://github.com/systemd/systemd/blob/main/man/loginctl.xml)

[S9  MicroPython framebuf  フォーマット定義](https://docs.micropython.org/en/latest/library/framebuf.html)

[S10  Waveshare公式 V4ドライバー](https://raw.githubusercontent.com/waveshareteam/Pico_ePaper_Code/main/python/Pico_ePaper-2.13_V4.py)

[S11  MicroPython v1.27.0 rp2 machine  lightsleep／deepsleep](https://raw.githubusercontent.com/micropython/micropython/v1.27.0/ports/rp2/modmachine.c)

[S12  MicroPython v1.27.0 CYW43  Wi-Fi終了処理](https://raw.githubusercontent.com/micropython/micropython/v1.27.0/extmod/network_cyw43.c)

[S13  MicroPython RP2 quick reference](https://docs.micropython.org/en/latest/rp2/quickref.html)

[S14  Raspberry Pi SDK low-power定義  使用版を別途固定](https://raw.githubusercontent.com/raspberrypi/pico-sdk/master/src/rp2_common/pico_low_power/include/pico/low_power.h)

[S15  systemd.timer  定時起動](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)

### ユーザー提供参照ファイル

| ID | ファイルと主な確認箇所 |
| --- | --- |
| L1 | server6側 app.py：32行 4096 byte確保、78行 白黒極性、HTTP配信とdebug設定 |
| L2 | server6側 zukyun.py：44行 配列確保、74–93行 回転とpacking |
| L3 | Pico2側 e_paper.py：421–426行 BUSY待ち、495–525行 init、579–584行 通常display、643–646行 sleep |
| L4 | Pico2側 main(20261003-084351).py：38–50行 HTTP取得・decode・display。認証情報の値は引用しない |
| L5 | zukyun.png／debug_image.bmp：実画像寸法とモード。MEIRYO.TTC／MEIRYOB.TTC：名称と利用許諾確認の対象 |

元の参照指定：J:\tane\Programming\linux\Raspberry_Pi_PICO\picoW\e-paper\e-paper_monitor。ネットワークドライブの状態は本システムの成立条件にせず、提供ファイルをv0.1の静的比較基準とする。原本の追加差分は次版で確認する。
