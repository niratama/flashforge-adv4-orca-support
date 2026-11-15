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

## 将来拡張 TODO: パターン B（ネイティブ統合プラグイン）への段階的移行準備

OrcaSlicer 2.5.0+ の公式プラグイン仕様（`orca.pages.PagesPluginCapabilityBase` / `orca.printer_agent.PrinterAgentBase`）を踏まえ、現行のブリッジサーバー・パターン A プラグインを維持したまま、将来的なパターン B 化を見据えてモジュール分割とテスト拡充を進める。

### 設計方針メモ
- **移行戦略（ハイブリッド段階移行）**
  - **Step 1（UI 統合: `Pages`）**: `PagesPluginCapabilityBase`（`window.orca.postMessage` / `onMessage`）で専用タブを構築。遅延ロード・リロード時の再初期化に備え、状態は Python 側で保持し UI 起動時に `{type: "ready"}` で同期する。
  - **Step 2（完全ネイティブ: `PrinterAgent`）**: `PrinterAgentBase`（`orca.PluginType.PrinterConnection`）を実装し、`.gcode` → `.gx` ラップ・送信を直接ハンドリングする。なお OrcaSlicer 側で `Preferences -> Developer -> Experimental Features -> Use printer agents instead of print hosts`（デフォルト OFF）およびプリンター設定の `Printer Agent` 選択が必要なため、当面はパターン A（OctoPrint 互換受信）と共存・切替可能にする。
- **カメラ配信**: `Pages` タブではカメラ URL 直接参照を基本とし、必要に応じて `CameraRelay` 経由のフレーム配信（低 fps 上限）を選択できる構成を想定。

### 準備タスク（現行実装のリファクタリング・テスト拡充）
- [x] HTTP 非依存のプラグインランタイム／コントローラー層（シングルトン管理・設定読み書き・コマンドディスパッチ）を切り出し、[app.py](./app.py) と [orca_plugin/server.py](./orca_plugin/server.py) の共通ロジックを集約する
- [x] [templates/index.html](./templates/index.html) の通信処理をトランスポート層（`fetch` ベース / 将来の `window.orca` ベース）として抽象化する
- [x] 切り出したランタイム／コントローラー層およびエッジケース（再接続、ライブチューニング、スナップショット、`.gx` 変換）に対する単体テストを拡充する
- [ ] （実機検証時）OrcaSlicer 2.5.0 上で `orca.pages` / `orca.printer_agent` の実クラス・メソッドシグネチャを確認し、プロトタイプ検証を行う
