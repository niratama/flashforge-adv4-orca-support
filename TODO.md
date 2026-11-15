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
- [ ] `orca-plugin.json` の必須フィールドとエントリポイント定義を確認する
- [ ] `@orca.plugin` / `register_capabilities()` でバックグラウンド常駐に適した capability（`Scripts` / `Pages` / `Printer Agents` 等）を確認する
- [ ] ローカル配置形式（ディレクトリ + `.py` / `.whl`、PEP 723 依存宣言）と監査フック（`orca.request_permissions()`）の挙動を確認する

### Phase 2: プラグイン用パッケージ・エントリポイント実装
- [ ] プラグインディレクトリ（または `.whl` 用構成）を作成する
- [ ] プラグイン起動時にブリッジサーバーを `daemon=True` スレッドで起動し、終了時に `FlashforgeClient.close()`（`~M602`）を呼ぶライフサイクル処理を追加する
- [ ] プラグイン配置先ディレクトリ基準で `config.json`（または [config-example.json](./config-example.json)）および [templates/index.html](./templates/index.html) を参照できるようパス解決を調整する
- [ ] ポート `5000` が既に使用中の場合（別インスタンス起動中など）のエラーハンドリング／通知を追加する

### Phase 3: 依存関係と権限の整理
- [ ] `orca.request_permissions()` で必要なネットワーク権限（プリンター `8899`、カメラ `8080`、ローカル待受 `5000`）を明示する
- [ ] `Flask` 依存をプラグイン側で解決するか、標準ライブラリ（`http.server.ThreadingHTTPServer`）へ置き換えて外部依存ゼロにするかを決定・実装する

### Phase 4: Windows 環境での実機検証
- [ ] Windows 側の `%APPDATA%\OrcaSlicer\orca_plugins\` へプラグインを配置する
- [ ] OrcaSlicer 2.5.0 起動時にプラグイン認識・権限承認ダイアログが正常に完了することを確認する
- [ ] Device タブのライブカメラ・ステータス表示・LED・一時停止／再開／キャンセルが動作することを確認する
- [ ] `Print plate` から `.gcode` → `.gx` 自動ラップ・アップロード・造形開始まで完了することを確認する
- [ ] OrcaSlicer 終了後に `~M602` が送られ、FlashPrint 等の別クライアントから再接続できることを確認する

### Phase 5: ドキュメント整備
- [ ] [README.md](./README.md) に Windows 版 OrcaSlicer プラグインとしての導入・設定手順を追記する
- [ ] 単体テスト（`uv run python -m unittest discover -s tests -v`）を更新・全件パスさせる
