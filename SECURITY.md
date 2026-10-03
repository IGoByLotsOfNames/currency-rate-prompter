# Security and data boundaries

- The application runtime uses the Python standard library and requires no credentials. Reference-provider access is explicit; the offline demo and automated tests use synthetic or injected data.
- Original private logs, email recipients, credentials, pickles and binaries are not part of the published revision. JSON inputs are schema-validated; the maintained implementation does not load pickle files.
- Alert delivery records to a local SQLite journal. It does not send email or make purchases, conversions or trades.
- State databases are local, unencrypted files. Keep them outside public repositories. The `.gitignore` excludes common state extensions, generated sessions and `.env` files.
- Both HTTP interfaces bind to loopback. The `serve` command provides read-only inspection. The interactive `app` command permits state-changing operations, protected by bounded JSON bodies, local Host/Origin checks and a per-server CSRF token. Neither is an authenticated public-hosting design; do not expose them through a public reverse proxy.
- Malformed inputs and external responses have size limits. Cooldowns and replay decisions are durable; failed provider requests do not silently generate quotes or discard existing history.

Please report a security issue through the repository's private vulnerability reporting feature if available. Otherwise use the author's linked contact route without posting secrets in a public issue.
