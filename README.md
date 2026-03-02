# TranslateDoc

A document translation web app powered by **Azure AI Translator**. Upload a document (Word, PDF, TXT, Markdown), pick your languages, and download the translated version — all in a single click.

![Architecture](https://img.shields.io/badge/Azure-Container%20App-blue) ![Python](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python-green) ![React](https://img.shields.io/badge/Frontend-React%20%7C%20Vite-purple)

---

## Features

| Feature | Details |
|---|---|
| **Supported formats** | `.pdf`, `.docx`, `.txt`, `.md` |
| **Languages** | English, German, French, Spanish, Italian, Chinese (Simplified), Japanese + auto-detect |
| **Enhanced accuracy** | Optional two-pass translation with AI-generated glossary (Premium) |
| **Auth** | Entra ID (Azure AD) — every user in your tenant |
| **Identity** | Managed Identity in Azure; API key fallback for local dev |
| **Infra-as-Code** | Bicep template for all Azure resources |
| **One-command deploy** | Single script creates everything |

---

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   Browser    │───▶│  Container App    │───▶│  Azure AI Translator│
│  (React SPA) │◀───│  (FastAPI/Python) │◀───│  (Cognitive Svc)    │
└──────────────┘     └──────────────────┘     └─────────────────────┘
                            │                          │
                     ┌──────┴──────┐            ┌──────┴───────┐
                     │  Entra ID   │            │ Azure OpenAI │
                     │  (EasyAuth) │            │ (glossary AI)│
                     └─────────────┘            └──────────────┘
```

**Resources created by the Bicep template:**

| Resource | Purpose |
|---|---|
| Azure AI Translator | Text translation API |
| Azure OpenAI | LLM for glossary generation (enhanced accuracy) |
| Azure Container Registry | Hosts the Docker image |
| Container App Environment | Serverless compute |
| Container App | Runs the application |
| User-Assigned Managed Identity | Passwordless access to Translator, OpenAI & ACR |
| Storage Account | Blob storage for batch document translation |
| Log Analytics + App Insights | Monitoring |

---

## Prerequisites

- An Azure subscription with **Contributor** + **User Access Administrator** role
- [Docker](https://docs.docker.com/get-docker/) (only for local testing with docker-compose)
- [Node.js](https://nodejs.org/) ≥ 18 (only for local frontend development)
- [Python](https://www.python.org/) ≥ 3.11 (only for local backend development)

---

## Quick Deploy to Azure (One Command)


```powershell
.\deploy.ps1 -ResourceGroup rg-translatedoc -Location eastus -AppName translatedoc
```

The script will:

1. Create the resource group
2. Register an Entra ID application (for SSO)
3. Deploy all Azure resources via Bicep
4. Build and push the Docker image to ACR
5. Update the Container App with the new image
6. Print the public URL

> **Tip:** All users in your Azure AD tenant will be able to sign in automatically.

---

## Local Development

### 1. Create Azure resources

You need an Azure AI Translator resource (required) and optionally Azure OpenAI + a Storage Account for enhanced accuracy mode.

```bash
# Create a resource group (or use an existing one)
az group create --name rg-translatedoc-dev --location eastus

# ── Azure AI Translator (required) ──────────────────────────────
az cognitiveservices account create \
    --name translatedoc-dev \
    --resource-group rg-translatedoc-dev \
    --kind TextTranslation \
    --sku S1 \
    --location eastus \
    --custom-domain translatedoc-dev \
    --yes

# Get the Translator key
az cognitiveservices account keys list \
    --name translatedoc-dev \
    --resource-group rg-translatedoc-dev \
    --query key1 -o tsv

# ── Azure OpenAI (optional – needed for Enhanced Accuracy) ─────
az cognitiveservices account create \
    --name translatedoc-dev-openai \
    --resource-group rg-translatedoc-dev \
    --kind OpenAI \
    --sku S0 \
    --location eastus \
    --custom-domain translatedoc-dev-openai \
    --yes

# Deploy the gpt-5.2 model
az cognitiveservices account deployment create \
    --name translatedoc-dev-openai \
    --resource-group rg-translatedoc-dev \
    --deployment-name gpt-5.2-chat \
    --model-name gpt-5.2-chat \
    --model-version 2026-02-10 \
    --model-format OpenAI \
    --sku-name GlobalStandard \
    --sku-capacity 30

# Get the OpenAI key
az cognitiveservices account keys list \
    --name translatedoc-dev-openai \
    --resource-group rg-translatedoc-dev \
    --query key1 -o tsv

# ── Storage Account (optional – needed for binary file translation e.g. PDF/DOCX) ──
az storage account create \
    --name sttranslatedev \
    --resource-group rg-translatedoc-dev \
    --location eastus \
    --sku Standard_LRS \
    --kind StorageV2 \
    --allow-blob-public-access false

# Get the storage connection string
az storage account show-connection-string \
    --name sttranslatedev \
    --resource-group rg-translatedoc-dev \
    --query connectionString -o tsv
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# ── Translator (required) ──────────────────────
AZURE_TRANSLATOR_ENDPOINT=https://translatedoc-dev.cognitiveservices.azure.com
AZURE_TRANSLATOR_KEY=<paste-translator-key>
AZURE_TRANSLATOR_REGION=eastus
USE_MANAGED_IDENTITY=false

# ── Storage (optional – for PDF/DOCX batch translation) ──
AZURE_STORAGE_CONNECTION_STRING=<paste-connection-string>

# ── Azure OpenAI (optional – for Enhanced Accuracy) ──
AZURE_OPENAI_ENDPOINT=https://translatedoc-dev-openai.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-5.2-chat
# Or use an OpenAI API key instead:
# OPENAI_API_KEY=sk-...
```

### 3a. Run with Docker Compose (easiest)

```bash
docker-compose up --build
```

Open http://localhost:8000

### 3b. Run frontend + backend separately (for hot-reload)

**Terminal 1 — Backend:**

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

# Load env vars (or set them in your shell)
# Windows PowerShell:
Get-Content ..\.env | ForEach-Object { if ($_ -match '^\s*([^#][^=]+)=(.*)') { [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process') } }
# Linux/macOS:
export $(grep -v '^#' ../.env | xargs)

uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — Frontend:**

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server starts at http://localhost:5173 and proxies `/api` requests to the backend at `:8000`.

---

## Running Tests

```bash
cd backend
pip install -r requirements.txt   # if not already done

pytest -v
```

All Azure Translator API calls are mocked in tests — no Azure credentials needed.

### Test structure

| File | Covers |
|---|---|
| `tests/test_translator.py` | Translation batching, URL construction, mocked API |
| `tests/test_api.py` | FastAPI endpoints, validation, error handling |

---

## Project Structure

```
translatedoc/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI app, static file serving
│   │   ├── config.py               # Pydantic settings
│   │   ├── routers/
│   │   │   └── translate.py        # /api/translate, /api/languages, /api/health
│   │   └── services/
│   │       ├── translator.py       # Azure Translator client + batching
│   │       ├── job_manager.py      # Job orchestration + enhanced pipeline
│   │       ├── glossary_generator.py  # LLM-powered glossary generation
│   │       └── text_extractor.py   # Text extraction from PDF/DOCX/XLSX/PPTX
│   ├── tests/                      # Unit & integration tests
│   ├── requirements.txt
│   └── pytest.ini
├── frontend/
│   ├── src/
│   │   ├── App.jsx                 # Main React component
│   │   ├── App.css                 # Styles & animations
│   │   ├── main.jsx                # Entry point
│   │   └── index.css               # Base styles
│   ├── index.html
│   ├── package.json
│   └── vite.config.js              # Proxy config for dev
├── infra/
│   └── main.bicep                  # All Azure resources (incl. OpenAI)
├── Dockerfile                      # Multi-stage (Node build + Python runtime)
├── docker-compose.yml              # Local testing
├── deploy.ps1                      # Windows deployment script
├── deploy.sh                       # Linux/macOS deployment script
├── .env.example                    # Environment variable template
└── README.md
```

---

## API Reference

### `GET /api/health`

Returns `{"status": "healthy"}`.

### `GET /api/languages`

Returns the list of supported languages:

```json
{
  "languages": [
    {"code": "auto", "name": "Auto-detect"},
    {"code": "en",   "name": "English"},
    {"code": "de",   "name": "German"},
    ...
  ]
}
```

### `POST /api/translate`

Translates a document.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `files` | File(s) (multipart) | Yes | — | One or more documents to translate |
| `source_language` | string | No | `auto` | Source language code |
| `target_language` | string | No | `en` | Target language code |
| `enhance_accuracy` | boolean | No | `false` | Enable two-pass translation with AI glossary (Premium — slower) |

**Response:** The translated document as a binary download with the original filename.

**Error responses:**

| Status | Meaning |
|---|---|
| 400 | Unsupported file type or invalid language |
| 413 | File exceeds size limit |
| 500 | Translation service error |

---

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `AZURE_TRANSLATOR_ENDPOINT` | `https://api.cognitive.microsofttranslator.com` | Translator API endpoint |
| `AZURE_TRANSLATOR_KEY` | *(empty)* | Subscription key (local dev only) |
| `AZURE_TRANSLATOR_REGION` | `eastus` | Azure region of the Translator resource |
| `USE_MANAGED_IDENTITY` | `false` | Set `true` in Azure to use Managed Identity |
| `AZURE_CLIENT_ID` | *(empty)* | User-assigned Managed Identity client ID (set automatically by Bicep) |
| `MAX_FILE_SIZE_MB` | `10` | Maximum upload size in MB |
| `AZURE_STORAGE_ACCOUNT_NAME` | *(empty)* | Storage account for batch translation |
| `AZURE_STORAGE_CONNECTION_STRING` | *(empty)* | Storage connection string (local dev with key auth) |
| `AZURE_OPENAI_ENDPOINT` | *(empty)* | Azure OpenAI endpoint for glossary generation |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.2-chat` | Azure OpenAI model deployment name |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | Azure OpenAI API version |
| `OPENAI_API_KEY` | *(empty)* | OpenAI API key (local dev fallback for glossary generation) |
| `OPENAI_MODEL` | `gpt-5.2-chat` | OpenAI model name (when using API key) |

---

## Enhanced Accuracy Mode (Premium)

When the **"Enhance accuracy"** checkbox is enabled, each file goes through a three-stage pipeline:

1. **Pass 1** — Standard translation via Azure AI Translator
2. **Glossary generation** — An LLM (Azure OpenAI or OpenAI) compares the source document with the Pass 1 result, identifies domain-specific terms, proper nouns, and inconsistencies, and generates a TSV glossary
3. **Pass 2** — Re-translation with the glossary attached, enforcing consistent terminology

### How it works

- Text is extracted from both the original and translated documents (supports PDF, DOCX, XLSX, PPTX, TXT, Markdown)
- The LLM receives both texts and produces a tab-separated glossary of up to 200 terms
- Azure Translator's built-in glossary support is used for the second pass (exact-match enforcement)
- If any enhancement step fails, the Pass 1 result is returned as a fallback

### When to use it

- Documents with domain-specific terminology (legal, medical, technical)
- Documents with proper nouns, brand names, or abbreviations
- When consistency across repeated terms is important

### Cost & speed implications

- **Slower**: Each file requires two translation passes + one LLM call
- **Additional cost**: Azure OpenAI tokens are consumed for glossary generation
- The UI clearly labels this as "Premium" with a description of the trade-off

### Authentication

| Environment | Authentication |
|---|---|
| **Azure (production)** | Managed Identity → Cognitive Services OpenAI User role (configured by Bicep) |
| **Local (runlocal.ps1)** | Azure OpenAI key auto-discovered from Azure, OR set `OPENAI_API_KEY` in `.env` |
| **Local (manual)** | Set `AZURE_OPENAI_ENDPOINT` + key, or `OPENAI_API_KEY` in `.env` |

---

## Authentication

### In Azure (production)

The Container App is configured with **built-in authentication** (EasyAuth) using Entra ID. Every user in your Azure AD tenant is automatically allowed. No app-level code is needed — the platform handles sign-in/sign-out.

### Locally

Authentication is **not enforced** when running locally. The backend serves requests without checking identity. You only need valid Azure Translator credentials in your `.env` file.

---

## Known Limitations

- **Images & charts:** Text embedded inside images, charts, or diagrams within documents is not translated — only the document's native text content is processed.
- **Markdown code blocks:** The translation engine may occasionally alter content inside fenced code blocks or inline code spans.
- **In-memory jobs:** Translation jobs are stored in memory and expire after 1 hour. An app restart loses all pending and completed jobs.
- **Language list:** The UI exposes 7 languages + auto-detect. Azure Translator supports many more, but they are not currently surfaced.
- **File size:** Default limit is 10 MB per document (configurable via `MAX_FILE_SIZE_MB`).

---

## Cleanup

To remove all Azure resources:

```bash
az group delete --name rg-translatedoc --yes --no-wait
```

To also remove the Entra ID app registration:

```bash
az ad app delete --id <client-id>
```

---

## License

MIT
