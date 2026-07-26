# Oracle Instant Client (Basic 19.31)

Thick mode in the mcp-proxy image needs **Oracle Instant Client Basic linux x64 19.31**.

## Preferred (offline / reproducible)

Download from Oracle and place the zip here (do not commit the zip if license forbids redistribution):

```text
instantclient-basic-linux.x64-19.31.0.0.0dbru.zip
```

Expected path:

```text
mcp/app/instantclient/instantclient-basic-linux.x64-19.31.0.0.0dbru.zip
```

Build will detect any `instantclient-basic-linux*.zip` or `*19.31*.zip` in this directory.

## Fallback (online build)

If no zip is present, the Dockerfile downloads:

```text
https://download.oracle.com/otn_software/linux/instantclient/1931000/instantclient-basic-linux.x64-19.31.0.0.0dbru.zip
```

Override URL:

```bash
docker build --build-arg INSTANTCLIENT_URL='https://...' -t mcp-proxy .
```

## Notes

- **amd64/x86_64 only** for this 19.31 Basic package layout
- Image runtime is Debian bookworm (glibc); Alpine/musl cannot load Instant Client
- Runtime env: `THICK_MODE=1`, `ORACLE_CLIENT_LIB_DIR=/opt/oracle/instantclient_19_31`
