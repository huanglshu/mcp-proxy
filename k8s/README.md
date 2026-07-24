# Kubernetes 部署指南

本文说明如何在 Kubernetes 中部署 `mcp-proxy`，并暴露一个或多个 named MCP server。

## 目录结构

```text
k8s/
├── README.md           # 本文档
├── namespace.yaml       # Namespace（可选）
├── configmap.yaml      # named server 配置（servers.json）
├── secret.yaml.example # 敏感环境变量示例（勿直接提交真实密钥）
├── deployment.yaml     # Deployment
├── service.yaml        # ClusterIP Service
└── ingress.yaml        # Ingress（可选）
```

## 前提条件

- 已可访问目标集群（`kubectl` 已配置）
- 镜像已构建并推送到仓库（见仓库根目录 `build_image.sh`）
  - 默认镜像：`harbor.gdalpha.com/alpha-ai-mcp/mcp-proxy:<VERSION>`
  - 版本号来自仓库根目录 `VERSION`
- 集群可拉取该镜像（私有仓库需提前配置 `imagePullSecrets`）
- 若 named server 使用 `uvx` / `npx` 等命令，基础镜像中需包含对应运行时  
  当前 Dockerfile 已包含 Node.js（`node` / `npm` / 可用 `npx`）；`uvx` 默认不在镜像内，如需使用请扩展镜像

## 架构说明

```text
Client (SSE / Streamable HTTP)
        │
        ▼
   Ingress / Service :8080
        │
        ▼
   mcp-proxy Pod
   ├── /status
   ├── /servers/<name>/sse
   ├── /servers/<name>/messages/
   └── /servers/<name>/mcp
        │
        ▼
   本地 stdio MCP 子进程（由 servers.json 定义）
```

仅使用 `--named-server-config` 时，**根路径没有默认服务**，必须通过 `/servers/<name>/...` 访问。

## 快速部署

### 1. 修改配置

1. 编辑 [`configmap.yaml`](./configmap.yaml)，按业务填写 `mcpServers`
2. 编辑 [`deployment.yaml`](./deployment.yaml)：
   - `image` 改为实际镜像 tag
   - 如有私有仓库，补充 `imagePullSecrets`
3. 如有 token 等密钥，复制 `secret.yaml.example` 为 `secret.yaml` 并填写，再在 Deployment 中引用

### 2. 应用清单

```bash
# 可选：创建命名空间
kubectl apply -f namespace.yaml

# 配置与工作负载
kubectl apply -f configmap.yaml
# kubectl apply -f secret.yaml   # 如有
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml

# 可选：Ingress
kubectl apply -f ingress.yaml
```

一键应用（按需排除 secret / ingress）：

```bash
kubectl apply -f namespace.yaml -f configmap.yaml -f deployment.yaml -f service.yaml
```

### 3. 验证

```bash
# Pod 状态
kubectl -n mcp-proxy get pods -l app=mcp-proxy

# 日志（应打印 Serving MCP Servers via SSE）
kubectl -n mcp-proxy logs -l app=mcp-proxy -f

# 集群内探测
kubectl -n mcp-proxy port-forward svc/mcp-proxy 8080:8080
curl -sS http://127.0.0.1:8080/status
```

## 访问方式

假设 Service 端口为 `8080`，named server 名为 `fetch`：

| 用途 | 路径 |
|------|------|
| 全局状态 | `GET /status` |
| SSE | `/servers/fetch/sse` |
| SSE 消息通道 | `/servers/fetch/messages/` |
| Streamable HTTP | `/servers/fetch/mcp` |

### 集群内访问

```text
http://mcp-proxy.mcp-proxy.svc.cluster.local:8080/servers/fetch/sse
```

### 本地 port-forward

```bash
kubectl -n mcp-proxy port-forward svc/mcp-proxy 8080:8080
```

客户端示例：

```json
{
  "mcpServers": {
    "fetch": {
      "url": "http://127.0.0.1:8080/servers/fetch/sse"
    }
  }
}
```

Streamable HTTP：

```json
{
  "mcpServers": {
    "fetch": {
      "url": "http://127.0.0.1:8080/servers/fetch/mcp",
      "transport": "streamable-http"
    }
  }
}
```

### 通过 Ingress 访问

修改 [`ingress.yaml`](./ingress.yaml) 中的 `host` 后应用。注意：

- SSE 需要较长连接，Ingress 超时建议调大（示例中已给出 Nginx 注解）
- 若经 TLS 终止，客户端使用 `https://...`

## servers.json 配置

ConfigMap 挂载为容器内 `/config/servers.json`，启动参数：

```text
--host 0.0.0.0 --port 8080 --named-server-config /config/servers.json
```

配置格式与仓库根目录 `config_example.json` 一致：

```json
{
  "mcpServers": {
    "fetch": {
      "enabled": true,
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-fetch"],
      "env": {
        "SOME_KEY": "optional"
      },
      "transportType": "stdio"
    }
  }
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| key（如 `fetch`） | 服务名，对应 URL `/servers/fetch/` |
| `command` | 必填，stdio 启动命令 |
| `args` | 可选，参数列表 |
| `env` | 可选，该服务额外环境变量 |
| `enabled` | 可选，`false` 时跳过；默认视为启用 |
| `timeout` / `transportType` | 兼容字段，当前 proxy 加载 named server 时忽略，传输固定为 stdio |

更新配置后：

```bash
kubectl -n mcp-proxy apply -f configmap.yaml
kubectl -n mcp-proxy rollout restart deploy/mcp-proxy
```

## 镜像构建与推送

在仓库根目录：

```bash
./build_image.sh
```

脚本会读取 `VERSION`，构建并推送：

```text
harbor.gdalpha.com/alpha-ai-mcp/mcp-proxy:<VERSION>
```

部署前请确认 `deployment.yaml` 中的 tag 与推送版本一致。

## 启动参数说明

Deployment 默认 args：

```yaml
args:
  - --host
  - "0.0.0.0"
  - --port
  - "8080"
  - --named-server-config
  - /config/servers.json
  - --pass-environment
  - --allow-origin
  - "*"
```

| 参数 | 作用 |
|------|------|
| `--host 0.0.0.0` | 监听所有网卡，否则 Service 无法连通 |
| `--port 8080` | 与 containerPort / Service port 一致 |
| `--named-server-config` | 从文件加载多个 named server |
| `--pass-environment` | 将 Pod 环境变量传给子进程（Secret/Config 注入时常用） |
| `--allow-origin '*'` | 开启 CORS；生产环境建议收紧 |

也可改为单服务模式（不使用 ConfigMap）：

```yaml
args:
  - --host
  - "0.0.0.0"
  - --port
  - "8080"
  - --
  - npx
  - -y
  - "@modelcontextprotocol/server-fetch"
```

此时访问根路径：`/sse`、`/mcp`。

## 多副本与负载均衡

可以。默认示例已将 `replicas` 设为 `2`，Service 开启 `sessionAffinity: ClientIP`。

| 传输 | 多副本表现 | 建议 |
|------|------------|------|
| SSE（`/sse` + `/messages/`） | 会话绑定在**单个 Pod** 内；跨 Pod 会断连或 404 | 必须会话亲和（Service `ClientIP` 或 Ingress cookie 粘性） |
| Streamable HTTP（`/mcp`） | 默认仍可能依赖会话；`--stateless` 后更易水平扩展 | 生产多副本优先 `--stateless` |
| `/status` | 无状态，任意 Pod 即可 | 可用作探针与健康检查 |

### 扩缩容

```bash
# 手动扩容
kubectl -n mcp-proxy scale deploy/mcp-proxy --replicas=3

# 或改 deployment.yaml 中的 replicas 后 apply
```

Service 侧会话亲和（已写入 `service.yaml`）：

```yaml
sessionAffinity: ClientIP
sessionAffinityConfig:
  clientIP:
    timeoutSeconds: 10800  # 3h，覆盖长连接 SSE
```

说明：

- `ClientIP` 是四层粘性：同一源 IP 会打到同一 Pod，**不同客户端可负载到不同 Pod**
- 经过多层 NAT / 公司代理时，大量客户端可能共享同一出口 IP，粘性会退化成“热点 Pod”
- Ingress 入口时，除 Service 亲和外，建议在 Ingress 上再开 cookie 粘性，例如 Nginx：

```yaml
annotations:
  nginx.ingress.kubernetes.io/affinity: "cookie"
  nginx.ingress.kubernetes.io/session-cookie-name: "mcp-proxy-affinity"
  nginx.ingress.kubernetes.io/session-cookie-max-age: "10800"
  nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
  nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
  nginx.ingress.kubernetes.io/proxy-buffering: "off"
```

### 更适合水平扩展的模式

在 Deployment args 中增加：

```yaml
- --stateless
```

客户端改走 Streamable HTTP：

```text
http://<host>:8080/servers/<name>/mcp
```

注意：每个副本会各自拉起一套 stdio 子进程（配置里的每个 named server 在**每个 Pod** 里都会启动）。副本数 × 子进程数 会叠加 CPU/内存，资源 `requests/limits` 需按副本数评估。

### 可选 HPA 示例

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: mcp-proxy
  namespace: mcp-proxy
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: mcp-proxy
  minReplicas: 2
  maxReplicas: 5
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

滚动更新已配置 `maxUnavailable: 0`，尽量保证升级过程中始终有可用副本。

## 资源与探针建议

示例 Deployment 中：

- 请求/限制可按实际 MCP 子进程数量与副本数调整
- 就绪探针使用 `GET /status`（轻量全局状态）
- 若子进程启动较慢，可增大 `initialDelaySeconds`

SSE 长连接场景：

- 不建议对 `/servers/*/sse` 使用过短的 idle timeout
- 多副本必须配合 Service / Ingress 会话亲和；否则同一客户端的 SSE 与 POST messages 可能落到不同 Pod

## 私有镜像拉取

```yaml
spec:
  template:
    spec:
      imagePullSecrets:
        - name: harbor-pull-secret
```

创建示例：

```bash
kubectl -n mcp-proxy create secret docker-registry harbor-pull-secret \
  --docker-server=harbor.gdalpha.com \
  --docker-username='<user>' \
  --docker-password='<password>'
```

## 常见问题

### 1. 访问 `/sse` 404

使用了 `--named-server-config` 且没有默认 `command_or_url` 时，只有 `/servers/<name>/sse`。请用配置中的服务名访问。

### 2. Pod Running 但日志报 command not found

镜像内缺少 named server 所需命令。当前镜像含 Python + Node.js；若配置了 `uvx`，需自行扩展镜像安装 `uv`，或改用镜像内已有命令（如 `npx`）。

### 3. 环境变量未传到 MCP 子进程

确认启动参数包含 `--pass-environment`，或在 `servers.json` 的对应服务 `env` 中显式写入。

### 4. Ingress 下 SSE 断开

调大 proxy read/send 超时，关闭或放宽缓冲相关设置（见 `ingress.yaml` 注解）。

### 5. 配置变更不生效

ConfigMap 更新后需重启 Pod（`rollout restart`），或改用 checksum 注解触发滚动更新。

## 卸载

```bash
kubectl delete -f ingress.yaml --ignore-not-found
kubectl delete -f service.yaml -f deployment.yaml -f configmap.yaml
kubectl delete -f secret.yaml --ignore-not-found
kubectl delete -f namespace.yaml --ignore-not-found
```
