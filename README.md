# AI Butler ERP

Inventory, purchasing, sales, payments, accounting and point of sale for a Malaysian
retailer, with an AI assistant that answers questions from the live data and drafts
documents for a person to confirm.

**Live demo:** https://ai-butler-erp.vercel.app — click *Enter as Owner* (or *Cashier*); no
sign-up. The data is a sample grocery shop and may be reset at any time.

Built step by step from a written design, as a portfolio project for AI engineering roles:
the interesting part is not the chat box, it is making an LLM useful inside a system where
numbers have to be right and permissions have to hold.

## What the AI part does, and how it is kept honest

- **Tool calling over the real API.** The butler (any OpenAI-compatible model; DeepSeek in
  the demo) gets tools that wrap the same route functions the screens use: stock, customers,
  open invoices, reports, the knowledge base. It never queries the database itself, so field
  redaction (cost prices, personal data) and store scope apply to it exactly as to the user
  who is asking. A cashier's butler has fewer tools than an owner's.
- **It proposes, people confirm.** A request like "make an invoice for Ah Seng, 2 cartons
  of Milo" becomes a pending action with system-computed checks (price, stock, credit
  limit). Nothing is written until the user presses Confirm; the tool run itself happens in
  a savepoint that is always rolled back.
- **The model does not do arithmetic.** An early run against the real model showed wrong
  sums, so totals on draft cards come from the server.
- **Retrieval that was measured, not assumed.** The knowledge base (pgvector) was evaluated
  on 21 questions in English, Chinese and Malay: vector search 95% hit@6, hybrid 90%,
  keyword 71%. The default follows the measurement. `python -m app.kb_eval` reruns it in CI.
- **MCP server.** The read tools are also exposed over the Model Context Protocol with
  purpose-limited tokens, so an external agent gets the same permission-checked view.
- **Scan intake.** Supplier papers (invoices, CNs, payment advices, bank slips, return notes)
  are uploaded or photographed into a queue, read by a vision model and sorted into their
  document type. The person checks the reading against the original side by side, then
  corrects it in a spreadsheet pop-up (edit, undo, cancel, confirm) where every cell is
  marked trusted / check / missing by server-side checks; confirming makes a draft document
  and opens the next paper. Recognition runs on either a vision model (any OpenAI-compatible
  endpoint) or, with no vision key, local OCR (RapidOCR, the engine of the companion
  Doc2Excel app) whose text the butler's text model shapes into the same JSON; with neither,
  the same screen works by hand.

## What the system does

| Area | What is there |
|---|---|
| Items | Multi-unit items with prices 1–5 (customer price levels), 11/12/13-digit barcodes, brand / group / colour / size, minimum selling price, reorder levels per store, stock per store, who supplied it and the purchase-price history |
| Sales | Quotation → sales order → delivery order → invoice → credit note; orders and delivery orders are fulfilled line by line, in parts; customer debit notes; credit-limit and below-cost warnings |
| Purchasing | Purchase orders received into goods-received notes (also the supplier invoice) in parts; a reorder list that suggests quantities net of what is on order and turns into a PO; document discount spread into line cost, tax-inclusive prices, cent adjustment, terms and due date; goods returns, supplier debit notes, credit notes without goods, refunds from suppliers |
| Stock | Moving-average cost ledger, adjustments, write-offs, transfers, stocktakes |
| Money | Receipts, supplier payments keyed in from the payment advice (matched to invoices, debit notes and credit notes by the supplier's own numbers), automatic and manual knock-off, statements of account, problem documents for what does not reconcile |
| Accounting | Chart of accounts; every posted document is booked to the general ledger automatically and reversed when voided; journal vouchers; trial balance, profit and loss, balance sheet, account ledger |
| Insights (BI) | Sales, gross profit and margin against the previous period, the trend by day / week / month, top items, customers, categories, brands and stores, invoices vs till and till takings by payment method, stock value and what is owed both ways |
| POS | Shifts, 5-sen cash rounding, supervisor override, returns; works offline as a PWA and uploads queued sales with idempotency keys |
| E-invoice | LHDN MyInvois UBL 1.0 documents, pre-submission checks, submit / validate / cancel, monthly consolidated e-invoice for walk-in sales, QR on the printout |
| Access | Tags (permission bundles), field-level redaction, only owner-approved computers can log in |
| Paper | Every document prints or saves as PDF with letterhead, tax identity and signature lines |

The general ledger is built from the documents rather than by each module: a sync step
books any posted document that has no journal yet, before every accounting read. The demo
script ends by checking that the trial balance and the balance sheet balance after the whole
story, and CI fails if they do not.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector
- **Frontend:** React 18, TypeScript, Vite; hand-written CSS (dark theme); installable
  offline PWA
- **AI:** OpenAI-compatible chat with tool calling; fastembed (MiniLM) or any embeddings
  endpoint; MCP
- **Delivery:** GitHub Actions (ruff, migrations, 288 tests, an end-to-end demo script, the
  retrieval evaluation; TypeScript build), Vercel (static app + the API as one Python
  function), Neon Postgres

## Run it locally

```bash
docker compose up -d                      # PostgreSQL + pgvector on 127.0.0.1:5433
cd backend
python -m venv .venv && .venv/Scripts/activate   # Windows; use bin/activate elsewhere
pip install -e ".[dev]"                   # add ",local-ocr" for scan recognition without a vision key
python -m app.demo                        # builds the sample shop through the real API
POSTGRES_DB=erp_demo uvicorn app.main:app --port 8000
```

```bash
cd frontend
npm ci
npm run dev                               # http://localhost:5173
```

Log in as `owner` / `demo-password-2026`. A new browser is shown a computer id the first
time; approve it with `python -m app.cli authorize-device <id>` (set `DEMO_MODE=1` to skip
approval, as the public site does). Copy `.env.example` to `.env` and fill `LLM_*` to switch
the butler on.

```bash
cd backend && pytest -q                   # needs the database container
```

## Windows desktop app

One exe runs the whole ERP on one computer. It starts Docker Desktop if needed, runs its own
PostgreSQL + pgvector container (`aibutler-erp-db` on 127.0.0.1:5434, data in the Docker
volume `aibutler-erp-data`), applies the migrations, and shows the ERP in a desktop window
served from 127.0.0.1 only, so this computer logs in without the approval step. On the first
run it asks to start with the sample shop or an empty company. Closing the window stops the
server and the container, and Docker Desktop too if the app started it.

```powershell
backend\.venv\Scripts\pip install -e "backend[local-ocr,desktop]"
.\desktopuild.ps1                        # -> dist\AI Butler ERP\AI Butler ERP.exe
```

Needs Docker Desktop installed. Settings and data live in `%LOCALAPPDATA%\AIButlerERP`
(`config.env` holds the generated secrets; put `LLM_*` / `VISION_*` keys there or in a `.env`
next to the exe). Scanning apps such as Doc2Excel can push papers into the scan queue with a
token made on the Scan screen (*Connect Doc2Excel*).

## Honest limits

- **LHDN:** the e-invoice flow is tested against a stand-in for MyInvois, and the public demo
  uses one too (its documents are marked *simulated*). It has not been run against the real
  LHDN sandbox; v1.1 digital signatures and debit-note e-invoices are not implemented.
- **Accounting:** no year-end close, SST return, bank reconciliation or export to an
  external accounting package yet.
- **Public demo:** retrieval there is keyword-only (the local embedding model does not fit a
  serverless function); administration of users, computers and company details is read-only;
  the butler answers a limited number of questions a day.
- **Not built:** batch / expiry and serial tracking, bills of material, promotions and
  loyalty, receipt-printer and cash-drawer drivers, a Singapore tax pack, an online store.
- Single-developer project: no load testing, no security audit.

## Layout

```
backend/app/api/v1      HTTP routes (thin: validation, permissions, shaping)
backend/app/services    business rules (sales, purchasing, stock, knock-off, ledger, POS, e-invoice, butler, kb)
backend/app/localization  country pack (Malaysia)
backend/alembic         migrations
backend/tests           pytest suite against a real PostgreSQL
frontend/src/pages      screens
frontend/src/docs       shared pieces of the document screens (picker, print sheet, e-invoice panel)
frontend/src/pos        till: offline catalog, outbox, receipt
api/index.py            Vercel entry point
desktop                 Windows desktop app (launcher + build script)
```
