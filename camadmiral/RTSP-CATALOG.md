# CamAdmiral RTSP compatibility catalog

This catalog is an independently maintained set of small, factual RTSP path
rules. It is not copied or derived from StrixCamDB, iSpyConnect, or another
camera URL database.

Every rule must include:

- a stable CamAdmiral rule ID;
- a public HTTPS URL to first-party vendor documentation;
- only the minimum manufacturer aliases needed for prioritization;
- no usernames, passwords, customer addresses, or other deployment data;
- no more than four paths per rule.

The loader validates those constraints and applies a global probe limit. A
successful adoption stores the catalog revision, rule ID, provenance URL, and
the exact resolved source. Later catalog revisions cannot silently replace an
adopted source.

Adding a rule requires a synthetic resolver test and human review of the
linked documentation. Community reports can identify documentation to review,
but are not sufficient provenance for a bundled rule by themselves.
