# Codex呼び出しプロンプトの証拠

母艦のニュース調査と画像生成で、`codex exec`へ渡す初期プロンプト全文を、呼び出し直前に永続保存する。従来の画風指示や生成条件は変更しない。既存の古いジョブには証拠を遡って作らず、この改訂を読み込んだ次の通常ジョブから記録する。

## 保存先と対応付け

`state/prompt_evidence/<job_slot>/<news|image>-<1から始まる試行番号>.json`を使う。毎時slotはUTC時間、手動slotは負数。`jobs.sqlite3`の`jobs.slot`、`stage_attempts(slot,stage,attempts)`、公開フレームの`metadata.job_slot`と照合できる。再試行は別ファイルにする。`prompt_evidence`以下はディレクトリ0700、JSONファイル0600で、Web配信もGit管理もしない。既存の`state/`はGit管理外である。

各記録には、`submitted_prompt`全文、`prompt_sha256`、実コマンドの`argv_without_prompt`、`model`、準備日時、slot・stage・attempt、CLI終了コード、終了日時、結果状態、返された成果物の種類とSHA-256を保存する。認証情報と環境変数は収集しない。記録の`argv_without_prompt`の末尾に`submitted_prompt`を加えた配列が、アプリが`subprocess.run()`へ渡した引数配列である。

`status=prepared`は書込み後に処理が停止したなど、起動・結果が不明な状態であり、Codexの実行成功を意味しない。`artifact_returned`はニュースJSONまたは画像PNGをバックエンドが返した意味で、ニュースの出典検証、1bit正本化、公開、Pico描画の成功とは別である。`failed`、`timeout`、`launch_error`、`interrupted`はそれぞれ該当試行の状態で、CLI終了コードが得られた場合だけ`cli_returncode`へ入る。記録できなければCodex呼び出しを開始しない。

例として、手動ジョブslot `-8`の画像初回の証拠は次で確認できる。

```bash
python3 -m json.tool state/prompt_evidence/-8/image-1.json
```

このJSONには出典と要約が含まれ得るため、内容をWeb、Git、公開ログへ載せない。画像生成の画風確認には`submitted_prompt`と`model`、`status`、`artifact_sha256`を見て、公開作品の`metadata.job_slot`やPNGハッシュと照合する。画像正本化でPNGのバイト列が変わる場合、返却画像のハッシュと公開PNGのハッシュは一致しなくてもよい。

## 証拠の限界

保存するのは**Codex CLIへ渡した初期指示**である。Codexが内部の画像生成ツールへ最終的に渡した別の指示文ではない。現行CLIの`--ephemeral`はセッション履歴を保存しない。[Codex CLI資料](https://learn.chatgpt.com/docs/developer-commands?surface=cli)の`--json`はJSONLイベントを出せるが、画像生成ツールの内部指示文が完全に含まれるとは資料から確認できなかった。今回の実装は`--json`へ切り替えず、未確認の内部指示を実ログとして扱わない。実画像生成やニュース調査を検証のために追加実行していない。

## 確認

模擬Codex呼び出しでニュースと画像の各`submitted_prompt`が実`subprocess.run()`引数の末尾と一致し、slot・stage・試行番号が一致することを確認した。成功、成果物なし、起動エラー、タイムアウト、再試行、突然のプロセス終了、証拠書込み失敗もテストする。後者ではCodexを呼ばない。実運用の最初の証拠は次の通常生成後に確認する。
