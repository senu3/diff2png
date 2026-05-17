# diff2png

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

## 差分ソース

解析時に次の差分ソースを選択できます。

- 作業ツリー: `git diff HEAD`
- 単一コミット: `git diff <commit>^ <commit>`
- コミット比較: `git diff <base> <target>`

単一コミット・コミット比較では、リポジトリのコミット一覧から選択して解析します。

## 表示モードについて

- `通常表示 (file)`
  - 追加行をハイライトして表示
  - `+0`（削除のみ）のhunkは一覧に表示しない
- `パッチ表示 (patch)`
  - `+` / `-` / 文脈行をそのまま表示

## 出力設定

| 定数              | 初期値             | 説明                                        |
| ----------------- | ------------------ | ------------------------------------------- |
| `CONTEXT_LINES`   | `5`                | hunk前後の余白行数                          |
| `MERGE_THRESHOLD` | `8`                | 近接hunk統合の閾値（行数）                  |
| `HTML_WIDTH`      | `960`              | PNG横幅(px)                                 |
| `OUTPUT_DIR_NAME` | `diff_screenshots` | PNG保存先（アプリディレクトリ配下の相対名） |
