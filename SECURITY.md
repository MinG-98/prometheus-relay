# Security Policy

## Sensitive data

Douyin Cookie JSON is an account credential. Never include it in an Issue,
pull request, log excerpt, screenshot, or public backup. If a Cookie is exposed,
invalidate the related login session and export a new Cookie before continuing.

The web console should listen on localhost and be served through HTTPS. Keep
Basic Auth enabled unless another authenticated gateway fully protects it.

## Reporting a vulnerability

Do not open a public Issue containing an exploitable vulnerability or any real
credential. Contact the maintainer privately through the security reporting
channel configured on the GitHub repository. Include reproduction steps using
sanitized test data only.
