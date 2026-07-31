# MCP 调用鉴权方案

本文说明在当前 `mcp-proxy` 部署形态下，如何为 MCP 调用增加鉴权，重点覆盖：

- 统一访问方式：`type: http` + `/servers/<name>/mcp`
- Kubernetes + Ingress Nginx `auth-url`
- 鉴权服务使用 Spring Boot 实现，并从原始路径解析 MCP 服务名

---

## 1. 背景与目标

### 1.1 当前访问方式

客户端统一使用 Streamable HTTP：

```json
{
  "mcpServers": {
    "loki-xuan": {
      "type": "http",
      "url": "https://ai-mcp-sic.gdalpha.com/servers/loki-xuan/mcp",
      "headers": {
        "Authorization": "Bearer sk-xxx"
      }
    }
  }
}
```

对应链路：

```text
Client
  → Ingress (ai-mcp-sic.gdalpha.com)
  → Service mcp-proxy:8080
  → mcp-proxy Pod
  → /servers/<name>/mcp
  → 本地 stdio MCP 子进程（如 loki-xuan）
```

路径约定：

| 路径 | 说明 |
|------|------|
| `/servers/<name>/mcp` | Streamable HTTP（当前主用） |
| `/servers/<name>/sse` | SSE（如仍启用） |
| `/servers/<name>/messages/` | SSE 消息通道 |
| `/status` | 健康检查 / 状态 |

`<name>` 来自 `servers.json` 中 `mcpServers` 的 key，例如 `loki-xuan`、`oracle`。

### 1.2 鉴权分层

```text
Client  ──① 入站鉴权──►  mcp-proxy / Ingress  ──② 出站鉴权──►  上游（如有）
                              │
                              └──③ tool 级授权（可选）
```

| 层级 | 现状 | 建议 |
|------|------|------|
| 出站（proxy → 远端 MCP） | 已有 `--headers` / `API_ACCESS_TOKEN` / OAuth2 Client Credentials | 继续使用 |
| 入站（Client → proxy） | 基本没有（仅 CORS） | **必须补齐**（公网 Ingress 场景） |
| Tool 级 | 无 | 有细粒度需求时再做 |

### 1.3 推荐结论

针对统一的：

```text
type: http
url: https://.../servers/<name>/mcp
```

**推荐：Ingress Nginx `auth-url` + Spring Boot 鉴权服务**。

理由：

1. Streamable HTTP 每次请求都可带 `Authorization`，适合 header 鉴权。
2. MCP 服务名在 URL 中：`/servers/<name>/...`，鉴权服务可直接解析，无需客户端再传 server 名。
3. 与现有 K8s / Ingress 部署一致，应用侧改动小。
4. 可按「用户/token × MCP 服务名」做授权。

可选增强：mcp-proxy 进程内再做一层 API Key（defense in depth）。本文主方案以 Ingress Auth 为准。

---

## 2. 总体架构

```text
Client
  │  POST /servers/loki-xuan/mcp
  │  Authorization: Bearer sk-xxx
  ▼
Ingress Nginx
  │  ① 子请求 GET auth-url（无 body）
  │     X-Original-Path: /servers/loki-xuan/mcp
  │     Authorization: Bearer sk-xxx
  ▼
Spring Boot 鉴权服务  (/auth/verify)
  │  解析 server = loki-xuan
  │  校验 token + ACL
  │  200：放行（可回写 X-User-Id、X-MCP-Server）
  │  401：未认证
  │  403：已认证但无该 server 权限
  ▼
（仅 200）转发到 mcp-proxy
  ▼
stdio MCP（loki-xuan 等）
```

### 2.1 职责划分

| 组件 | 职责 |
|------|------|
| 客户端 | 请求 URL 带 `/servers/<name>/mcp`，Header 带 Token |
| Ingress | TLS、超时、调用 `auth-url`，把原始 path/method/token 传给鉴权服务 |
| Spring Boot 鉴权服务 | 验 Token、解析 MCP 服务名、按 ACL 授权 |
| mcp-proxy | 反代到 named stdio MCP；可接收 `X-User-Id` 做审计 |
| 后端 MCP | 业务工具，一般不再做 HTTP 鉴权 |

### 2.2 安全基线

1. 公网域名必须鉴权；`mcp-proxy` 仅 ClusterIP，不额外 NodePort 裸奔。
2. Token 放 K8s Secret，不进 git / 明文 ConfigMap。
3. 服务名**以 URL path 解析结果为准**，不信任客户端自定义的 server 头。
4. 日志 mask `Authorization` / `X-Api-Key`，可打 `userId` + `server`。
5. `/status` 对探针放行，但不返回密钥或内部命令信息。
6. 支持多 Token，便于轮换（新旧并存）。

---

## 3. 客户端配置

### 3.1 推荐

```json
{
  "mcpServers": {
    "loki-xuan": {
      "type": "http",
      "url": "https://ai-mcp-sic.gdalpha.com/servers/loki-xuan/mcp",
      "headers": {
        "Authorization": "Bearer sk-loki-reader"
      }
    }
  }
}
```

### 3.2 兼容

部分客户端可用：

```http
Authorization: Bearer sk-xxx
X-Api-Key: sk-xxx
```

只要最终 HTTP 请求带上其一即可。不要用 query `?token=`（易进日志、Referer、分享链接）。

### 3.3 说明

- Streamable HTTP 还会带 MCP 协议头（如 `mcp-session-id`），鉴权只读 Token 相关头，不要改动其它头。
- 不同宿主字段名可能是 `headers` 或 `requestInit.headers`，语义相同。

---

## 4. Ingress Nginx 配置

### 4.1 核心注解

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mcp-proxy
  namespace: kube-ops
  labels:
    app: mcp-proxy
    app.kubernetes.io/name: mcp-proxy
  annotations:
    # 鉴权服务（集群内 DNS）
    nginx.ingress.kubernetes.io/auth-url: "http://mcp-ingress-auth.kube-ops.svc.cluster.local:8081/auth/verify"
    nginx.ingress.kubernetes.io/auth-method: GET
    # 将鉴权服务 200 响应头继续传给 mcp-proxy（可选）
    nginx.ingress.kubernetes.io/auth-response-headers: X-User-Id,X-MCP-Server

    # 显式把原始请求信息传给 auth 子请求
    nginx.ingress.kubernetes.io/auth-snippet: |
      proxy_set_header X-Original-URI $request_uri;
      proxy_set_header X-Original-Path $uri;
      proxy_set_header X-Original-Method $request_method;
      proxy_set_header X-Original-URL $scheme://$host$request_uri;
      proxy_set_header Authorization $http_authorization;
      proxy_set_header X-Api-Key $http_x_api_key;
      proxy_set_header X-Real-IP $remote_addr;

    # Streamable HTTP / 长连接
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"

    # 若仍使用 SSE，可保留粘性；纯 /mcp + --stateless 时非必须
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/session-cookie-name: "mcp-proxy-affinity"
    nginx.ingress.kubernetes.io/session-cookie-max-age: "10800"
spec:
  ingressClassName: nginx
  rules:
    - host: ai-mcp-sic.gdalpha.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: mcp-proxy
                port:
                  number: 8080
```

### 4.2 鉴权服务能拿到的信息

| Header | 含义 | 用途 |
|--------|------|------|
| `X-Original-Path` | 原始路径，如 `/servers/loki-xuan/mcp` | **解析 MCP 服务名** |
| `X-Original-URI` | 路径 + query | 备用 |
| `X-Original-URL` | 完整 URL | 备用 |
| `X-Original-Method` | 原始 HTTP 方法 | 审计 |
| `Authorization` | Bearer Token | 身份认证 |
| `X-Api-Key` | 可选 API Key | 身份认证 |

### 4.3 能否获取 MCP 服务名？

**可以。** 服务名在路径中：

```text
/servers/<mcpServerName>/mcp
         └─────────────┘
            loki-xuan
```

鉴权服务用正则即可：

```text
^/servers/([^/]+)(?:/.*)?$
```

示例：

| 原始请求 | 解析出的 server |
|----------|-----------------|
| `/servers/loki-xuan/mcp` | `loki-xuan` |
| `/servers/oracle/mcp` | `oracle` |
| `/servers/loki-xuan/sse` | `loki-xuan` |
| `/status` | 无（建议放行） |

注意：

1. auth 子请求**默认不带 body**，不能按 JSON-RPC tool 名鉴权；server 级授权完全够用 path。
2. 不要在鉴权前用 `rewrite-target` 把 `/servers/<name>` 剥掉，否则 path 里会丢掉服务名。
3. mcp-proxy 必须仍收到完整路径 `/servers/<name>/mcp`（命名路由依赖它）。

### 4.4 状态码约定

| 状态 | 含义 | Ingress 行为 |
|------|------|----------------|
| `200` | 通过 | 转发到 mcp-proxy |
| `401` | 未认证（无/错 Token） | 拒绝 |
| `403` | 已认证但无权访问该 server | 拒绝 |
| 不要用 `302` | API/Agent 场景不适合跳转登录页 | — |

### 4.5 `/status` 与探针

- K8s `readinessProbe` / `livenessProbe` 若走 **Service 直连**，不经 Ingress，则不受 `auth-url` 影响（当前 Deployment 即此模式）。
- 若外网监控也走 Ingress，鉴权服务需对 `/status` 放行。

---

## 5. Spring Boot 鉴权服务

### 5.1 模块目标

提供：

```text
GET|HEAD /auth/verify
```

供 Ingress `auth-url` 回调。成功 `200`，失败 `401/403`。

### 5.2 依赖（`pom.xml` 摘要）

```xml
<parent>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-starter-parent</artifactId>
  <version>3.3.5</version>
</parent>

<properties>
  <java.version>17</java.version>
</properties>

<dependencies>
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-web</artifactId>
  </dependency>
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-validation</artifactId>
  </dependency>
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
  </dependency>
</dependencies>
```

### 5.3 配置（`application.yml`）

```yaml
server:
  port: 8081

management:
  endpoints:
    web:
      exposure:
        include: health,info
  endpoint:
    health:
      probes:
        enabled: true

mcp:
  auth:
    public-paths:
      - /status
    # token -> 可访问的 MCP server 列表；* 表示全部
    # 生产请用 Secret / 环境变量注入，勿提交真实 token
    tokens:
      sk-admin: ["*"]
      sk-loki-reader: ["loki-xuan", "loki"]
      sk-oracle-reader: ["oracle"]
```

### 5.4 配置属性

```java
package com.gdalpha.mcpauth.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@ConfigurationProperties(prefix = "mcp.auth")
public class McpAuthProperties {

    private List<String> publicPaths = new ArrayList<>();
    private Map<String, List<String>> tokens = new HashMap<>();

    public List<String> getPublicPaths() {
        return publicPaths;
    }

    public void setPublicPaths(List<String> publicPaths) {
        this.publicPaths = publicPaths;
    }

    public Map<String, List<String>> getTokens() {
        return tokens;
    }

    public void setTokens(Map<String, List<String>> tokens) {
        this.tokens = tokens;
    }
}
```

### 5.5 启动类

```java
package com.gdalpha.mcpauth;

import com.gdalpha.mcpauth.config.McpAuthProperties;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

@SpringBootApplication
@EnableConfigurationProperties(McpAuthProperties.class)
public class McpIngressAuthApplication {

    public static void main(String[] args) {
        SpringApplication.run(McpIngressAuthApplication.class, args);
    }
}
```

### 5.6 解析 MCP 服务名

```java
package com.gdalpha.mcpauth.service;

import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.net.URI;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Component
public class McpServerNameResolver {

    private static final Pattern SERVER_PATH =
            Pattern.compile("^/servers/([^/]+)(?:/.*)?$");

    /**
     * 从原始 path / URI / URL 解析 named server。
     * 例：/servers/loki-xuan/mcp → loki-xuan
     */
    public String resolve(String originalPath, String originalUri, String originalUrl) {
        String path = firstNonBlank(originalPath, pathFromUri(originalUri), pathFromUrl(originalUrl));
        if (!StringUtils.hasText(path)) {
            return null;
        }
        int q = path.indexOf('?');
        if (q >= 0) {
            path = path.substring(0, q);
        }
        if (!path.startsWith("/")) {
            path = "/" + path;
        }
        Matcher m = SERVER_PATH.matcher(path);
        return m.matches() ? m.group(1) : null;
    }

    public boolean isPublicPath(String path, List<String> publicPaths) {
        if (!StringUtils.hasText(path)) {
            return false;
        }
        String pure = path.contains("?") ? path.substring(0, path.indexOf('?')) : path;
        try {
            if (pure.startsWith("http://") || pure.startsWith("https://")) {
                pure = URI.create(pure).getPath();
            }
        } catch (Exception ignored) {
            // keep pure
        }
        if ("/status".equals(pure)) {
            return true;
        }
        for (String p : publicPaths) {
            if (!StringUtils.hasText(p)) {
                continue;
            }
            if (pure.equals(p) || pure.startsWith(p.endsWith("/") ? p : p + "/")) {
                return true;
            }
        }
        return false;
    }

    private static String pathFromUri(String uri) {
        if (!StringUtils.hasText(uri)) {
            return null;
        }
        return uri.startsWith("/") ? uri : "/" + uri;
    }

    private static String pathFromUrl(String url) {
        if (!StringUtils.hasText(url)) {
            return null;
        }
        try {
            return URI.create(url).getPath();
        } catch (Exception e) {
            int scheme = url.indexOf("://");
            if (scheme > 0) {
                int pathStart = url.indexOf('/', scheme + 3);
                return pathStart >= 0 ? url.substring(pathStart) : "/";
            }
            return url.startsWith("/") ? url : null;
        }
    }

    private static String firstNonBlank(String... values) {
        for (String v : values) {
            if (StringUtils.hasText(v)) {
                return v.trim();
            }
        }
        return null;
    }
}
```

### 5.7 认证与授权

```java
package com.gdalpha.mcpauth.service;

import com.gdalpha.mcpauth.config.McpAuthProperties;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.Optional;

@Service
public class AuthService {

    private final McpAuthProperties properties;

    public AuthService(McpAuthProperties properties) {
        this.properties = properties;
    }

    public Optional<AuthPrincipal> authenticate(String authorizationHeader, String apiKeyHeader) {
        String token = extractToken(authorizationHeader, apiKeyHeader);
        if (!StringUtils.hasText(token)) {
            return Optional.empty();
        }
        if (!properties.getTokens().containsKey(token)) {
            return Optional.empty();
        }
        String userId = "user-" + Integer.toHexString(token.hashCode());
        return Optional.of(new AuthPrincipal(userId, token, properties.getTokens().get(token)));
    }

    public boolean authorize(AuthPrincipal principal, String mcpServerName) {
        List<String> allowed = principal.allowedServers();
        if (allowed == null || allowed.isEmpty()) {
            return false;
        }
        if (allowed.stream().anyMatch("*"::equals)) {
            return true;
        }
        if (!StringUtils.hasText(mcpServerName)) {
            return false;
        }
        return allowed.stream().anyMatch(s -> s.equalsIgnoreCase(mcpServerName));
    }

    private String extractToken(String authorization, String apiKey) {
        if (StringUtils.hasText(authorization)) {
            String v = authorization.trim();
            if (v.regionMatches(true, 0, "Bearer ", 0, 7)) {
                return v.substring(7).trim();
            }
            return v;
        }
        if (StringUtils.hasText(apiKey)) {
            return apiKey.trim();
        }
        return null;
    }

    public record AuthPrincipal(String userId, String token, List<String> allowedServers) {}
}
```

ACL 示例：

```json
{
  "sk-admin": ["*"],
  "sk-loki-reader": ["loki-xuan", "loki"],
  "sk-oracle-reader": ["oracle"]
}
```

### 5.8 控制器

```java
package com.gdalpha.mcpauth.web;

import com.gdalpha.mcpauth.config.McpAuthProperties;
import com.gdalpha.mcpauth.service.AuthService;
import com.gdalpha.mcpauth.service.AuthService.AuthPrincipal;
import com.gdalpha.mcpauth.service.McpServerNameResolver;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseEntity;
import org.springframework.util.StringUtils;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.bind.annotation.RestController;

import java.util.Optional;

@RestController
public class IngressAuthController {

    private static final Logger log = LoggerFactory.getLogger(IngressAuthController.class);

    private final AuthService authService;
    private final McpServerNameResolver serverNameResolver;
    private final McpAuthProperties properties;

    public IngressAuthController(
            AuthService authService,
            McpServerNameResolver serverNameResolver,
            McpAuthProperties properties) {
        this.authService = authService;
        this.serverNameResolver = serverNameResolver;
        this.properties = properties;
    }

    /**
     * Nginx Ingress auth-url 回调。
     * 成功必须 200；失败 401/403。不要 302。
     */
    @RequestMapping(value = "/auth/verify", method = {RequestMethod.GET, RequestMethod.HEAD})
    public ResponseEntity<Void> verify(HttpServletRequest request) {
        String originalPath = header(request, "X-Original-Path");
        String originalUri = header(request, "X-Original-URI", "X-Original-Uri");
        String originalUrl = header(request, "X-Original-URL", "X-Original-Url");
        String method = header(request, "X-Original-Method", "X-Original-METHOD");

        String path = StringUtils.hasText(originalPath)
                ? originalPath
                : (StringUtils.hasText(originalUri) ? originalUri : originalUrl);

        if (serverNameResolver.isPublicPath(path, properties.getPublicPaths())) {
            return ResponseEntity.ok().build();
        }

        String mcpServer = serverNameResolver.resolve(originalPath, originalUri, originalUrl);
        String authorization = header(request, HttpHeaders.AUTHORIZATION);
        String apiKey = header(request, "X-Api-Key", "X-API-KEY");

        Optional<AuthPrincipal> principalOpt = authService.authenticate(authorization, apiKey);
        if (principalOpt.isEmpty()) {
            log.info("auth denied: unauthorized method={} path={} server={}",
                    method, path, mcpServer);
            return ResponseEntity.status(401).build();
        }

        AuthPrincipal principal = principalOpt.get();
        if (!authService.authorize(principal, mcpServer)) {
            log.info("auth denied: forbidden user={} method={} path={} server={}",
                    principal.userId(), method, path, mcpServer);
            return ResponseEntity.status(403).build();
        }

        log.debug("auth ok user={} server={} path={}", principal.userId(), mcpServer, path);

        return ResponseEntity.ok()
                .header("X-User-Id", principal.userId())
                .header("X-MCP-Server", mcpServer == null ? "" : mcpServer)
                .build();
    }

    private static String header(HttpServletRequest request, String... names) {
        for (String name : names) {
            String v = request.getHeader(name);
            if (StringUtils.hasText(v)) {
                return v;
            }
        }
        return null;
    }
}
```

### 5.9 本地验证

```bash
# 有权访问 loki-xuan → 200
curl -i "http://127.0.0.1:8081/auth/verify" \
  -H "X-Original-Path: /servers/loki-xuan/mcp" \
  -H "X-Original-Method: POST" \
  -H "Authorization: Bearer sk-loki-reader"

# 无权访问 oracle → 403
curl -i "http://127.0.0.1:8081/auth/verify" \
  -H "X-Original-Path: /servers/oracle/mcp" \
  -H "Authorization: Bearer sk-loki-reader"

# 无 token → 401
curl -i "http://127.0.0.1:8081/auth/verify" \
  -H "X-Original-Path: /servers/loki-xuan/mcp"

# 公开路径 → 200
curl -i "http://127.0.0.1:8081/auth/verify" \
  -H "X-Original-Path: /status"
```

---

## 6. K8s 部署清单（鉴权服务）

### 6.1 Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: mcp-ingress-auth
  namespace: kube-ops
  labels:
    app: mcp-ingress-auth
spec:
  selector:
    app: mcp-ingress-auth
  ports:
    - name: http
      port: 8081
      targetPort: 8081
```

### 6.2 Secret（示例）

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mcp-ingress-auth-secrets
  namespace: kube-ops
type: Opaque
stringData:
  # 实际部署请替换；也可用文件挂载整份 ACL
  MCP_AUTH_TOKENS_JSON: |
    {
      "sk-admin": ["*"],
      "sk-loki-reader": ["loki-xuan", "loki"],
      "sk-oracle-reader": ["oracle"]
    }
```

> 上面 `application.yml` 若改为从环境变量/JSON 加载，可与此 Secret 对齐。第一期也可用 Spring 配置树 + 环境变量覆盖。

### 6.3 Deployment（骨架）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-ingress-auth
  namespace: kube-ops
  labels:
    app: mcp-ingress-auth
spec:
  replicas: 2
  selector:
    matchLabels:
      app: mcp-ingress-auth
  template:
    metadata:
      labels:
        app: mcp-ingress-auth
    spec:
      containers:
        - name: mcp-ingress-auth
          image: harbor.gdalpha.com/alpha-ai-mcp/mcp-ingress-auth:1.0.0
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8081
          envFrom:
            - secretRef:
                name: mcp-ingress-auth-secrets
                optional: true
          readinessProbe:
            httpGet:
              path: /actuator/health/readiness
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /actuator/health/liveness
              port: http
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests:
              cpu: 50m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
```

---

## 7. 与 mcp-proxy 现有能力的关系

### 7.1 出站鉴权（已有，保持不变）

`mcp-proxy` 作为客户端连接远端 MCP 时：

```bash
# Header
mcp-proxy --headers Authorization 'Bearer YOUR_TOKEN' https://remote/sse

# 环境变量
API_ACCESS_TOKEN=xxx mcp-proxy https://remote/sse

# OAuth2 Client Credentials
mcp-proxy --client-id ... --client-secret ... --token-url ... https://remote/sse
```

这与「入站 Ingress Auth」互补：一个保护「谁能调用我们」；一个保护「我们如何调用上游」。

### 7.2 入站应用内鉴权（可选第二道）

若希望不经 Ingress 的集群内调用也受控，可在 `mcp_server.py` 增加 Bearer 中间件：

- CLI：`--auth-token`（可重复）
- 环境变量：`MCP_PROXY_AUTH_TOKENS`
- 放行 `/status`
- 保护 `/servers/*/mcp`（及 SSE 相关路径）

主路径仍建议 Ingress Auth 做「按 server 授权」；应用内可做全局 Token 兜底。

### 7.3 Tool 级授权（可选第三期）

Ingress 看不到 JSON-RPC body 中的 tool 名。若需要：

- 允许 `list_*` / 只读 tool
- 禁止 `run_shell` 等

应在 `mcp-proxy` 的 `proxy_server.py`（`call_tool` / `list_tools`）或后端 MCP 内实现，并可结合 Ingress 回传的 `X-User-Id`。

---

## 8. 落地步骤建议

### 第一期（推荐先做）

1. 实现 Spring Boot `/auth/verify`（Token + server ACL）。
2. 部署 `mcp-ingress-auth` Service/Deployment。
3. Ingress 增加 `auth-url` / `auth-snippet` / `auth-response-headers`。
4. 客户端为每个 MCP 配置 `Authorization: Bearer ...`。
5. 用 curl 验证 200/401/403，再用真实 MCP 客户端打 `/servers/<name>/mcp`。

### 第二期

1. Token/ACL 改为 Secret 或配置中心，支持热更新。
2. 引入 JWT / 公司 IdP（`sub` + `scope` 如 `mcp:server:loki-xuan`）。
3. 审计日志：`userId`、`server`、`method`、时间、结果。

### 第三期

1. mcp-proxy 消费 `X-User-Id` 做访问审计。
2. Tool 级 ACL / `list_tools` 过滤。
3. 按命名空间或多租户拆分 token 与配额。

---

## 9. 常见问题

### Q1：鉴权服务拿不到服务名？

检查：

1. Ingress 是否配置了 `auth-snippet`，并传递 `X-Original-Path` / `X-Original-URI`。
2. 是否被 `rewrite-target` 改写了 path。
3. 控制器是否兼容多种 header 大小写/命名。

### Q2：为什么是 GET 子请求？

Ingress `auth-url` 默认用独立子请求做门禁，**不转发原始 body**。对「按 path + header 鉴权」足够；不要在 auth 服务里等 MCP JSON body。

### Q3：SSE 还要不要管？

当前主用 `/mcp`。若仍暴露 `/sse` 与 `/messages/`：

- 鉴权规则同样基于 `/servers/<name>/...`，可一并保护。
- 多副本时 SSE 仍需会话粘性；Streamable HTTP 建议 `--stateless`。

### Q4：401 和 403 如何选？

| 场景 | 状态码 |
|------|--------|
| 没有 Token / Token 不存在或无效 | 401 |
| Token 有效，但不在该 server 的 ACL 中 | 403 |

### Q5：能否只靠 Ingress 基本认证？

`auth-type: basic` 只能做简单用户名密码，**不好按 MCP 服务名做 ACL**。需要 server 级授权时，用自定义 `auth-url`（本文方案）。

### Q6：集群内直连 Service 会不会绕过鉴权？

会。因此：

- Service 用 ClusterIP。
- 网络策略限制仅 Ingress Controller 访问 mcp-proxy（可选）。
- 或在 mcp-proxy 内再加一层 Token 校验。

---

## 10. 配置速查

### 客户端

```json
{
  "type": "http",
  "url": "https://ai-mcp-sic.gdalpha.com/servers/loki-xuan/mcp",
  "headers": {
    "Authorization": "Bearer sk-xxx"
  }
}
```

### Ingress

```yaml
nginx.ingress.kubernetes.io/auth-url: "http://mcp-ingress-auth.kube-ops.svc.cluster.local:8081/auth/verify"
nginx.ingress.kubernetes.io/auth-method: GET
nginx.ingress.kubernetes.io/auth-response-headers: X-User-Id,X-MCP-Server
```

### 鉴权服务

```text
GET /auth/verify
Header: X-Original-Path=/servers/loki-xuan/mcp
Header: Authorization=Bearer sk-xxx
→ 200 / 401 / 403
```

### 服务名解析

```text
/servers/<name>/...  →  name
```

---

## 11. 相关文件

| 文件 | 说明 |
|------|------|
| [README.md](./README.md) | K8s 部署总说明 |
| [ingress.yaml](./ingress.yaml) | Ingress 模板（可按本文增加 auth 注解） |
| [deployment.yaml](./deployment.yaml) | mcp-proxy 部署 |
| [configmap.yaml](./configmap.yaml) | `servers.json` named server 定义 |
| [secret.yaml.example](./secret.yaml.example) | 后端 MCP 密钥示例（出站/子进程环境变量） |

---

## 12. 总结

1. 统一入口是 **Streamable HTTP**：`/servers/<name>/mcp`。
2. 入站鉴权推荐 **Ingress Nginx `auth-url` + Spring Boot `/auth/verify`**。
3. **MCP 服务名从 `X-Original-Path` 解析**，按 `token × server` 授权。
4. 客户端只需配置 URL + `Authorization` header。
5. 出站鉴权沿用 mcp-proxy 现有能力；tool 级控制放到后续在 proxy/后端实现。
