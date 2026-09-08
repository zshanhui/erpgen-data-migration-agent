# Local ERPNext demo (Docker)

Spun up from the official [frappe/frappe_docker](https://github.com/frappe/frappe_docker)
playground (`pwd.yml`) for local development and migration-tool testing.

## Quick reference

| Item | Value |
|---|---|
| URL | http://localhost:8082 |
| Site name | `frontend` |
| Login | `Administrator` / `admin` |
| ERPNext version | v16 (`frappe/erpnext:v16.33.0`) |
| DB | MariaDB 11.8 (root password `admin`, internal only) |
| Redis | 6.2 (cache + queue) |

> **Port note:** upstream `pwd.yml` maps the frontend to host `8080`, but a
> local process already listens on 8080, so the demo runs on **8082**. The
> override lives OUTSIDE the repo in `pwd.override.yml` (compose `!override`
> tag), keeping the `frappe_docker` clone byte-identical to upstream so
> upstream changes can be merged cleanly. The repo itself must stay pristine:
> all local tweaks go in this `docker/` dir or the override file.

## Commands

Use the helper (handles the keychain workaround + override below; run from
`data-migration/`):

```bash
./docker/erpnext.sh up        # start (first run creates the site)
./docker/erpnext.sh ps        # status
./docker/erpnext.sh logs      # follow all logs
./docker/erpnext.sh down      # stop (keeps volumes)
```

Or manually (from `frappe_docker/`):

```bash
export DOCKER_CONFIG=/Users/lishanhui/oss/nyauto/erpgen/data-migration/docker/docker-config
export DOCKER_HOST=unix://$HOME/.orbstack/run/docker.sock
docker compose -f pwd.yml -f ../data-migration/docker/pwd.override.yml up -d
docker compose -f pwd.yml -f ../data-migration/docker/pwd.override.yml down -v   # stop + wipe data volumes
```

> The `frappe_docker` clone is intentionally left untouched (`git status` clean)
> so upstream `frappe/frappe_docker` changes merge without conflicts.

> **Keychain workaround:** this machine's global `~/.docker/config.json` sets
> `credsStore: osxkeychain`, which fails from non-interactive shells with
> `Keychain Error (-67674)` even for public images. The project-local config in
> `docker/docker-config/` omits `credsStore`; combined with `DOCKER_HOST`
> pointing at OrbStack's socket it bypasses the keychain entirely. Your own
> interactive terminal may not need this.
>
> Note: overriding `DOCKER_CONFIG` also hides the CLI plugins (docker-compose,
> docker-buildx) from `~/.docker/cli-plugins/`, so `docker-config/cli-plugins/`
> contains symlinks to OrbStack's bundled binaries. If they ever go missing,
> re-create them with:
> `ln -sfn /Applications/OrbStack.app/Contents/MacOS/xbin/docker-compose docker-config/cli-plugins/docker-compose`

## First-run: setup wizard

A freshly created ERPNext site has **no company and no master data** (v16 no
longer seeds defaults). Until the setup wizard runs, inserts fail with
`LinkValidationError`. Run it once via API (idempotent):

```bash
bash scripts/setup-demo.sh
```

This creates Company "Demo Manufacturing" (DM, USD), Standard chart of
accounts, Customer Groups (All Customer Groups / Individual / Commercial / Non
Profit / Government), Territories, Item Groups (Products, Raw Material, …),
239 UOMs, Cost Centers, Warehouses, and Price Lists — mirroring what clicking
through the browser wizard does.

The `create-site` service runs once and exits after creating site `frontend`
with ERPNext installed; the `backend` service serves it afterwards.

## Useful REST endpoints for the migration tool

- Login (get API key): `POST /api/method/login` with `usr` / `pwd`, or use
  `Authorization: token api_key:api_secret` with keys generated in
  `System Settings` / user profile.
- DocType metadata: `GET /api/resource/DocType/Customer` or the lighter
  `GET /api/method/frappe.client.get_list?doctype=DocField&filters=[["parent","=","Customer"]]`
- Insert: `POST /api/resource/Customer` with JSON body.
- Bulk import: `POST /api/method/frappe.core.doctype.data_import.data_import.import_file`.

Full API docs ship with the site at `/api/method/frappe.integrations.frappe_providers...`
no — see https://docs.frappe.io/framework/user/en/api/rest
