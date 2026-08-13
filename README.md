# Xiaoao Flight Mesh

MIT 授權的可攜式機票掃描節點。相同映像可跑在 NAS、公開 GitHub Actions
標準執行器、Oracle Always Free VM，並由現有 Cloudflare Worker 依序容錯。

## 來源

- `fast-flights`：MIT 專案，直接讀取 Google Flights 公開回應，不開瀏覽器。
- `skyscanner`、`trip`、`kayak`、`expedia`：用 Playwright 讀取公開搜尋頁的 HKD 參考價。

瀏覽器來源只使用一般公開頁面；遇到 CAPTCHA、人機驗證或存取拒絕時立即停止該來源，
不繞過網站保護。來源頁結構與價格範圍隨時可能改變，因此所有結果都只標示為參考價，
仍須在航空公司官網或 OTA 核對含稅、行李與退改條款。

## NAS / 一般 Linux

1. 複製 `.env.example` 為 `.env` 並放入長隨機字串。
2. 執行 `docker compose up -d --build`。
3. 只把 `127.0.0.1:8789` 接到既有 Cloudflare Tunnel，不要直接公開此連接埠。

健康檢查不含秘密：`GET /health`。搜尋使用 `POST /search-batch` 並要求
`Authorization: Bearer <FLIGHT_MESH_TOKEN>`。

## 節點矩陣

| 節點 | 狀態 | 用途 |
|---|---|---|
| NAS Docker | 本目錄 `docker-compose.yml` | 首選自架節點，持續運作 |
| GitHub Actions | `.github/workflows/flight-mesh.yml` | 公開 repo 的排程／手動備援 |
| Oracle VM | `deploy/oracle/docker-compose.yml` | 免費 VM 的第二個自架地點 |
| Cloudflare Browser | 既有 `xiaoao-flight-browser` | 最後的 Google 瀏覽器備援 |

GitHub 與 Oracle 都需要各平台帳戶先建立 runner/VM；程式不會假裝已取得不存在的帳戶權限。
Cloud Run 需要啟用計費，因此不列入「絕不付費」自動鏈。

公開倉庫不保存任何 Token。未設定 Actions Secrets 時，排程只執行健康測試並安全閒置；
設定 `FLIGHT_MESH_JOB_URL` 與 `FLIGHT_MESH_TOKEN` 後才會取出私人搜尋工作並回傳結果。

## 介面

```json
{
  "searches": [{
    "origin": "HKG",
    "destination": "KUL",
    "outboundDate": "2026-12-16",
    "returnDate": "2026-12-27",
    "cabin": "economy",
    "adults": 2,
    "children": 1,
    "checkedBags": 1
  }]
}
```

每個來源獨立失敗，回應保留成功結果及 `failures`；整批不會因單一 OTA 改版而中止。
