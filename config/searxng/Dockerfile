# Corporate networks intercept outbound TLS (MITM proxy); trust those roots
# so SearXNG can actually reach upstream search engines. Base image is Void Linux.
FROM searxng/searxng:latest
COPY certs/*.crt /usr/local/share/ca-certificates/
USER root
RUN update-ca-certificates
USER searxng
