# OrcaSlicer 2.5.0+ プラグイン化 TODO

WSL 上で手動起動しているブリッジサーバー（`app.py`）を、Windows 版 **OrcaSlicer 2.5.0+** の組み込み Python プラグインシステムへ統合するための作業計画です。

## 方針

- **第一段階（パターン A: ブリッジ自動起動プラグイン）**
  - 実機検証済みの [flashforge_client.py](./flashforge_client.py)・[app.py](./app.py)・[templates/index.html](./templates/index.html) をそのまま活かし、OrcaSlicer 起動時にローカルブリッジ（`127.0.0.1:5000`）をバックグラウンドで自動起動・終了するプラグインとして構成する。
  - OrcaSlicer からは従来通り OctoPrint 互換 API（`http://localhost:5000`）および Device タブで接続する。
- **将来拡張（パターン B: ネイティブ統合）**
  - 必要に応じて OrcaSlicer の `Printer Agents` / `Pages` capability を直接使い、ローカル HTTP サーバーを介さない形式への移行も検討する。

---

## タスクリスト

### Phase 1: OrcaSlicer 2.5.0 プラグイン仕様の確認
- [x] `orca-plugin.json` の必須フィールドとエントリポイント定義を確認する
- [x] `@orca.plugin` / `register_capabilities()` でバックグラウンド常駐に適した capability（`ScriptPluginCapabilityBase`）を確認する
- [x] ローカル配置形式（ディレクトリ + `.py`、PEP 723 依存宣言）と監査フック（`orca.request_permissions()`）の挙動を確認する

### Phase 2: プラグイン用パッケージ・エントリポイント実装
- [x] プラグインディレクトリ（`orca_plugin/`）とビルドスクリプト（`scripts/build_orca_plugin.py`）を作成する
- [x] プラグイン起動時にブリッジサーバーを `daemon=True` スレッドで起動し、終了時に `FlashforgeClient.close()`（`~M602`）を呼ぶライフサイクル処理を追加する
- [x] プラグイン配置先ディレクトリ基準で `config.json`（または [config-example.json](./config-example.json)）および [templates/index.html](./templates/index.html) を参照できるようパス解決を調整する
- [x] ポート `5000` が既に使用中の場合（別インスタンス起動中など）のエラーハンドリング／通知を追加する

### Phase 3: 依存関係と権限の整理
- [x] `orca.request_permissions()` で必要なネットワーク権限（プリンター `8899`、カメラ `8080`、ローカル待受 `5000`）を明示する
- [x] 標準ライブラリ（`http.server.ThreadingHTTPServer` + `email.parser.BytesParser`）へ置き換えて外部依存ゼロのプラグイン用サーバー（`orca_plugin/server.py`）を実装する

### Phase 4: Windows 環境での実機検証
- [x] Windows 側の `%APPDATA%\OrcaSlicer\orca_plugins\` へプラグインを配置する
- [x] OrcaSlicer 2.5.0 起動時にプラグイン認識・権限承認ダイアログが正常に完了することを確認する
- [x] Device タブのライブカメラ・ステータス表示・LED・一時停止／再開／キャンセルが動作することを確認する
- [x] `Print plate` から `.gcode` → `.gx` 自動ラップ・アップロード・造形開始まで完了することを確認する
- [x] OrcaSlicer 終了後に `~M602` が送られ、FlashPrint 等の別クライアントから再接続できることを確認する

### Phase 5: ドキュメント整備
- [x] [README.md](./README.md) に Windows 版 OrcaSlicer プラグインとしての導入・設定手順を追記する
- [x] 単体テスト（`uv run python -m unittest discover -s tests -v`）を更新・全件パスさせる

---

## 機能拡張 TODO: 印刷中の温度・プリント速度調整（ライブチューニング）

- [x] [scripts/tcp_proxy.py](./scripts/tcp_proxy.py) 経由で FlashPrint を Adventurer 4 に接続し、印刷中の以下操作時の TCP `8899` コマンドと応答をキャプチャする
  - ヘッド目標温度の変更: `~M104 S<temp> T0\r\n` → `CMD M104 Received.\r\nok`
  - プラットフォーム目標温度の変更: `~M140 S<temp>\r\n` → `CMD M140 Received.\r\nok`
  - プリント速度（%）の変更: `~M663 S<percent>\r\n` → `CMD M663 Received.\r\nok`
- [x] 確認できた仕様を [PROTOCOL.md](./PROTOCOL.md) に追記する
- [x] [flashforge_client.py](./flashforge_client.py)・[app.py](./app.py)・[orca_plugin/server.py](./orca_plugin/server.py)・[templates/index.html](./templates/index.html) に温度設定／プリント速度変更の API と UI を実装し、単体テストを追加する

---

## 機能拡張 完了済み（直近の追加実装）

- [x] `xgcode 1.0`（`.gx`）ヘッダーの拡充（OrcaSlicer G-code からのメタデータ・サムネイル抽出、`multi_extruder_type` の raft / support フラグ、`perimeter_shells` 設定）と Web UI での造形ジョブ情報・サムネイル表示
- [x] セキュリティ・認証強化（`server.api_key` による POST 認証、Cross-origin POST 検証、PNG zlib 展開サイズ上限、カメラ URL バリデーション）
- [x] プラグイン設定 UI（[orca_plugin/plugin.py](./orca_plugin/plugin.py) の `has_config_ui()` / `get_config_ui()` / `get_default_config()`、UI からの `api_key` 除外、設定マージ）と起動時自動開始・終了時ハング修正（単体テスト 42 件パス）

---

## 将来拡張 TODO: パターン B（ネイティブ統合プラグイン）への段階的移行

OrcaSlicer 2.5.0+ の公式プラグイン仕様（`orca.pages.PagesPluginCapabilityBase` / `orca.printer_agent.PrinterAgentBase`）を踏まえた段階的移行計画です。

### 設計方針メモ
- **移行戦略（ハイブリッド段階移行）**
  - **Phase 1（`Pages` 先行 + ローカル HTTP 共存）**:
    - `PagesPluginCapabilityBase`（`window.orca.postMessage` / `onMessage`）で OrcaSlicer 内に専用タブを追加し、共有の `BridgeController`（`handle_message()`）と接続する。
    - `Print plate`（OctoPrint 互換アップロード）およびカメラストリーム／スナップショット画像配信は、Phase 1 では共存するローカル HTTP サーバー（`http://127.0.0.1:<port>/api/camera/...`）をそのまま参照・活用する。
    - 遅延ロード・リロード時の再初期化に備え、状態は Python 側（`BridgeController`）で保持し、UI 起動時の `ready` / 定期ポーリングで同期する。
  - **Phase 2（`PrinterAgent` 統合 + HTTP サーバーのオプション化）**:
    - `PrinterAgentBase`（`orca.PluginType.PrinterConnection`）を実装し、`.gcode` → `.gx` ラップ・送信を直接ハンドリングする。
    - `use_printer_agents`（`Preferences -> Developer -> Experimental Features`、デフォルト OFF）の有効化が必要なため、当面は設定で OctoPrint HTTP サーバーの起動有無を切り替えられるようにする。

### 準備タスク（完了済み）
- [x] HTTP 非依存のコントローラー層（[bridge_controller.py](./bridge_controller.py)）を切り出し、[app.py](./app.py) と [orca_plugin/server.py](./orca_plugin/server.py) の共通ロジックを集約する
- [x] [templates/index.html](./templates/index.html) の通信処理を `BridgeTransport`（`fetch` / `window.orca`）として抽象化する
- [x] コントローラー層・`.gx` 変換・セキュリティ・プラグインライフサイクルの単体テストを拡充する（42 件パス）

### Phase 1 タスク（`Pages` 専用タブ実装）
- [x] [templates/index.html](./templates/index.html) の初期 `<img id="camera-img">` およびスナップショット画像 URL を `BridgeTransport` 経由でローカル HTTP ベース URL（`http://127.0.0.1:<port>`）対応にする
- [x] [orca_plugin/plugin.py](./orca_plugin/plugin.py)（または専用モジュール）に `PagesPluginCapabilityBase` サブクラスを追加し、`on_message()` → `BridgeController.handle_message()` → `post_message()` を接続する
- [x] `orca` モジュールをモックした `Pages` capability の単体テストを追加し、`scripts/build_orca_plugin.py` でプラグインバンドルを確認する
- [x] Windows 版 OrcaSlicer 2.5.0+ 実機で `Pages` タブの表示・操作・カメラ映像・リロード時同期を検証する

### Phase 2 タスク（`PrinterAgent` ネイティブ統合）
- [x] OrcaSlicer ソース（`PrinterAgentPluginCapability.cpp` / `IPrinterAgent.hpp` / `MoonrakerPrinterAgent.cpp`）から `orca.printer_agent` の API・`PrintParams`・`push_status` JSON 仕様を調査・確定する
- [x] **Step 2-1**: 共有ランタイム（`BridgeController`）を `BridgeServer` から分離し、`server.enabled`（デフォルト `true`）でローカル HTTP サーバーをオプション化＋HTTP レス `Pages` 向けスナップショット取得 RPC（`camera.snapshots.get`）を追加する
- [x] **Step 2-2**: `orca_plugin/printer_agent.py`（`PrinterAgentMixin` + `FlashforgePrinterAgent`）および `FlashforgeClient.send_file()` の進捗・キャンセルコールバック対応、`.3mf` 内 G-code 抽出を実装する
- [x] **Step 2-3**: [orca_plugin/plugin.py](./orca_plugin/plugin.py) への `FlashforgePrinterAgent` 登録、[scripts/build_orca_plugin.py](./scripts/build_orca_plugin.py) 同梱、単体テスト（`tests/test_printer_agent.py`）を追加する
- [x] **Step 2-4**: `server.enabled = false` 時でも `Device` タブ・`Adventurer 4` タブ・印刷中自動スナップショットが Adventurer 4 カメラ（`:8080`、同時接続 1 本制限）を競合せず共有できるよう、カメラリレー専用の動的空きポート（`127.0.0.1:0`）を軽量起動する仕組みを追加する
- [ ] **Step 2-5**: Windows 版 OrcaSlicer 2.5.0+ 実機で `use_printer_agents` 有効時の `Print plate`（確認済み）・Lamp（確認済み）・カメラ同時表示（確認済み）・残りの Device タブ制御を最終検証する

---

## 機能拡張 TODO: Device タブ制御拡充 & Adventurer 4 追加コマンド

`Parallel-7/flashforge-api-docs`（`networkserver_commands_adventurer4.yaml`）および実機検証をもとに、Adventurer 4（TCP `8899`）のリモート操作を拡充する。

### 1. Device タブ連携の拡充（`orca_plugin/printer_agent.py`）
- [x] **速度メーター（`100%`）**: `print.command == "print_speed"`（Silent 50% / Standard 100% / Sport 124% / Ludicrous 166%）を `~M663 S<percent>` へ接続し、`push_status` の `spd_lvl` / `spd_mag` と同期する
- [x] **冷却ファン（`Fan`）**: `gcode_line` の `M106` / `M107` を Adventurer 4 の `~M106`（ON）/ `~M107`（OFF）へ接続する
- [x] **ホーム（`🏠`）＆ XY / Bed(Z) 軸移動**: `command_go_home` / `command_axis_control`（`~G28`, `~G91` → `~G1` → `~G90`）を **待機中（`READY`）限定** で許可し、印刷中の誤操作をブロックする

### 2. 安全・便利機能（`Adventurer 4` タブ / `flashforge_client.py`）
- [ ] **ステッパーモーター解除（`~M18`）** / 励磁（`~M17`）ボタン
- [ ] **緊急停止（`~M112`）** ボタン（誤操作防止の確認ダイアログ付き）
- [ ] **現在座標表示（`~M114`）**（必要に応じてステータスへ追加）

### 3. 内部ストレージファイル管理（実機検証済み）
- [x] `~M661`（内部ファイル一覧取得）・`~M662`（サムネイル取得）の実機応答を検証し、`PROTOCOL.md` に記録する
- [x] 応答形式が確認できたら、`Adventurer 4` タブから過去アップロード済みファイルの一覧表示・サムネイル表示・再印刷（`~M23`）ができるようにする
