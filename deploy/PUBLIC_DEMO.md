# Tencent Cloud public showcase

Live workbench: https://benluo.art/foldspace/  
Portfolio case study: https://benluo.art/projects/protein-studio/

The public service uses a separate Docker volume seeded with one published AFDB task and a three-target reference campaign. It supports molecular interaction, confidence / PAE analysis, completed-target inspection and artifact downloads. Task submissions, imports, cancellation, storage changes and worker APIs are rejected by both the public handler and the Nginx method policy. Native weights, search databases and GPU execution are configured separately in the full local/private application.

## Start

```sh
docker compose -p foldspace-public \
  -f compose.yaml -f deploy/public-demo.compose.yaml up -d --build
```

The container is bound only to `127.0.0.1:8765`. Include `public-demo.nginx.conf.example` inside the existing HTTPS virtual host, validate with `nginx -t`, then reload. `PROTEIN_STUDIO_BASE_PATH=/foldspace` owns path generation in the application, including API, structure, PAE, exports and worker client URLs. HTTPS origin is explicitly configured as `PROTEIN_STUDIO_PUBLIC_ORIGIN=https://benluo.art`; arbitrary forwarded headers are not trusted.

`PROTEIN_STUDIO_PUBLIC_DATA_DIR=/app/data` must point to a dedicated public-demo volume. The initializer rejects nonempty unmarked directories and refuses workloads outside its published fixture identity. Seeding finishes before opening the HTTP listener. The volume survives container restarts, retaining the seed IDs and output checksums.

Limits in the public override: 384 MiB memory, 0.75 CPU, 96 processes, bounded container logs. These are operational limits for the public reference showcase, not GPU throughput measurements.

## Deployed layout and rollback

- Application release: `/opt/protein-studio/releases/20261025-public` (2026-10-25 update through commit `9748e3b`; previous `20261023-public` directory retained — the `protein-studio:public` tag is overwritten on rebuild, so rollback means rebuilding from the old release directory)
- Compose project / container: `foldspace-public` / `foldspace-public-studio-1`
- Nginx snippet: `/etc/nginx/snippets/foldspace.conf`
- Previous Nginx configuration: `/opt/protein-studio/backups/nginx-default-before-foldspace.conf`
- Portfolio static release: `/opt/portfolio-fe/releases/foldspace-20261023`
- Portfolio previous image: `portfolio-fe-frontend:before-foldspace-20261023`

For a workbench rollback, remove its two Nginx include lines (or restore the saved configuration), test Nginx, and reload; the portfolio and its other upstreams remain independently managed. Stop the dedicated Compose project without `-v` to retain public-demo data. The portfolio can independently return to its previous image tag and recreate only its `frontend` service.

The portfolio release is built from the existing local working tree with the Foldspace addition; preexisting local changes were preserved. It adds a featured project in both languages, a dedicated static detail route, two preview images, engineering explanations and verified public demo / GitHub links.

## Verification

- Public deployment / mount / worker-path focused backend tests: 14 passed.
- Existing API, PAE and proxy compatibility: 27 passed; worker mode/storage/path compatibility: 10 passed.
- Prefix browser: real 3Dmol rendering, PAE, sources and downloads; no root URL leaks.
- Public HTTPS browser: 229-residue AFDB view, PAE, three completed targets, read-only controls, mobile layout; no browser mutation requests or runtime errors.
- Portfolio: type check, lint, static export, 79 Jest tests and 7 citation tests passed; homepage/list/detail, bilingual gallery and 390px layout verified over HTTPS.
- Existing RAG health and Stock Agents frontend remain HTTP 200.
- 2026-10-25 update: rsync release + compose rebuild (pip layer now on the Aliyun PyPI mirror), container healthy; same-origin `/api/health` 200 over HTTPS, public `app.js` carries the pDockQ family and interface-focus features; direct host curl without the public origin is rejected by design.

Detailed generated evidence stays outside Git under `artifacts/`, including `tencent-foldspace-browser.json`, `portfolio-foldspace-live-verification.json` and screenshots. No production GPU prediction was executed in this deployment.
