# Portfolio Tracker

A personal investment portfolio tracker for Colombian investors. Self-hosted, operated via Claude Code.

Pass a PDF or screenshot from your broker to Claude — it extracts, ingests, and maintains your portfolio automatically, with FIFO cost basis, specific-lot assignment, and a Colombia tax report.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) with an Anthropic API key

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/your-username/portfolio.git
cd portfolio

# 2. Install dependencies
pip install -r requirements.txt

# 3. Initialize the database
# Open Claude Code in this directory, then run:
/setup
```

## Commands

| Command | What it does |
|---------|-------------|
| `/setup` | Create or migrate the database |
| `/ingest` | Ingest transactions from a PDF or screenshot |
| `/snapshot` | Show current positions with live prices and P&L |
| `/snapshot ibkr` | Snapshot filtered by broker |
| `/tax 2024` | Colombia tax report for fiscal year 2024 |

## Tax Report

The tax report is **hardcoded for Colombia**:
- **Ganancia Ocasional** (occasional gain): assets held >730 days → flat 15%
- **Renta Ordinaria** (ordinary income): assets held ≤730 days → progressive rate
- TRM (exchange rate) from Banco de la República
- UVT from DIAN (updated annually in `tools/tax_report.py`)

## Ticker Resolution

When a new security is ingested, the system tries to resolve its Yahoo Finance ticker automatically via ISIN. If it can't (ambiguous or Yahoo search fails), Claude will ask you:

1. Which exchange the instrument trades on (e.g., LSE, NASDAQ, XETRA)
2. Or the Yahoo Finance ticker directly (search at finance.yahoo.com)

Resolved tickers are saved in the local database — you won't be asked again.

## TRM Manual Fallback

If the Banco de la República API is unavailable, Claude will ask you to download TRM rates manually:

1. Go to: https://suameca.banrep.gov.co/estadisticas-economicas/informacionSerie/1/tasa_cambio_peso_colombiano_trm_dolar_usd
2. Switch to "Tabla" view
3. Select the dates of interest and download
4. Run: `python3 tools/load_trm.py <downloaded_file.txt>`

## Data Privacy

Your portfolio data stays local. `portfolio.db` is in `.gitignore` and never committed. Only generic code and SQL schemas are tracked in git.
