"""Follow-on gap 7: tests for deeper Spring Boot domain modeling (Beans,
@Configuration, @ExceptionHandler/@ControllerAdvice, @Scheduled/@Async)."""
from __future__ import annotations

from mcp_kb.ingestion.parsers.java_parser import JavaParser
from mcp_kb.models import EdgeType, NodeType, SourceFile, ContentType


CONFIG_JAVA = """
package com.example.order.config;

@Configuration
public class AppConfig {

    @Bean
    @Profile("prod")
    public PaymentGateway paymentGateway() {
        return new StripeGateway();
    }

    @Bean(name = "cacheManager")
    public CacheManager cacheManager() {
        return new ConcurrentMapCacheManager();
    }
}
"""

ADVICE_JAVA = """
package com.example.order.web;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(OrderNotFoundException.class)
    @ResponseStatus(HttpStatus.NOT_FOUND)
    public ErrorResponse handleNotFound(OrderNotFoundException ex) {
        return new ErrorResponse(ex.getMessage());
    }

    @ExceptionHandler({ValidationException.class, IllegalArgumentException.class})
    public ErrorResponse handleValidation(Exception ex) {
        return new ErrorResponse(ex.getMessage());
    }
}
"""

SCHEDULED_JAVA = """
package com.example.order.service;

@Service
public class ReconciliationService {

    @Scheduled(cron = "0 0 * * * *", fixedDelay = 60000)
    @Async
    @Retryable(maxAttempts = "3")
    public void reconcile() {
        doWork();
    }

    private void doWork() {}
}
"""


def _source(tmp_path, name: str, code: str) -> SourceFile:
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return SourceFile(repo="order-service", rel_path=name, abs_path=str(path),
                      content_type=ContentType.JAVA, sha256="x" * 64, size_bytes=len(code))


def test_configuration_bean_methods_produce_bean_definition_nodes(settings, tmp_path):
    parser = JavaParser(settings)
    result = parser.parse(_source(tmp_path, "AppConfig.java", CONFIG_JAVA))

    beans = [n for n in result.nodes if n.type == NodeType.BEAN_DEFINITION]
    names = {b.name for b in beans}
    assert names == {"paymentGateway", "cacheManager"}

    payment_bean = next(b for b in beans if b.name == "paymentGateway")
    assert payment_bean.attributes["returns"] == "PaymentGateway"
    assert payment_bean.attributes["profile"] == "prod"

    generates_edges = [e for e in result.edges if e.type == EdgeType.GENERATES]
    assert len(generates_edges) == 2


def test_exception_handler_advice_produces_catches_edges(settings, tmp_path):
    parser = JavaParser(settings)
    result = parser.parse(_source(tmp_path, "GlobalExceptionHandler.java", ADVICE_JAVA))

    catches = [e for e in result.edges if e.type == EdgeType.CATCHES]
    assert len(catches) == 3  # NotFound + Validation + IllegalArgument
    assert all(e.attributes["global_handler"] is True for e in catches)

    exc_names = {n.name for n in result.nodes if n.type == NodeType.EXCEPTION_TYPE}
    assert {"OrderNotFoundException", "ValidationException", "IllegalArgumentException"} <= exc_names


def test_scheduled_async_retryable_metadata_captured(settings, tmp_path):
    parser = JavaParser(settings)
    result = parser.parse(_source(tmp_path, "ReconciliationService.java", SCHEDULED_JAVA))

    method = next(n for n in result.nodes if n.type == NodeType.METHOD and n.name == "reconcile")
    scheduling = method.attributes["scheduling"]
    assert scheduling["scheduled"]["cron"] == "0 0 * * * *"
    assert scheduling["scheduled"]["fixed_delay"] == "60000"
    assert "async" in scheduling
    assert scheduling["retryable"]["max_attempts"] == "3"
