# Flashforge Adventurer 4 通信仕様メモ（実機キャプチャベース）

Flashforge Adventurer 4（`Control Success V2.1.` ファームウェア）の TCP ポート `8899` 通信について、FlashPrint とのプロキシキャプチャ（`./tmp/proxy2.log` 相当の実機ログ）および本サーバーの実装で確認できた仕様をまとめます。

---

## 1. 基本プロトコル（TCP 8899）

- **トランスポート**: 生 TCP ソケット（ポート `8899`）
- **コマンド形式**: 先頭に `~`、末尾に `\r\n` を付与した ASCII 文字列
- **応答形式**: 1 行以上のテキストに続き、最終行として `ok\r\n` が返る

---

## 2. セッション管理（単一排他ロック）

| コマンド | 正常応答例 | 備考 |
| :--- | :--- | :--- |
| `~M601 S1\r\n` | `CMD M601 Received.\r\nControl Success V2.1.\r\nok\r\n` | 制御セッション開始 |
| `~M601 S1\r\n`（競合時） | `CMD M601 Received.\r\nControl failed.\r\nok\r\n` | 別クライアントが接続中、または `~M602` なしで切断直後 |
| `~M602\r\n` | `CMD M602 Received.\r\nok\r\n` | 制御セッション終了（切断前に送信が必要） |

> [!NOTE]
> プリンター側は同時に 1 つの制御セッションしか受け付けません。`~M602` を送らずに TCP 切断して即再接続すると `Control failed.` になるため、同一プロセス内ではセッションを再利用する必要があります。

---

## 3. ステータス取得・制御コマンド

| コマンド | 用途 | 応答例 |
| :--- | :--- | :--- |
| `~M115\r\n` | 機種・ファームウェア情報 | `Machine Type: ...` 〜 `ok` |
| `~M105\r\n` | ノズル・ベッド温度 | `CMD M105 Received.\r\nT0:29/0 B:26/0\r\nok` |
| `~M27\r\n` | SD/内部ストレージ造形進捗 | `CMD M27 Received.\r\nSD printing byte 0/100\r\nLayer: 0/0\r\nok` |
| `~M119\r\n` | 機械状態・LED・現在ファイル | `Endstop: ...` / `MachineStatus: READY` / `MoveMode: READY` / `LED: 0` / `CurrentFile: ` 〜 `ok` |
| `~M25\r\n` | 一時停止 | `ok` |
| `~M24\r\n` | 再開 | `ok` |
| `~M26\r\n` | 造形キャンセル | `ok` |
| `~M146 r255 g255 b255 F0\r\n` | 庫内 LED 点灯（消灯は `r0 g0 b0`） | `ok` |

※ FlashPrint の接続時には上記に加えて `~M650\r\n`（`X: 1.0 Y: 0.5`）および `~M114\r\n`（現在座標）も送信されていました。

---

## 4. ファイル転送・造形開始（`~M28` 〜 `~M29` 〜 `~M23`）

FlashPrint（`450,932` バイトの `.gx` ファイル送信時）のキャプチャおよび OrcaSlicer からの実機検証で確認できた実シーケンスは以下の通りです。

1. **転送開始 (`~M28`)**
   - 送信: `~M28 <file_size> 0:/user/<filename>.gx\r\n`
   - 応答: `CMD M28 Received.\r\nWriting to file: /data/<filename>.gx\r\nok\r\n`
2. **ファイル本体の送信（`raw` ストリーム / `.gx` コンテナ）**
   - `<file_size>` バイトの **生ファイルデータそのまま**（`xgcode 1.0\n\x00` ヘッダ 58 バイト + 80×60 24bit BMP `14,454` バイト + G-code 本体の `.gx` バイナリ）を送信する。
   - **パケットヘッダ（`0x5a5aa5a5`）や 4096 バイト単位の `0x00` パディングは付与しない**。
   - **送信中にプリンター側からチャンクごとの `ok` 応答は返らない**。
   - 4096 バイトチャンクあたり数ミリ秒のペーシングを入れ、最終チャンク送信後に `150ms` 程度の待機と `TCP_NODELAY` を併用して `~M29` がデータ末尾と同一 TCP セグメントに結合されないようにする（待機なし一括送信時は `~M29` がタイムアウトする）。
3. **転送完了 (`~M29`)**
   - 送信: `~M29\r\n`
   - 応答: `CMD M29 Received.\r\nDone saving file.\r\nok\r\n`
4. **造形開始 (`~M23`)**
   - 送信: `~M23 0:/user/<filename>.gx\r\n`
   - 応答: `CMD M23 Received.\r\nFile opened: <filename>.gx Size: <file_size>\r\nFile selected\r\nok\r\n`

---

## 5. 検証済み事項

- OrcaSlicer から送信される `.gcode` ファイルを送信前に `xgcode 1.0`（`.gx`）コンテナへ自動ラップし、チャンク間ペーシング（`3ms`）＋ `~M29` 前待機（`150ms`）＋ `TCP_NODELAY` を適用することで、実機へのアップロード〜加熱〜造形開始まで正常に完了することを確認済み。
