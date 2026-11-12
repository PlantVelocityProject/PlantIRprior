# Container network diagnosis

Use for download TLS, connection or timeout failures. Do not assume that a host request proves container connectivity, or that every failure is a transport fault.

## Compare the actual routes

Formal stages use `singularity exec --userns --cleanenv`, which does not automatically preserve ordinary host proxy variables. Compare the same failing endpoint from the host and the selected container environment before blaming an accession. Relevant symptoms include curl 28/35, TLS EOF, backend exit 1 and download timeouts; each still needs a verified cause.

A local deployment previously required proxy access for S3/EBI/eutils and a direct route for locate.ncbi.nlm.nih.gov. The recorded proxy was `http://127.0.0.1:17898`. These are deployment observations, not permanent requirements: verify the current proxy, endpoint and route before applying them.

## Apply only a verified transport change

When changing the transport environment is within the user's authorized repair scope, inject selected variables with `SINGULARITYENV_*`. For the recorded local proxy, the example is:

```bash
systemctl --user set-environment   SINGULARITYENV_HTTPS_PROXY=http://127.0.0.1:17898   SINGULARITYENV_HTTP_PROXY=http://127.0.0.1:17898   SINGULARITYENV_https_proxy=http://127.0.0.1:17898   SINGULARITYENV_http_proxy=http://127.0.0.1:17898   SINGULARITYENV_NO_PROXY=localhost,127.0.0.1,::1,locate.ncbi.nlm.nih.gov   SINGULARITYENV_no_proxy=localhost,127.0.0.1,::1,locate.ncbi.nlm.nih.gov
```

Transient task units inherit the user manager environment, so an API-service-only drop-in may not affect them. Persistence in `~/.config/environment.d/50-plantiragent-proxy.conf` is a separate environment change; preserve unrelated settings and do not rewrite it automatically during an ordinary update.

Verify the route in the actual task/container and check only relevant, redacted variables. Do not dump an entire process environment, proxy credentials or API keys. Preserve partial downloads and use the bounded recovery procedure in [Operations](operations.md#recovery-decisions).

For requested cancellation use [Stop instructions](operations.md#stop-only-the-requested-execution); catalog/file-type maintenance is also covered in Operations. Neither action is a network repair by default.
