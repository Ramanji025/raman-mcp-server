"""Curated catalog: artifact coordinates → feature tags + API symbol names.

Covers Spring Boot starters, core Spring libraries, AWS SDK, .NET packages,
and common Python packages.  The catalog is intentionally data-only so it
can be extended without touching pipeline logic.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactEntry:
    """Known capability tags/concept mappings for one package artifact."""

    features: tuple[str, ...]       # capability tags, e.g. ("Web", "REST API")
    concept_names: tuple[str, ...]  # API symbols this artifact surfaces
    description: str = ""


# ---------------------------------------------------------------------------
# Key: lower-cased artifactId / package name  (groupId is ignored here;
#      the resolver matches on artifact name first, then refines by group).
# ---------------------------------------------------------------------------
_CATALOG: dict[str, ArtifactEntry] = {

    # ── Spring Boot starters ────────────────────────────────────────────────
    "spring-boot-starter-web": ArtifactEntry(
        features=("Web", "REST API", "Spring MVC", "Embedded Tomcat", "HTTP"),
        concept_names=("@RestController", "@RequestMapping", "DispatcherServlet",
                       "@GetMapping", "@PostMapping"),
        description="Spring MVC + embedded Tomcat for REST APIs",
    ),
    "spring-boot-starter-data-jpa": ArtifactEntry(
        features=("Data", "JPA", "ORM", "Hibernate", "Persistence"),
        concept_names=("@Entity", "@Repository", "JpaRepository", "@Transactional",
                       "@Column", "@Id"),
        description="Spring Data JPA with Hibernate ORM",
    ),
    "spring-boot-starter-security": ArtifactEntry(
        features=("Security", "Authentication", "Authorization", "CSRF Protection"),
        concept_names=("@PreAuthorize", "SecurityFilterChain", "UserDetailsService",
                       "PasswordEncoder", "@EnableWebSecurity"),
        description="Spring Security — authn/authz filter chain",
    ),
    "spring-boot-starter-actuator": ArtifactEntry(
        features=("Observability", "Health Checks", "Metrics", "Monitoring"),
        concept_names=("HealthIndicator", "ActuatorEndpoint", "MeterRegistry"),
        description="Production-ready operational endpoints",
    ),
    "spring-boot-starter-cache": ArtifactEntry(
        features=("Caching", "Cache Abstraction"),
        concept_names=("@Cacheable", "@CacheEvict", "@EnableCaching", "CacheManager"),
        description="Spring Cache abstraction",
    ),
    "spring-boot-starter-aop": ArtifactEntry(
        features=("AOP", "Aspect-Oriented Programming", "Cross-cutting Concerns"),
        concept_names=("@Aspect", "@Around", "@Before", "@After", "@Pointcut"),
        description="AspectJ-backed AOP support",
    ),
    "spring-boot-starter-validation": ArtifactEntry(
        features=("Validation", "Bean Validation", "Input Validation"),
        concept_names=("@Valid", "@NotNull", "@NotBlank", "@Size", "@Email"),
        description="Jakarta Bean Validation via Hibernate Validator",
    ),
    "spring-boot-starter-test": ArtifactEntry(
        features=("Testing", "Unit Testing", "JUnit", "Mockito"),
        concept_names=("@SpringBootTest", "@MockBean", "@WebMvcTest",
                       "@DataJpaTest", "MockMvc"),
        description="Test slice annotations + JUnit 5 + Mockito",
    ),
    "spring-boot-starter-data-redis": ArtifactEntry(
        features=("Caching", "Redis", "Data", "Key-Value Store"),
        concept_names=("RedisTemplate", "StringRedisTemplate", "@RedisHash"),
        description="Spring Data Redis — Lettuce or Jedis client",
    ),
    "spring-boot-starter-data-mongodb": ArtifactEntry(
        features=("Data", "MongoDB", "NoSQL", "Document Store"),
        concept_names=("@Document", "MongoTemplate", "MongoRepository"),
        description="Spring Data MongoDB",
    ),
    "spring-boot-starter-webflux": ArtifactEntry(
        features=("Reactive", "WebFlux", "Non-blocking I/O", "REST API"),
        concept_names=("@RestController", "RouterFunction", "WebClient",
                       "Mono", "Flux"),
        description="Spring WebFlux reactive web stack (Netty)",
    ),
    "spring-boot-starter-oauth2-resource-server": ArtifactEntry(
        features=("Security", "OAuth2", "JWT", "Resource Server"),
        concept_names=("@EnableResourceServer", "JwtDecoder",
                       "BearerTokenAuthenticationFilter"),
        description="OAuth2 resource server with JWT validation",
    ),
    "spring-boot-starter-oauth2-client": ArtifactEntry(
        features=("Security", "OAuth2", "OIDC", "SSO"),
        concept_names=("OAuth2LoginConfigurer", "OAuth2AuthorizedClientManager"),
        description="OAuth2 / OIDC client-side login support",
    ),
    "spring-boot-starter-mail": ArtifactEntry(
        features=("Messaging", "Email", "SMTP"),
        concept_names=("JavaMailSender", "MimeMessage"),
        description="JavaMail / Spring Mail abstraction",
    ),
    "spring-boot-starter-quartz": ArtifactEntry(
        features=("Scheduling", "Job Scheduling", "Quartz"),
        concept_names=("@QuartzJob", "QuartzScheduler", "JobDetail", "Trigger"),
        description="Quartz Scheduler integration",
    ),
    "spring-boot-starter-batch": ArtifactEntry(
        features=("Batch Processing", "ETL", "Job Execution"),
        concept_names=("@EnableBatchProcessing", "Job", "Step",
                       "ItemReader", "ItemWriter", "ItemProcessor"),
        description="Spring Batch — chunk-oriented processing framework",
    ),

    # ── Spring Cloud ────────────────────────────────────────────────────────
    "spring-cloud-starter-netflix-eureka-client": ArtifactEntry(
        features=("Service Discovery", "Eureka", "Cloud", "Microservices"),
        concept_names=("@EnableEurekaClient", "EurekaClient", "DiscoveryClient"),
        description="Eureka client for service registration/discovery",
    ),
    "spring-cloud-starter-openfeign": ArtifactEntry(
        features=("HTTP Client", "Feign", "Service-to-Service", "REST Client"),
        concept_names=("@FeignClient", "@EnableFeignClients", "FeignClient"),
        description="Declarative HTTP client via OpenFeign",
    ),
    "spring-cloud-starter-gateway": ArtifactEntry(
        features=("API Gateway", "Routing", "Proxy", "Cloud"),
        concept_names=("RouteLocator", "GatewayFilter", "PredicateFactory"),
        description="Spring Cloud Gateway — reactive API gateway",
    ),
    "spring-cloud-starter-config": ArtifactEntry(
        features=("Configuration", "Centralized Config", "Cloud"),
        concept_names=("@RefreshScope", "ConfigServer", "bootstrap.yml"),
        description="Spring Cloud Config client — centralized property source",
    ),
    "spring-cloud-starter-circuitbreaker-resilience4j": ArtifactEntry(
        features=("Resilience", "Circuit Breaker", "Retry", "Fault Tolerance"),
        concept_names=("@CircuitBreaker", "@Retry", "@TimeLimiter",
                       "CircuitBreakerFactory", "Resilience4j"),
        description="Resilience4j circuit breaker + retry integration",
    ),
    "spring-cloud-starter-sleuth": ArtifactEntry(
        features=("Distributed Tracing", "Observability", "Logging"),
        concept_names=("Span", "Trace", "TraceId", "SpanId"),
        description="Distributed tracing via Sleuth (Brave)",
    ),

    # ── Spring Kafka / Messaging ────────────────────────────────────────────
    "spring-kafka": ArtifactEntry(
        features=("Messaging", "Kafka", "Event Streaming", "Pub/Sub"),
        concept_names=("@KafkaListener", "KafkaTemplate", "ConsumerRecord",
                       "ProducerRecord", "@EnableKafka"),
        description="Spring for Apache Kafka",
    ),
    "spring-boot-starter-amqp": ArtifactEntry(
        features=("Messaging", "RabbitMQ", "AMQP", "Pub/Sub"),
        concept_names=("@RabbitListener", "RabbitTemplate", "AmqpAdmin"),
        description="Spring AMQP — RabbitMQ integration",
    ),

    # ── OpenAPI / Swagger ───────────────────────────────────────────────────
    "springdoc-openapi-starter-webmvc-ui": ArtifactEntry(
        features=("API Documentation", "OpenAPI", "Swagger UI"),
        concept_names=("@OpenAPIDefinition", "@Operation", "@ApiResponse",
                       "@Parameter", "OpenApiCustomiser"),
        description="SpringDoc — auto-generates OpenAPI 3 docs + Swagger UI",
    ),
    "springdoc-openapi-ui": ArtifactEntry(
        features=("API Documentation", "OpenAPI", "Swagger UI"),
        concept_names=("@OpenAPIDefinition", "@Operation"),
        description="SpringDoc OpenAPI UI (legacy artifact)",
    ),

    # ── Keycloak ────────────────────────────────────────────────────────────
    "keycloak-spring-boot-starter": ArtifactEntry(
        features=("Security", "SSO", "Keycloak", "OAuth2", "OIDC"),
        concept_names=("KeycloakSecurityContext", "KeycloakDeployment",
                       "KeycloakAuthenticationProvider"),
        description="Keycloak adapter for Spring Boot",
    ),

    # ── AWS SDK (Java) ──────────────────────────────────────────────────────
    "aws-java-sdk-s3": ArtifactEntry(
        features=("Cloud Storage", "AWS S3", "Object Storage"),
        concept_names=("AmazonS3", "S3Client", "PutObjectRequest"),
        description="AWS SDK v1 — Amazon S3 client",
    ),
    "aws-java-sdk-dynamodb": ArtifactEntry(
        features=("NoSQL", "DynamoDB", "AWS", "Key-Value Store"),
        concept_names=("DynamoDBMapper", "AmazonDynamoDB", "@DynamoDBTable"),
        description="AWS SDK v1 — DynamoDB client + mapper",
    ),
    "aws-java-sdk-sqs": ArtifactEntry(
        features=("Messaging", "AWS SQS", "Queue", "Cloud"),
        concept_names=("AmazonSQS", "SendMessageRequest", "ReceiveMessageRequest"),
        description="AWS SDK v1 — SQS messaging client",
    ),
    "software.amazon.awssdk:s3": ArtifactEntry(
        features=("Cloud Storage", "AWS S3", "Object Storage"),
        concept_names=("S3Client", "S3AsyncClient", "PutObjectRequest"),
        description="AWS SDK v2 — Amazon S3 client",
    ),

    # ── Observability ───────────────────────────────────────────────────────
    "micrometer-registry-prometheus": ArtifactEntry(
        features=("Metrics", "Prometheus", "Observability", "Monitoring"),
        concept_names=("MeterRegistry", "Counter", "Gauge", "Timer"),
        description="Micrometer — Prometheus metrics registry",
    ),
    "opentelemetry-sdk": ArtifactEntry(
        features=("Distributed Tracing", "Observability", "OpenTelemetry"),
        concept_names=("Span", "Tracer", "TracerProvider", "OpenTelemetry"),
        description="OpenTelemetry SDK for tracing + metrics",
    ),

    # ── Persistence ─────────────────────────────────────────────────────────
    "liquibase-core": ArtifactEntry(
        features=("Database Migration", "Schema Management", "Liquibase"),
        concept_names=("@ChangeLog", "LiquibaseChangeSet", "Liquibase"),
        description="Liquibase database change management",
    ),
    "flyway-core": ArtifactEntry(
        features=("Database Migration", "Schema Management", "Flyway"),
        concept_names=("Flyway", "FlywayMigration", "V1__migration"),
        description="Flyway SQL-based database migrations",
    ),

    # ── Utilities ───────────────────────────────────────────────────────────
    "lombok": ArtifactEntry(
        features=("Code Generation", "Boilerplate Reduction"),
        concept_names=("@Data", "@Getter", "@Setter", "@Builder",
                       "@NoArgsConstructor", "@AllArgsConstructor"),
        description="Project Lombok — compile-time code generation",
    ),
    "mapstruct": ArtifactEntry(
        features=("Object Mapping", "DTO Mapping", "Code Generation"),
        concept_names=("@Mapper", "@Mapping", "MapStruct"),
        description="MapStruct — compile-time bean mapper generation",
    ),
    "jackson-databind": ArtifactEntry(
        features=("JSON", "Serialization", "Deserialization"),
        concept_names=("ObjectMapper", "@JsonProperty", "@JsonIgnore",
                       "@JsonSerialize", "JsonNode"),
        description="Jackson — JSON serialization / deserialization",
    ),

    # ── NuGet / .NET ────────────────────────────────────────────────────────
    "amazon.lambda.core": ArtifactEntry(
        features=("Serverless", "AWS Lambda", "Cloud", "Function-as-a-Service"),
        concept_names=("ILambdaContext", "ILambdaLogger"),
        description="AWS Lambda .NET runtime core",
    ),
    "amazon.lambda.serialization.systemtextjson": ArtifactEntry(
        features=("Serverless", "JSON", "AWS Lambda"),
        concept_names=("DefaultLambdaJsonSerializer",),
        description="System.Text.Json serializer for AWS Lambda",
    ),
    "awssdk.s3": ArtifactEntry(
        features=("Cloud Storage", "AWS S3", "Object Storage"),
        concept_names=("AmazonS3Client", "PutObjectRequest"),
        description="AWS SDK for .NET — Amazon S3",
    ),
    "awssdk.sqs": ArtifactEntry(
        features=("Messaging", "AWS SQS", "Queue"),
        concept_names=("AmazonSQSClient", "SendMessageRequest"),
        description="AWS SDK for .NET — Amazon SQS",
    ),
    "awssdk.lambda": ArtifactEntry(
        features=("Serverless", "AWS Lambda", "Cloud"),
        concept_names=("AmazonLambdaClient", "InvokeRequest"),
        description="AWS SDK for .NET — Lambda invocation",
    ),
    "microsoft.aspnetcore.app": ArtifactEntry(
        features=("Web", "REST API", "ASP.NET Core", "HTTP"),
        concept_names=("IHostBuilder", "WebApplication", "Middleware"),
        description="ASP.NET Core meta-package",
    ),
    "microsoft.entityframeworkcore": ArtifactEntry(
        features=("Data", "ORM", "Entity Framework", "Persistence"),
        concept_names=("DbContext", "DbSet", "Migration", "ModelBuilder"),
        description="Entity Framework Core — .NET ORM",
    ),
    "nlog": ArtifactEntry(
        features=("Logging", "Structured Logging"),
        concept_names=("Logger", "LogManager", "NLogConfiguration"),
        description="NLog structured logging framework",
    ),
    "serilog": ArtifactEntry(
        features=("Logging", "Structured Logging"),
        concept_names=("Log", "ILogger", "LoggerConfiguration"),
        description="Serilog structured logging",
    ),

    # ── Python / pip ────────────────────────────────────────────────────────
    "boto3": ArtifactEntry(
        features=("Cloud", "AWS SDK", "S3", "DynamoDB", "SQS"),
        concept_names=("boto3.client", "boto3.resource", "Session"),
        description="AWS SDK for Python",
    ),
    "pillow": ArtifactEntry(
        features=("Image Processing", "Graphics"),
        concept_names=("Image", "ImageFilter", "ImageDraw"),
        description="Python Imaging Library (PIL fork)",
    ),
    "fastapi": ArtifactEntry(
        features=("Web", "REST API", "Async", "OpenAPI"),
        concept_names=("FastAPI", "@app.get", "Depends", "BaseModel"),
        description="FastAPI — async Python REST framework",
    ),
    "pydantic": ArtifactEntry(
        features=("Validation", "Data Modelling", "Serialization"),
        concept_names=("BaseModel", "Field", "model_validator"),
        description="Pydantic — data validation using Python type hints",
    ),
    "sqlalchemy": ArtifactEntry(
        features=("Data", "ORM", "SQL", "Persistence"),
        concept_names=("Base", "Session", "Column", "relationship"),
        description="SQLAlchemy — Python SQL toolkit and ORM",
    ),
    "celery": ArtifactEntry(
        features=("Async Tasks", "Background Jobs", "Distributed Tasks"),
        concept_names=("@app.task", "Celery", "AsyncResult"),
        description="Celery — distributed task queue",
    ),
    "kafka-python": ArtifactEntry(
        features=("Messaging", "Kafka", "Event Streaming"),
        concept_names=("KafkaProducer", "KafkaConsumer"),
        description="Python Apache Kafka client",
    ),
}


def lookup(artifact_name: str) -> ArtifactEntry | None:
    """Case-insensitive lookup by artifact / package name."""
    return _CATALOG.get(artifact_name.lower())


def all_entries() -> dict[str, ArtifactEntry]:
    """Return a copy of the full curated artifact-to-capability catalog."""
    return dict(_CATALOG)
