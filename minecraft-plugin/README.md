# MellowBridge Paper plugin

Private authenticated bridge for adding a player to the Paper whitelist. It is deliberately outbound-only from the bot's point of view: the plugin exposes a small HTTP listener, not an unauthenticated game command or public API.

## Build and install

Requires Java 21, Maven 3.9+, and Paper 1.21.4. From this directory:

```sh
mvn package
cp target/mellow-bridge-0.1.0.jar /path/to/server/plugins/
```

Start the server once to create `plugins/MellowBridge/config.yml`, stop it, configure the API, and restart. Generate a high-entropy secret using `openssl rand -hex 32`; set it in the plugin config and bot `.env` as the exact same `MINECRAFT_API_TOKEN`. Keep the config readable only by the server account. The plugin refuses to start with an example/short token or when `online-mode` is disabled (unless the security option is explicitly weakened; this is not recommended).

Defaults bind to `127.0.0.1:8765`, permitting only IPv4/IPv6 loopback. When bot and Minecraft run on separate hosts, bind only to a private interface, add only the bot's private source IP to `allowed-ips`, restrict the port with a firewall, and put HTTPS/TLS in front of the bridge. Never expose this plain HTTP listener to the public internet.

The bot sends `POST /v1/whitelist/add` with a Bearer token, validated username, and stable `Idempotency-Key`. The plugin checks both source IP and the token in constant-time, resolves the Minecraft profile through Paper/Mojang, applies changes on the server thread, checks the whitelist afterward, and persists confirmed operation results. A repeated request for a successful operation returns the cached result; reusing a key for another username is rejected. Transient/unconfirmed results are not cached, so the bot can safely retry the same key.

The endpoint accepts only 3–16 character Minecraft usernames. Online mode and UUID resolution avoid treating a mutable display name as the account identity. Check server logs for `MellowBridge` startup errors; the API has an authenticated `GET /v1/health` endpoint.
