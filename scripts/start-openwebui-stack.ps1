[CmdletBinding()]
param(
    [string]$Neo4jPassword,
    [int]$OpenWebUiPort = 3001,
    [switch]$BuildKnowledge,
    [switch]$SkipOllamaEnrichment
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (-not $Neo4jPassword) {
    $secret = Read-Host "Neo4j password" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $Neo4jPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if ([string]::IsNullOrWhiteSpace($Neo4jPassword)) {
    throw "A Neo4j password is required."
}
if ($OpenWebUiPort -lt 1 -or $OpenWebUiPort -gt 65535) {
    throw "OpenWebUiPort must be between 1 and 65535."
}

$env:NEO4J_PASSWORD = $Neo4jPassword
$env:LLM_PROVIDER = if ($env:LLM_PROVIDER) { $env:LLM_PROVIDER } else { "ollama" }
$env:LLM_MODEL = if ($env:LLM_MODEL) { $env:LLM_MODEL } else { "qwen2.5-coder:14b" }
$env:OLLAMA_BASE_URL = if ($env:OLLAMA_BASE_URL) { $env:OLLAMA_BASE_URL } else { "http://host.docker.internal:11434/v1" }
$env:OLLAMA_GENERATE_URL = if ($env:OLLAMA_GENERATE_URL) { $env:OLLAMA_GENERATE_URL } else { "http://host.docker.internal:11434/api/generate" }
$env:OPEN_WEBUI_PORT = $OpenWebUiPort

Write-Host "Starting Neo4j, Qdrant, PostgreSQL, MCP server, and Open WebUI..."
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose startup failed. Run 'docker compose logs' for details."
}

Write-Host "`nService status:"
docker compose ps

$webUiUrl = "http://localhost:$OpenWebUiPort"
$mcpUrl = "http://mcp-server:8000/mcp"

Write-Host "`nOpen WebUI: $webUiUrl"
Write-Host "Neo4j Browser: http://localhost:7474"
Write-Host "MCP endpoint from Windows: http://localhost:8000/mcp"
Write-Host "MCP endpoint inside Open WebUI Docker network: $mcpUrl"
Write-Host "`nOpen WebUI setup (first run):"
Write-Host "1. Open $webUiUrl and create the first admin account."
Write-Host "2. Admin Panel -> Settings / External Connections -> MCP Servers."
Write-Host "3. Add 'microservices-knowledge-base' as Streamable HTTP using $mcpUrl."
Write-Host "4. Enable the server for the desired model or workspace."
Write-Host "5. Use MCP prompt 'default_platform_assistant' or copy the default system prompt from docs/OPENWEBUI_INTEGRATION.md."

if ($BuildKnowledge) {
    $rewriteArgs = @("exec", "mcp-server", "python", "-m", "mcp_kb.ingestion.full_rewrite", "--skip-clone", "--ollama-url", "http://host.docker.internal:11434")
    if ($SkipOllamaEnrichment) {
        $rewriteArgs += "--skip-llm-enrichment"
    }
    Write-Host "`nBuilding knowledge in the MCP container..."
    & docker compose @rewriteArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Knowledge rewrite failed. Review 'docker compose logs mcp-server'."
    }
}

Start-Process $webUiUrl
