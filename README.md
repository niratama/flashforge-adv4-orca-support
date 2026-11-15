# Flashforge Adventurer 4 - OrcaSlicer ローカルブリッジサーバー

Flashforge Adventurer 4 を OrcaSlicer から制御・監視するための軽量ローカルブリッジサーバー（Web UI + OctoPrint 互換 API）です。

## ディレクトリ構成

```text
flashforge-adv4-orca-support/
├── app.py                  # Flask Web サーバー & OctoPrint 互換 API
├── config-example.json     # プリンター IP / ポート / カメラ URL / サーバー設定テンプレート
├── flashforge_client.py    # Flashforge TCP (8899) クライアント
├── PROTOCOL.md             # Adventurer 4 TCP 通信仕様メモ
├── pyproject.toml          # プロジェクト定義 & 依存パッケージ (uv)
├── uv.lock                 # ロックファイル
├── scripts/
│   └── tcp_proxy.py        # TCP 8899 通信解析用プロキシ
├── templates/
│   └── index.html          # Device タブ用ダッシュボード UI
└── tests/
    ├── test_app.py
    └── test_flashforge_client.py
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
    "host": "0.0.0.0",
    "port": 5000,
    "debug": false
  }
}
```

環境変数（`PRINTER_IP`, `PRINTER_PORT`, `CAMERA_URL`, `SERVER_PORT`）でも上書きできます。

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
