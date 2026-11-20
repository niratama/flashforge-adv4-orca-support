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
| `~M105\r\n` | ヘッド・プラットフォーム温度 | `CMD M105 Received.\r\nT0:29/0 B:26/0\r\nok` |
| `~M27\r\n` | SD/内部ストレージ造形進捗 | `CMD M27 Received.\r\nSD printing byte 0/100\r\nLayer: 0/0\r\nok` |
| `~M119\r\n` | 機械状態・LED・現在ファイル | `Endstop: ...` / `MachineStatus: READY` / `MoveMode: READY` / `LED: 0` / `CurrentFile: ` 〜 `ok` |
| `~M25\r\n` | 一時停止 | `ok` |
| `~M24\r\n` | 再開 | `ok` |
| `~M26\r\n` | 造形キャンセル | `ok` |
| `~M146 r255 g255 b255 F0\r\n` | 庫内 LED 点灯（消灯は `r0 g0 b0`） | `ok` |
| `~M104 S230 T0\r\n` | ヘッド目標温度の設定（印刷中も可） | `CMD M104 Received.\r\nok` |
| `~M140 S60\r\n` | プラットフォーム目標温度の設定（印刷中も可） | `CMD M140 Received.\r\nok` |
| `~M663 S90\r\n` | プリント速度（%）の変更（例: `90`, `100`） | `CMD M663 Received.\r\nok` |

※ FlashPrint の接続時には上記に加えて `~M650\r\n`（`X: 1.0 Y: 0.5`）および `~M114\r\n`（現在座標）も送信されていました。

---

## 4. ファイル転送・造形開始（`~M28` 〜 `~M29` 〜 `~M23`）

FlashPrint（`450,932` バイトの `.gx` ファイル送信時）のキャプチャおよび OrcaSlicer からの実機検証で確認できた実シーケンスは以下の通りです。

1. **転送開始 (`~M28`)**
   - 送信: `~M28 <file_size> 0:/user/<filename>.gx\r\n`
   - 応答: `CMD M28 Received.\r\nWriting to file: /data/<filename>.gx\r\nok\r\n`
2. **ファイル本体の送信（`raw` ストリーム / `.gx` コンテナ）**
   - `<file_size>` バイトの **生ファイルデータそのまま**（`xgcode 1.0\n\x00` ヘッダ 58 バイト + 80×60 24bit BMP `14,454` バイト + G-code 本体の `.gx` バイナリ）を送信する。
   - 58 バイトの `xgcode 1.0` ヘッダ（リトルエンディアン）の `0x1C..0x39` にはメタデータ 12 フィールド（`print_time` [s], `filament_0_usage` [mm], `filament_1_usage` [mm], `multi_extruder_type`（ラフトあり `5` / なし `1`）, `layer_height` [µm], `reserved0`, `perimeter_shells`, `print_speed` [mm/s], `hotbed_temp` [°C], `extruder_0_temp` [°C], `extruder_1_temp` [°C], `reserved1`）が格納される。`flashforge_client.py` では OrcaSlicer の G-code から `print_time`・`filament_0_usage`・`multi_extruder_type`（`raft_layers`）・`layer_height`・`perimeter_shells`（`wall_loops`）・`print_speed`・`hotbed_temp`・`extruder_0_temp` および埋め込み PNG サムネイル（80×60 24bit BMP 変換）を自動設定する。
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

---

## 6. `.gx` (`xgcode 1.0`) ファイルフォーマット仕様と検証知見

FlashPrint（`ffslicer 2.4.4`）の出力実ファイル、`maugier/rust-xgcode`、Slic3r #4869、および Adventurer 4 実機での表示確認から得られた `.gx` フォーマットの詳細は以下のとおりです。

### 6.1 全体構造

```text
[0x0000 .. 0x003A]  58 bytes     xgcode 1.0 バイナリヘッダ（リトルエンディアン）
[0x003A .. 0x38B0]  14,454 bytes 80×60 24bit 非圧縮 BMP サムネイル（54B ヘッダ + 14,400B BGR ピクセル）
[0x38B0 .. gcode_offset2]        （ffslicer 2.4.4 のみ）追加の 64×64 PNG サムネイル
[gcode_offset2 .. EOF]           プレーンテキスト ASCII G-code 本体
```

### 6.2 58 バイトヘッダ (`0x00..0x39`) のレイアウト

| オフセット | サイズ / 型 | フィールド名 | 内容・単位・実機での扱い |
| :--- | :--- | :--- | :--- |
| `0x00` | 16 bytes | `magic` | `"xgcode 1.0\n\x00\x00\x00\x00\x00"` 固定 |
| `0x10` | `u32` LE | `bmp_offset` | BMP 開始オフセット（`0x3A` = `58`） |
| `0x14` | `u32` LE | `gcode_offset1` | BMP 終端オフセット（`14512` = `0x38B0`）。追加 PNG がある場合はその開始位置 |
| `0x18` | `u32` LE | `gcode_offset2` | G-code 本体の開始オフセット（追加 PNG が無ければ `14512`、ffslicer 2.4.4 では PNG 終端位置） |
| `0x1C` | `u32` LE | `print_time` | 推定造形時間（秒）— **実機画面に表示** |
| `0x20` | `u32` LE | `filament_0_usage` | 右/主エクストルーダのフィラメント使用量（mm）— **実機画面に m 換算で表示** |
| `0x24` | `u32` LE | `filament_1_usage` | 左/第2エクストルーダのフィラメント使用量（mm、Adv4 では `0`） |
| `0x28` | `u16` LE | `multi_extruder_type` | エクストルーダ／スライスフラグ（`0x01` 主ノズル \| `0x04` ラフト \| `0x08` サポート: なし `1` / ラフトのみ `5` / ラフト+サポート `13`） |
| `0x2A` | `u16` LE | `layer_height` | レイヤー高さ（ミクロン / µm、例: `200` = `0.20 mm`）— **実機画面に表示** |
| `0x2C` | `u16` LE | `reserved0` | 予約（`0`） |
| `0x2E` | `u16` LE | `perimeter_shells` | 外壁シェル数（例: `2`, `3`） |
| `0x30` | `u16` LE | `print_speed` | 造形速度（mm/s）— **実機画面に表示** |
| `0x32` | `u16` LE | `hotbed_temp` | プラットフォーム目標温度（°C）— **実機画面に表示** |
| `0x34` | `u16` LE | `extruder_0_temp` | 右/主ノズル目標温度（°C）— **実機画面に表示** |
| `0x36` | `u16` LE | `extruder_1_temp` | 左/第2ノズル目標温度（°C、Adv4 では未使用。ffslicer 2.4.4 は既定値 `210` を出力） |
| `0x38` | `u16` LE | `reserved1` | 予約（ffslicer 2.4.4 では `0xFF01` / `65281`、旧バージョンでは `0`） |

### 6.3 FlashPrint (`ffslicer 2.4.4`) 実ファイル比較で判明した挙動

- **`0x14` と `0x18` の役割の違い**: 旧バージョンの FlashPrint（Slic3r #4869 当時）はどちらも `14512` を出力していたが、ffslicer 2.4.4 では `0x14`（`14512`）の直後に 64×64 PNG が埋め込まれ、`0x18` が実際の G-code 開始位置を指す。追加 PNG を省略して `0x14 == 0x18 == 14512` としても Adventurer 4 実機は問題なくプレビュー表示・造形できる。
- **`0x28` (`multi_extruder_type`) とラフト・サポート有無**: 同一モデル・同一条件でオプションを切り替えた結果、ラフトなし・サポートなしで `1`（`0x0001`）、ラフトあり・サポートなしで `5`（`0x0005`）、ラフトあり・サポートありで `13`（`0x000D`）になることを確認（`bit 0` = `0x01` 主/右エクストルーダ有効、`bit 2` = `0x04` ラフト有効、`bit 3` = `0x08` サポート有効）。
- **`0x2E` (`perimeter_shells`)**: シェル数設定（2 → 3）に連動してそのまま格納される。
- **壁（ウォール）と最初の数レイヤー低速化**: 専用のヘッダフラグは存在せず、`print_time` と `filament_0_usage` にのみ反映される。
- **サムネイル BMP**: 80×60・24bit RGB 非圧縮 BMP（下から上へのボトムアップ、BGR 順）。OrcaSlicer が G-code 内に埋め込む `; thumbnail begin ...`（Base64 PNG）を標準ライブラリのみでデコード・アスペクト比維持リサイズして変換することで、実機タッチパネル上にカラープレビューが表示されることを確認済み。

---

## 7. 内部ストレージファイル一覧・サムネイル取得（`~M661` / `~M662`）

Adventurer 4 実機への直接プローブで確認したバイナリ応答フォーマットは以下のとおりです。いずれもテキストヘッダ（`ok\r\n`）の **直後** にビッグエンディアンのバイナリ本体が続きます。

### 7.1 ファイル一覧取得 (`~M661`)
- **送信**: `~M661\r\n`
- **応答**:
  ```text
  CMD M661 Received.\r\n
  ok\r\n
  <4B magic: 0x44 0xAA 0xAA 0x44 ("D\xaa\xaaD")>
  <4B uint32 BE: file_count>
  [file_count 回繰り返し]:
    <4B delimiter: 0x3A 0x3A 0xA3 0xA3 ("::\xa3\xa3")>
    <4B uint32 BE: path_byte_len>
    <path_byte_len bytes: UTF-8 ファイルパス（例: "/data/Buckle_Box_v2.gx"、日本語名可）>
  ```

### 7.2 サムネイル取得 (`~M662`)
- **送信**: `~M662 /data/<filename>.gx\r\n`
- **応答**:
  ```text
  CMD M662 Received.\r\n
  ok\r\n
  <4B magic: 0x2A 0x2A 0xA2 0xA2 ("**\xa2\xa2")>
  <4B uint32 BE: bmp_byte_len (例: 14454 = 0x00003876)>
  <bmp_byte_len bytes: 80×60 24bit 非圧縮 BMP ("BM...")>
  ```
- 取得した BMP はそのまま `image/bmp` として Web UI / `Pages` タブに表示可能。また既存ファイルの再印刷は `~M23 0:/user/<filename>.gx\r\n` で開始できる。
