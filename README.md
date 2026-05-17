# diff shot

`git diff HEAD` の変更hunkを確認・選択してPNG出力するエビデンスツールです。

## セットアップ

```bash
uv add flask playwright
uv run playwright install chromium
```

## 使い方

```bash
uv run python app.py
```

ブラウザが自動で開きます（http://127.0.0.1:5000）。

1. **リポジトリパス** に対象リポジトリの絶対パスを入力
2. **解析** ボタンを押すと変更hunk一覧とプレビューが表示される
3. チェックボックスで出力するhunkを選択
4. **選択を出力** または **全て出力** でPNG生成

PNGは `diff_screenshots/` フォルダに出力されます。

## ファイル構成

```
diff_shot/
├── app.py
├── templates/
│   └── index.html
└── diff_screenshots/   ← 自動生成
```

## 設定（app.py 先頭）

| 定数              | 初期値 | 説明                       |
| ----------------- | ------ | -------------------------- |
| `CONTEXT_LINES`   | `5`    | hunk前後の余白行数         |
| `MERGE_THRESHOLD` | `8`    | 近接hunk統合の閾値（行数） |
| `HTML_WIDTH`      | `960`  | PNG横幅(px)                |
