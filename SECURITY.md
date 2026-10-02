# Security and data boundaries

- Runtime has no third-party dependencies and no credential requirement. The live provider is opt-in; demo and tests are offline.
- No original private logs, email recipients, credentials, pickles or binaries are included. JSON is schema-validated; pickle is never loaded.
- The notifier records to a local SQLite journal. It does not send email or make purchases, conversions or trades.
- State databases are local, unencrypted files. Keep them outside public repositories. The `.gitignore` excludes common state extensions and `.env` files.
- The optional server binds to loopback, verifies its Host header and exposes only read-only routes. Do not reverse-proxy it to the internet; it has no authentication or production hardening.
- Malformed inputs and external responses have size limits. Cooldowns and replay decisions are durable; failed provider requests do not silently generate quotes.

Please report a security issue through the repository's private vulnerability reporting feature if available. Otherwise use the author's linked contact route without posting secrets in a public issue.
