# Flashforge Adventurer 4 - OrcaSlicer ローカルブリッジサーバー

Flashforge Adventurer 4 を OrcaSlicer から制御・監視するための軽量ローカルブリッジサーバー（Web UI + OctoPrint 互換 API）です。

## 主な機能

- **OrcaSlicer 連携（OctoPrint 互換 API）**: スライス後の `.gcode` を自動で Adventurer 4 向け `.gx`（`xgcode 1.0` + サムネイル BMP）へ変換して転送・印刷開始
- **ライブカメラ中継（`/api/camera/stream`）**: Adventurer 4 のカメラ（`:8080`）の同時接続数 1 本制限に対応するため、サーバー側で 1 本だけ接続して複数画面へ配信
- **静止画の定期保存 & 履歴表示**: 印刷中（または常時）に一定間隔で JPEG を自動保存し、スライダー・サムネイルで履歴を確認可能（「撮影」ボタンで手動保存も可）
- **温度推移グラフ**: ヘッド・プラットフォームの実測値 / 目標値の推移をサーバー側で直近 4 時間分（設定変更可）保持してリアルタイム描画
- **本体制御 & 日本語ステータス**: 一時停止 / 再開 / 中止 / LED 切替、印刷中のヘッド目標温度・プラットフォーム目標温度・プリント速度 (%) の調整、`BUILDING_FROM_SD` などの状態コードの日本語表示

## ディレクトリ構成

```text
flashforge-adv4-orca-support/
├── app.py                  # Flask Web サーバー & OctoPrint 互換 API
├── bridge_services.py      # カメラ中継・静止画保存・温度履歴（標準ライブラリのみ）
├── config-example.json     # プリンター・カメラ・履歴・サーバー設定テンプレート
├── flashforge_client.py    # Flashforge TCP (8899) クライアント
├── orca_plugin/            # OrcaSlicer 2.5.0+ 用ゼロ依存プラグイン
│   ├── orca-plugin.json
│   ├── plugin.py
│   └── server.py
├── PROTOCOL.md             # Adventurer 4 TCP 通信仕様メモ
├── pyproject.toml          # プロジェクト定義 & 依存パッケージ (uv)
├── uv.lock                 # ロックファイル
├── scripts/
│   ├── build_orca_plugin.py # dist/flashforge_adv4_bridge/ 生成スクリプト
│   └── tcp_proxy.py        # TCP 8899 通信解析用プロキシ
├── templates/
│   └── index.html          # Device タブ用ダッシュボード UI
└── tests/
    ├── test_app.py
    ├── test_bridge_services.py
    ├── test_flashforge_client.py
    └── test_plugin_server.py
```

## 1. セットアップと起動

Python 3.10 以上と [uv](https://docs.astral.sh/uv/) を推奨します。

```bash
uv sync
```

（`uv` を使わない場合は仮想環境を作成して `pip install -r requirements.txt` でも導入できます。）

### `config.json` の作成・編集

`config-example.json` を `config.json` にコピーし、`printer.ip` と `printer.camera_url` をご自身の環境に合わせて変更してください（`config.json` は `.gitignore` 対象です）。

```bash
cp config-example.json config.json
```

```json
{
  "printer": {
    "ip": "192.168.1.100",
    "port": 8899,
    "timeout": 5.0,
    "transfer_timeout": 30.0,
    "camera_url": "http://192.168.1.100:8080/?action=stream"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 5000,
    "debug": false
  },
  "camera": {
    "snapshot_enabled": true,
    "snapshot_only_while_printing": true,
    "snapshot_interval_seconds": 60,
    "snapshot_max_count": 200,
    "snapshot_dir": "snapshots"
  },
  "history": {
    "temperature_hours": 4.0,
    "sample_interval_seconds": 5.0
  }
}
```

- `server.host` / `server.port`: 待受アドレス・ポート（デフォルトは安全のため `127.0.0.1:5000`。別 PC から接続する場合のみ `0.0.0.0` 等に変更してください）
- `server.api_key`（任意）: 設定した場合、制御・ファイルアップロード等の `POST` リクエストで `X-Api-Key`（または `Authorization: Bearer ...`）ヘッダによる認証を要求します。
  - **未設定の場合（通常のローカル利用）**: キー入力は一切不要です（デフォルトの `127.0.0.1` 待受＋同一オリジン検証により、ローカル PC 内での利用やプラグイン利用はキー無しで安全に動作します）。
  - **設定した場合（LAN 公開時など）**: OrcaSlicer の OctoPrint 接続設定および Web UI / Device タブ右上の **API Key** ボタンに初回のみ入力・保存してください（`localStorage` に記憶されるため次回以降は入力不要です）。
  - キーは以下のコマンド等で生成できます：
    ```bash
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    # または: openssl rand -hex 32
    ```
- `server.max_upload_bytes`（任意）: `POST /api/files/local` の最大アップロードサイズ（バイト単位、デフォルト `104857600` = 100 MiB）
- `camera.snapshot_only_while_printing`: `true` で印刷中のみ自動保存、`false` で常時保存
- `camera.snapshot_interval_seconds` / `snapshot_max_count`: 保存間隔（秒）と最大保持枚数（超過分は古い順に削除）
- `history.temperature_hours` / `sample_interval_seconds`: 温度グラフの保持時間（時間）とサンプリング間隔（秒）

環境変数（`PRINTER_IP`, `PRINTER_PORT`, `CAMERA_URL`, `SERVER_HOST`, `SERVER_PORT`, `BRIDGE_API_KEY`）でも上書きできます。

> **セキュリティ仕様**: `POST` リクエストにはブラウザからの CSRF 対策として `Origin` / `Referer` の同一オリジン検証が適用されます（OrcaSlicer や `curl` 等の非ブラウザクライアントはそのまま利用可能です）。また、`printer.camera_url` は `http://` / `https://` のみ許可されます。

### サーバー起動

```bash
uv run python app.py
```

TCP 通信やファイル転送の詳細ログを出したい場合は `LOG_LEVEL=DEBUG` を指定して起動します。

```bash
LOG_LEVEL=DEBUG uv run python app.py
```

ブラウザで `http://localhost:5000` を開くとダッシュボードが表示されます。

### テスト実行

```bash
uv run python -m unittest discover -s tests -v
```

## 2. OrcaSlicer の設定手順

1. OrcaSlicer 左上のプリンター設定横にある **Wi-Fi / 接続アイコン** をクリックします。
2. **Host Type（ホストタイプ）** で **`OctoPrint`** を選択します。
3. **Hostname, IP or URL** に **`http://localhost:5000`**（別 PC で動かす場合はその IP とポート）を入力します。
4. **API Key** は任意の文字列（または空欄、OrcaSlicer が要求する場合は `dummy` など）を入力し、**Test** を押して接続成功を確認します。
5. **Device UI（デバイス画面）** を利用する場合も同じく `http://localhost:5000` が OrcaSlicer 内の **Device タブ** に表示されます。
6. スライス後、**Print plate**（または Upload and Print）を実行すると `POST /api/files/local` 経由で Adventurer 4 へ Gコードが転送され、印刷が開始されます。

## 3. ダッシュボードの使い方

ブラウザ（`http://localhost:5000`）または OrcaSlicer の **Device タブ** から以下の機能を利用できます。

- **ライブカメラ**
  - ヘッダーのステータスバッジでカメラの接続状態を確認できます（`受信中 (視聴:N)` / `応答待ち` / `切断 / 再接続待ち` / `待機中`）。マウスを乗せると最終フレームからの経過秒数やエラー詳細が表示されます。
  - **撮影** ボタンで現在の映像を静止画として即座に保存できます。
  - **再接続** ボタンでサーバーからプリンタ側カメラへの接続を張り直します（切断時も 3 秒後に自動で再試行します）。
  - FlashPrint など別アプリが直接 `:8080` を開いていると 1 接続枠が埋まり接続できないため、併用時はご注意ください。
- **ステータス & 操作**
  - ヘッド / プラットフォームの現在温度・目標温度、印刷進捗（%）、印刷中のファイル名、日本語化された本体状態（`待機中` / `印刷中` / `印刷完了` / `一時停止中` / `未接続`）を表示します。
  - **一時停止** / **再開** / **印刷中止** / **LED: ON/OFF** ボタンで本体を操作できるほか、印刷中に **ヘッド目標温度**・**プラットフォーム目標温度**・**プリント速度 (%)** を変更できます。
- **温度推移グラフ**
  - ヘッド・プラットフォームの実測値（実線）と目標値（破線）の推移を表示します。
  - 右上のプルダウンで表示範囲（直近 `30分` / `1時間` / `2時間` / `4時間`）を切り替えられ、グラフ上にカーソルを合わせるとその時刻の温度を確認できます。
- **静止画履歴**
  - 自動保存または手動撮影された静止画を、スライダー・前後（`◀` / `▶`）ボタン・下部サムネイル一覧から切り替えて確認できます。
  - 最新の履歴を取り込みたい場合は **更新** ボタン（自動でも 15 秒ごとに更新）を使用してください。

## 4. Windows 版 OrcaSlicer 2.5.0+ プラグインとして使う（パターン A）

外部ライブラリ不要（Python 標準ライブラリのみ）で動作するプラグインバンドルを生成できます。

```bash
uv run python scripts/build_orca_plugin.py
```

生成された `dist/flashforge_adv4_bridge/` フォルダを Windows 側の OrcaSlicer プラグインディレクトリへコピーし、`config.json`（無ければ `config-example.json` をリネーム）にプリンター IP を設定してください：

```text
%APPDATA%\OrcaSlicer\orca_plugins\flashforge_adv4_bridge\
```

OrcaSlicer を起動するとバックグラウンドで `http://localhost:5000` が自動起動します（初回のみネットワーク通信等の権限確認ダイアログが表示されます）。

## 5. 開発・解析用 TCP プロキシ（`scripts/tcp_proxy.py`）

FlashPrint とプリンター間の TCP `8899` 通信をキャプチャしてプロトコルを確認するためのツールです。

```bash
uv run python -B scripts/tcp_proxy.py --target-host 192.168.1.25 | tee tmp/proxy.log
```

- `--target-host`: 接続先プリンターの IP（デフォルト: `192.168.1.25`）
- `--target-port`: 接続先ポート（デフォルト: `8899`）
- `--listen-host` / `--listen-port`: プロキシの待受アドレス・ポート（デフォルト: `127.0.0.1:8899`。別端末や Windows 側から WSL 上のプロキシへ接続する場合は `--listen-host 0.0.0.0` を指定してください）
- `--verbose-all`: 大きなファイル転送チャンクも含め全パケットを出力

**キャプチャ手順の注意点:**
1. Adventurer 4 の制御セッション（`~M601 S1`）は 1 接続限定のため、実行前に `app.py` を停止してください。
2. FlashPrint の接続先 IP に `127.0.0.1`（Windows から WSL の `127.0.0.1:8899` に繋がらない場合は `ip addr` で確認した WSL の IP）を指定して操作します。

## ライセンス

[MIT License](LICENSE)
