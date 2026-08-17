# CamAdmiral camera catalog

This directory contains independently maintained RTSP path facts taken from
vendor documentation. It does not contain data copied from StrixCamDB,
iSpyConnect, or another camera URL database.

Each rule must cite an HTTPS vendor documentation page, remain narrowly scoped,
and include no credentials. Run `python3 scripts/compile-rtsp-catalog.py --check`
before committing a change. The generated runtime artifact is
`camadmiral/rtsp_catalog.json`.
