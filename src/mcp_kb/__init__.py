"""MCP Microservices Knowledge Base.

A centralized MCP knowledge engine for a Spring Boot microservice ecosystem:
code understanding, service relationships, API/event discovery, schema
understanding, defect and impact analysis, and developer onboarding.
"""

from ._ssl_bootstrap import inject as _inject_os_truststore

# Trust the OS certificate store before any TLS client is created so that
# corporate TLS-inspection proxies don't break outbound HTTPS (HuggingFace,
# OpenAI, Qdrant, ...). No-op when disabled or unsupported.
_inject_os_truststore()

__version__ = "1.0.0"

