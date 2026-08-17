# Security

Report suspected vulnerabilities privately to `security@reefy.ai`. Do not open
a public issue containing credentials, camera URLs, network addresses, or
reproduction data from a real installation.

CamAdmiral is intended to run on a trusted private network. Internet or
cross-network access must terminate TLS and authentication at a trusted reverse
proxy. The go2rtc API remains loopback-only and must never be published.

Security fixes are released only after the unit, component, and isolated E2E
release gate passes.
