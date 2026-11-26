# 0012. Captcha and security checks are solved by the owner

Date: 2026-11-26 · Status: accepted

## Decision
On captcha, 2FA, a security check, 403/429 or a robots disallow, the task pauses and the platform
backs off. For a captcha or 2FA in the worker, the worker shows its window and messages the owner;
the owner solves it and the task resumes. No captcha-solving services, proxy rotation or browser
fingerprint changes are used. Volume stays human-scale and is configurable per platform.
