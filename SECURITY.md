# Security Policy

## Sensitive data

Douyin Cookie JSON is an account credential. Never include it in an Issue,
pull request, log excerpt, screenshot, or public backup. If a Cookie is exposed,
invalidate the related login session and export a new Cookie before continuing.

An active login QR code is also a short-lived credential. Do not share or
publish it. The QR login flow keeps the image and pending browser session only
in server memory, never returns captured Cookies to the browser, and writes the
final login state only to the private data volume.

The web console should listen on localhost and be served through HTTPS. Keep
Basic Auth enabled unless another authenticated gateway fully protects it.
Use HTTPS for QR login so the code and account metadata cannot be observed in
transit. Public GitHub Actions runs are not recommended for real account tasks,
because workflow logs in a public repository can reveal account metadata.

## Reporting a vulnerability

Do not open a public Issue containing an exploitable vulnerability or any real
credential. Contact the maintainer privately through the security reporting
channel configured on the GitHub repository. Include reproduction steps using
sanitized test data only.
