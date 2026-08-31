# Xiaoao Flight Mesh

小澳機票的可攜式搜尋節點。相同映像可跑在 Synology NAS、GitHub Actions
或一般 Linux，搜尋結果再由既有 Cloudflare Worker 匯入家庭機票雷達。

## v1 神價搜尋原則

「掃過目的地」不等於「掃過日期」。引擎會把假期展開成完整日期矩陣，再以
可續跑的 cursor 分批處理。例如 2026-12-19 至 12-27、旅程 4–8 天共有
15 組合法日期；49 個目的地 × 4 個出發地 × 3 個艙等就是 8,820 組，
而不是只抽一組日期後宣稱完成。

- 第一輪先讓每個目的地都取得一個日期觀察，之後持續補齊日期矩陣。
- 候選／比較中的行程可插隊複驗，但不會因此永遠跳過其他日期。
- 只有「相同旅客數、三人總價、含稅」才可進入價格比較。
- 快照只供即時顯示；標記為 snapshot，不能觸發神價通知。
- 至少五筆同類歷史才能判斷歷史低位；神價還必須在 15 分鐘內由兩個
  獨立來源支持，或由可購買的 live-offer API 確認。
- 來源價差超過 8% 會隔離為衝突，不會選較便宜的一筆偷偷通知。

## 價格來源

預設順序：

1. `google-playwright`：NAS Chromium 讀取 Google Flights 明確標示的
   航班結果卡，確認頁面顯示的旅客數後才把價格標成 family。
2. `serpapi-google-flights`：選填的獨立結構化來源。只有設定
   `SERPAPI_KEY` 才啟用。
3. `fast-flights`：速度快的補充來源；未能證實含稅家庭總價時只作線索，
   不可單獨觸發通知。

舊版 Skyscanner／Trip／Kayak／Expedia 公開頁「抓取頁面所有 HK$ 數字」
已退出預設鏈路。那種資料無法證明金額屬於哪一班、幾位旅客或是否含稅，
寧可少一筆，也不製造漂亮但不能買的假神價。

瀏覽器來源遇到 CAPTCHA、人機驗證或存取拒絕時會立即停止，不繞過網站保護。
單一來源連續失敗三次會暫停 30 分鐘；健康狀態可在 `/health` 稽核。

## NAS / 一般 Linux

1. 複製 `.env.example` 為 `.env`，設定至少 32 字元的
   `FLIGHT_MESH_TOKEN`。
2. 執行 `docker compose up -d --build`。
3. 只把 `127.0.0.1:8789` 接到既有 Cloudflare Tunnel，不要公開連接埠。

SQLite 快照保存在 Docker volume `flight-mesh-data`，重新部署不會清空。

## API

健康檢查（不含秘密）：

```
GET /health
```

建立完整日期矩陣的一頁：

```json
POST /plan
Authorization: Bearer <token>

{
  "plan": {
    "holidayStart": "2026-12-19",
    "holidayEnd": "2026-12-27",
    "minDays": 4,
    "maxDays": 8,
    "origins": ["HKG", "MFM", "CAN", "SZX"],
    "destinations": ["ICN", "KIX"],
    "cabins": ["economy", "business"],
    "adults": 2,
    "children": 1
  },
  "cursor": 0,
  "limit": 12,
  "completedKeys": [],
  "priorityKeys": []
}
```

搜尋一批精確行程：

```json
POST /search-batch
Authorization: Bearer <token>

{
  "searches": [{
    "origin": "HKG",
    "destination": "KUL",
    "outboundDate": "2026-12-19",
    "returnDate": "2026-12-25",
    "cabin": "economy",
    "adults": 2,
    "children": 1,
    "checkedBags": 1
  }]
}
```

每個來源獨立失敗；整批保留成功結果與 `failures`。全部 live source
暫時失敗時，可回傳 72 小時內的 last-known-good snapshot，但它不算驗價。

## 驗證

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
python -m py_compile xiaoao_mesh/*.py xiaoao_mesh/providers/*.py
```

GitHub Actions 沒有設定私人 bridge secrets 時只跑測試並安全閒置；公開倉庫
不保存 token 或 API key。

