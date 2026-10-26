<div align="center">

#  Market Data Pipeline

### Automated Cannabis Market Data Pipeline
<br/>

![Python](https://img.shields.io/badge/Python-3.10+-1a1a2e?style=for-the-badge&logo=python&logoColor=4ade80&labelColor=0d1117)
![Playwright](https://img.shields.io/badge/Playwright-Automation-1a1a2e?style=for-the-badge&logo=playwright&logoColor=4ade80&labelColor=0d1117)
![Azure](https://img.shields.io/badge/Azure-Cloud-1a1a2e?style=for-the-badge&logo=microsoftazure&logoColor=38bdf8&labelColor=0d1117)
![SQL Server](https://img.shields.io/badge/SQL%20Server-Database-1a1a2e?style=for-the-badge&logo=microsoftsqlserver&logoColor=f87171&labelColor=0d1117)
![Docker](https://img.shields.io/badge/Docker-Containerized-1a1a2e?style=for-the-badge&logo=docker&logoColor=38bdf8&labelColor=0d1117)

<br/>

> ### ⚡ Enterprise-grade scraping, validation, storage, reporting & automation

<img src="https://img.shields.io/badge/STATUS-PRODUCTION_READY-0d1117?style=flat-square&labelColor=0d1117&color=4ade80" />
&nbsp;
<img src="https://img.shields.io/badge/UPTIME-AUTOMATED-0d1117?style=flat-square&labelColor=0d1117&color=38bdf8" />
&nbsp;
<img src="https://img.shields.io/badge/PIPELINE-END_TO_END-0d1117?style=flat-square&labelColor=0d1117&color=f472b6" />

</div>

<br/>

```
╔══════════════════════════════════════════════════════════════╗
║   SCRAPE  →  VALIDATE  →  STORE  →  REPORT  →  AUTOMATE        ║
╚══════════════════════════════════════════════════════════════╝
```

<br/>

## ◆ Overview

A **production-ready cannabis market data automation pipeline** built in Python. The system continuously collects dispensary product data from multiple cannabis retailer websites using:

`🌐 Playwright browser automation`&nbsp;&nbsp;`⚡ Direct API scraping`&nbsp;&nbsp;`🔄 Hybrid techniques`

After scraping, the pipeline automatically handles the full lifecycle:

<table>
<tr>
<td width="50%" valign="top">

**📥 Ingestion**
- Stores raw JSON responses
- Converts data into structured CSV
- Uploads raw data to Azure Blob

</td>
<td width="50%" valign="top">

**📤 Delivery**
- Loads processed data into SQL Server
- Generates multi-sheet Excel reports
- Sends automated email reports

</td>
</tr>
</table>

<br/>

## ◆ System Architecture

```text
                    ┌──────────────────────┐
                    │   DISPENSARY SITES   │
                    └──────────┬───────────┘
                               │
               ┌───────────────┴────────────────┐
               │                                 │
        ┌──────▼──────┐                 ┌────────▼────────┐
        │ API SCRAPER │                 │ PLAYWRIGHT BOT  │
        └──────┬──────┘                 └────────┬────────┘
               │                                 │
               └───────────────┬─────────────────┘
                               ▼
                    ┌──────────────────┐
                    │ RAW JSON STORAGE │
                    └────────┬─────────┘
                             ▼
                   ┌────────────────────┐
                   │ CSV TRANSFORMATION │
                   └────────┬───────────┘
                            ▼
                ┌─────────────────────────┐
                │ SQL + BLOB UPLOAD LAYER │
                └──────────┬──────────────┘
                           ▼
                ┌─────────────────────────┐
                │ EXCEL REPORT GENERATION │
                └──────────┬──────────────┘
                           ▼
                ┌─────────────────────────┐
                │ AUTOMATED EMAIL REPORTS │
                └─────────────────────────┘
```

<br/>

## ◆ Core Technologies

| | Technology | Purpose |
|:--:|:--|:--|
| 🐍 | **Python** | Main programming language |
| 🎭 | **Playwright** | Browser automation |
| ⚡ | **Requests** | API scraping |
| ☁️ | **Azure Blob Storage** | Raw JSON storage |
| 🗄️ | **SQL Server** | Structured data storage |
| 📊 | **Pandas** | Data transformation |
| 📧 | **SMTP** | Email automation |
| 🐳 | **Docker** | Containerization |
| 🔐 | **Azure Key Vault** | Secret management |

<br/>

## ◆ Project Structure

```text
market_data/
│
├── .gitignore
├── README.md
├── requirements.txt
├── Dockerfile
├── run.py                          # ▶ entrypoint
│
└── app/
    ├── main.py                     # 🧠 orchestration engine
    │
    ├── config/
    │   └── config.py               # 🔐 Key Vault + secrets
    │
    ├── output/
    │   ├── pending/
    │   │   ├── csv/
    │   │   └── json/
    │   ├── processed/              # 🟢 success
    │   ├── failed/                 # 🔴 errors
    │   └── mail/                   # 📧 reports
    │
    ├── scripts/
    │   ├── tuscon/    └── *.py
    │   └── conneticut/└── *.py
    │
    └── services/
        ├── upload_to_blob_and_sql.py
        └── mail.py
```

<br/>

## ◆ Execution Flow

<table>
<tr>
<td width="50%" valign="top">

### ▶️ `run.py`
**Main entrypoint** for the pipeline.

- Initialize pipeline execution
- Trigger `app/main.py`
- Support scheduler / cron execution

```bash
python run.py
```

</td>
<td width="50%" valign="top">

### 🧠 `main.py`
**Core orchestration engine.**

- Run all scrapers
- Generate JSON & CSV outputs
- Validate generated files
- Upload to Blob + SQL Server
- Generate Excel reports
- Send automated emails

</td>
</tr>
</table>

<br/>

## ◆ Scraper Types

<table>
<tr>
<td width="50%" valign="top">

### 1️⃣ API-Based Scrapers
Directly consume internal APIs exposed by dispensary sites.

**Advantages**
- ⚡ Extremely fast
- 🪶 Lower resource usage
- 📄 Easier pagination
- 🛡️ Stable extraction
- 📦 Structured JSON

```text
Request API → Receive JSON → Validate
→ Extract Products → Save JSON → CSV
```

**Supported:** `REST` · `GraphQL` · `Internal JSON`

</td>
<td width="50%" valign="top">

### 2️⃣ Playwright-Based Scrapers
For highly dynamic, JS-heavy, anti-bot sites.

**Capabilities**
- 🖥️ JavaScript rendering
- 📜 Scroll / lazy loading
- 🔑 Session generation
- 🎚️ Dynamic filtering

```text
Launch Browser → Handle Age Gate
→ Apply Filters → Dynamic Scroll
→ Extract Cards → Save JSON + CSV
```

**Features:** locators · infinite scroll · resource blocking

</td>
</tr>
</table>

<br/>

## ◆ Secure Configuration · Azure Key Vault

All sensitive credentials are managed centrally through **Azure Key Vault**, handled in `app/config/config.py`.

```text
🔑 SQL credentials      🔑 Blob storage credentials
🔑 Email credentials    🔑 API keys
🔑 Database names       🔑 Container names
```

> **Handles:** secret loading · credential management · centralized configuration · environment setup

<br/>

## ◆ Data Validation

<table>
<tr>
<td width="50%" valign="top">

### ✅ JSON Validation
- File exists
- Non-empty content
- Valid JSON structure
- Usable records

</td>
<td width="50%" valign="top">

### ✅ CSV Validation
- Readable encoding
- Valid dataframe
- Required columns
- Duplicate handling

</td>
</tr>
</table>

<br/>

## ◆ SQL Pipeline

| Feature | Detail |
|:--|:--|
| 🏗️ **Automatic Table Creation** | Tables created automatically if missing |
| 🔁 **Idempotent Uploads** | Existing records deleted before insert — no duplicates |
| 🚀 **High-Speed Inserts** | `fast_executemany = True` for bulk loading |
| 🧹 **Data Cleaning** | NULL normalization · safe float conversion · invalid value handling |

<br/>

## ◆ Azure Blob Storage

Raw JSON responses are uploaded for **debugging · auditing · reprocessing · recovery · schema analysis**.

```text
Structure:   /location/company/file.json
Example:     /connecticut/curaleaf/2026-05-22_curaleaf.json
```

<br/>

## ◆ File Lifecycle

<table align="center">
<tr>
<td align="center">🟡<br/><b>PENDING</b><br/><sub><code>output/pending/</code></sub><br/>Newly generated</td>
<td align="center">→</td>
<td align="center">🟢<br/><b>PROCESSED</b><br/><sub><code>output/processed/</code></sub><br/>Successfully handled</td>
<td align="center">→</td>
<td align="center">🔴<br/><b>FAILED</b><br/><sub><code>output/failed/</code></sub><br/>Validation errors</td>
</tr>
</table>

<br/>

## ◆ Automated Reporting

The pipeline fetches SQL data, generates a multi-sheet Excel report, and emails it automatically.

| Sheet Name | Purpose |
|:--|:--|
| `market_data` | Complete scraped dataset |
| `only_deals_data` | Products with active promotions |

<br/>

## ◆ Logging & Error Handling

<table>
<tr>
<td width="50%" valign="top">

### 📜 Logging
Centralized across scrapers, uploads, SQL, validation, and email.

- ⏱️ Timestamps
- 🏷️ INFO / WARNING / ERROR
- 🧵 Traceback logging
- ⚡ Fail-fast architecture

</td>
<td width="50%" valign="top">

### 🚨 Failure Modes
Strict **fail-fast** design.

- **Scraper:** blocked APIs, invalid JSON
- **Upload:** SQL / blob / malformed files
- **Email:** SMTP, missing attachments, auth

</td>
</tr>
</table>

<br/>

## ◆ Performance Optimizations

<table>
<tr>
<td width="33%" valign="top" align="center">

**🎭 Playwright**
<br/><sub>image blocking · font blocking · optimized selectors · reduced waits</sub>

</td>
<td width="33%" valign="top" align="center">

**⚡ API**
<br/><sub>pagination batching · selective requests · fallback calls</sub>

</td>
<td width="33%" valign="top" align="center">

**🗄️ SQL**
<br/><sub>batch inserts · fast executemany · reduced commits</sub>

</td>
</tr>
</table>

<br/>

## ◆ Installation

```bash
# 1️⃣  Clone repository
git clone <repo-url>
cd market_data

# 2️⃣  Create virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3️⃣  Install requirements
pip install -r requirements.txt

# 4️⃣  Install Playwright browsers
playwright install
```

<br/>

## ◆ Running The Pipeline

```bash
python run.py
```

This automatically performs the full chain:

```text
✅ Run all scrapers          ✅ Upload JSON to Azure Blob
✅ Generate JSON files       ✅ Upload CSV data into SQL Server
✅ Generate CSV files        ✅ Generate Excel reports
                             ✅ Send email reports
```

<br/>

## ◆ Docker Support

```bash
# Build image
docker build -t market-data .

# Run container
docker run market-data
```

<br/>

## ◆ Output Examples

```text
📄 CSV    →  output/pending/csv/2026-05-22_curaleaf.csv
🧾 JSON   →  output/pending/json/2026-05-22_curaleaf.json
📊 Excel  →  output/mail/market_data_2026-05-22.xlsx
```

<br/>

## ◆ Important Notes

> - 🛡️ Some websites require browser automation due to anti-bot protections.
> - 🔑 Some APIs require session generation before access.
> - 🗃️ JSON storage exists intentionally for auditability and debugging.
> - ⚙️ Playwright scrapers are optimized for performance and minimal rendering overhead.
> - 🔐 Azure Key Vault is used for secure credential handling.

<br/>

<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:2c5364,50:203a43,100:0f2027&height=120&section=footer" width="100%" />


</div>
