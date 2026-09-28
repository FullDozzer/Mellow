package dev.mellow.plugin;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.bukkit.Bukkit;
import org.bukkit.OfflinePlayer;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.profile.PlayerProfile;

import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** A small authenticated bridge. No unauthenticated or public control endpoint is exposed. */
public final class MellowPlugin extends JavaPlugin {
    private static final Pattern USERNAME = Pattern.compile("^[A-Za-z0-9_]{3,16}$");
    private static final Pattern OPERATION_KEY = Pattern.compile("^[A-Za-z0-9_-]{1,64}$");
    private static final Gson GSON = new Gson();
    private record InFlightRequest(String username, CompletableFuture<String> result) {}
    private final Map<String, InFlightRequest> inFlight = new ConcurrentHashMap<>();
    private final Object operationLock = new Object();
    private HttpServer server;
    private String token;
    private Set<String> allowedIps;
    private int timeoutSeconds;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        token = getConfig().getString("api.token", "");
        allowedIps = Set.copyOf(getConfig().getStringList("api.allowed-ips"));
        timeoutSeconds = Math.max(2, getConfig().getInt("api.request-timeout-seconds", 10));
        if (token.length() < 32 || token.startsWith("CHANGE_ME")) {
            getLogger().severe("API token is missing or too short (minimum 32 characters); bridge will not start.");
            Bukkit.getPluginManager().disablePlugin(this);
            return;
        }
        if (getConfig().getBoolean("security.require-online-mode", true) && !Bukkit.getOnlineMode()) {
            getLogger().severe("Online mode is required for safe account identity. Bridge will not start.");
            Bukkit.getPluginManager().disablePlugin(this);
            return;
        }
        loadOperations();
        try {
            String bind = getConfig().getString("api.bind", "127.0.0.1");
            int port = getConfig().getInt("api.port", 8765);
            server = HttpServer.create(new InetSocketAddress(bind, port), 32);
            server.createContext("/v1/health", this::handleHealth);
            server.createContext("/v1/whitelist/add", this::handleWhitelistAdd);
            server.setExecutor(Executors.newFixedThreadPool(4, runnable -> {
                Thread thread = new Thread(runnable, "MellowBridge-HTTP");
                thread.setDaemon(true);
                return thread;
            }));
            server.start();
            getLogger().info("Authenticated API listening on " + bind + ":" + port);
        } catch (IOException | IllegalArgumentException exception) {
            getLogger().severe("Could not start the private API: " + exception.getClass().getSimpleName());
            Bukkit.getPluginManager().disablePlugin(this);
        }
    }

    @Override
    public void onDisable() {
        if (server != null) {
            server.stop(1);
        }
    }

    private void handleHealth(HttpExchange exchange) throws IOException {
        if (!authorized(exchange)) return;
        if (!"GET".equals(exchange.getRequestMethod())) {
            respond(exchange, 405, response("ERROR", "Method not allowed"));
            return;
        }
        respond(exchange, 200, response("SUCCESS", "MellowBridge is ready"));
    }

    private void handleWhitelistAdd(HttpExchange exchange) throws IOException {
        if (!authorized(exchange)) return;
        if (!"POST".equals(exchange.getRequestMethod())) {
            respond(exchange, 405, response("ERROR", "Method not allowed"));
            return;
        }
        String key = exchange.getRequestHeaders().getFirst("Idempotency-Key");
        if (key == null || !OPERATION_KEY.matcher(key).matches()) {
            respond(exchange, 400, response("ERROR", "A valid Idempotency-Key is required"));
            return;
        }
        JsonObject input;
        try {
            byte[] raw = exchange.getRequestBody().readNBytes(2049);
            if (raw.length > 2048) {
                respond(exchange, 413, response("ERROR", "Request too large"));
                return;
            }
            input = JsonParser.parseString(new String(raw, StandardCharsets.UTF_8)).getAsJsonObject();
        } catch (RuntimeException exception) {
            respond(exchange, 400, response("ERROR", "Invalid JSON"));
            return;
        }
        String username = input.has("username") && input.get("username").isJsonPrimitive()
                ? input.get("username").getAsString() : "";
        if (!USERNAME.matcher(username).matches()) {
            respond(exchange, 400, response("ERROR", "Invalid Minecraft username"));
            return;
        }

        String existing = cachedStatus(key, username);
        if (existing != null) {
            if ("KEY_CONFLICT".equals(existing)) {
                respond(exchange, 409, response("ERROR", "Idempotency key was already used for a different account"));
            } else {
                respond(exchange, 200, response(existing, "Cached result"));
            }
            return;
        }
        CompletableFuture<String> mine = new CompletableFuture<>();
        InFlightRequest candidate = new InFlightRequest(username, mine);
        InFlightRequest active = inFlight.putIfAbsent(key, candidate);
        CompletableFuture<String> future;
        if (active == null) {
            future = mine;
            resolveAndAdd(username, key, mine);
        } else {
            if (!active.username().equalsIgnoreCase(username)) {
                respond(exchange, 409, response("ERROR", "Idempotency key is already in progress for a different account"));
                return;
            }
            future = active.result();
        }
        try {
            String result = future.get(timeoutSeconds, TimeUnit.SECONDS);
            respond(exchange, 200, response(result, "Whitelist operation confirmed"));
        } catch (Exception exception) {
            // Keep the same operation key retryable; the server-side cache prevents duplicate adds.
            respond(exchange, 503, response("ERROR", "Minecraft server did not confirm the operation"));
        }
    }

    private void resolveAndAdd(String username, String key, CompletableFuture<String> result) {
        // Resolve the Mojang profile outside the Bukkit main thread, then mutate whitelist on main.
        try {
            PlayerProfile profile = Bukkit.createPlayerProfile(username);
            profile.update().orTimeout(timeoutSeconds, TimeUnit.SECONDS).whenComplete((resolved, error) -> {
                if (error != null || resolved == null || resolved.getUniqueId() == null
                        || resolved.getName() == null || !resolved.getName().equalsIgnoreCase(username)) {
                    finishError(key, result);
                    return;
                }
                UUID uuid = resolved.getUniqueId();
                Bukkit.getScheduler().runTask(this, () -> {
                    try {
                        OfflinePlayer player = Bukkit.getOfflinePlayer(uuid);
                        boolean already = Bukkit.getWhitelistedPlayers().stream()
                                .anyMatch(existing -> uuid.equals(existing.getUniqueId()));
                        if (already) {
                            persistOperation(key, username, "ALREADY_WHITELISTED");
                            finish(key, result, "ALREADY_WHITELISTED");
                            return;
                        }
                        player.setWhitelisted(true);
                        boolean confirmed = Bukkit.getWhitelistedPlayers().stream()
                                .anyMatch(existing -> uuid.equals(existing.getUniqueId()));
                        if (!confirmed) {
                            finishError(key, result);
                            return;
                        }
                        persistOperation(key, username, "SUCCESS");
                        finish(key, result, "SUCCESS");
                    } catch (Exception exception) {
                        getLogger().warning("Whitelist operation failed (" + exception.getClass().getSimpleName() + ")");
                        finishError(key, result);
                    }
                });
            });
        } catch (Exception exception) {
            getLogger().warning("Minecraft profile lookup could not start (" + exception.getClass().getSimpleName() + ")");
            finishError(key, result);
        }
    }

    private String cachedStatus(String key, String username) {
        synchronized (operationLock) {
            String prefix = "operations." + key + ".";
            if (!getConfig().contains(prefix + "status")) return null;
            if (!username.equalsIgnoreCase(getConfig().getString(prefix + "username", ""))) return "KEY_CONFLICT";
            return getConfig().getString(prefix + "status");
        }
    }

    private void persistOperation(String key, String username, String status) {
        synchronized (operationLock) {
            String prefix = "operations." + key + ".";
            getConfig().set(prefix + "username", username);
            getConfig().set(prefix + "status", status);
            saveConfig();
        }
    }

    private void loadOperations() {
        // Operations are stored in the plugin's config file so confirmed IDs survive restart.
        getConfig().getConfigurationSection("operations");
    }

    private void finish(String key, CompletableFuture<String> future, String status) {
        inFlight.computeIfPresent(key, (ignored, active) -> active.result() == future ? null : active);
        future.complete(status);
    }

    private void finishError(String key, CompletableFuture<String> future) {
        inFlight.computeIfPresent(key, (ignored, active) -> active.result() == future ? null : active);
        future.complete("ERROR");
    }

    private boolean authorized(HttpExchange exchange) throws IOException {
        InetAddress remote = exchange.getRemoteAddress().getAddress();
        String address = remote == null ? "" : remote.getHostAddress();
        String auth = exchange.getRequestHeaders().getFirst("Authorization");
        boolean ipAllowed = allowedIps.contains(address);
        byte[] provided = auth == null ? new byte[0] : auth.getBytes(StandardCharsets.UTF_8);
        byte[] expected = ("Bearer " + token).getBytes(StandardCharsets.UTF_8);
        boolean tokenValid = MessageDigest.isEqual(provided, expected);
        if (!ipAllowed || !tokenValid) {
            respond(exchange, 401, response("ERROR", "Unauthorized"));
            return false;
        }
        return true;
    }

    private static JsonObject response(String status, String message) {
        JsonObject json = new JsonObject();
        json.addProperty("status", status);
        json.addProperty("message", message);
        return json;
    }

    private static void respond(HttpExchange exchange, int status, JsonObject body) throws IOException {
        byte[] bytes = GSON.toJson(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) {
            output.write(bytes);
        }
    }
}
