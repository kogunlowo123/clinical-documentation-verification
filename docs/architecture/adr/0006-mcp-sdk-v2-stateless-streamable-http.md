# ADR-0006: MCP Python SDK 2.x, stateless streamable HTTP, tool-level authorization

- Status: Accepted
- Date: 2026-10-14

## Context

The portfolio standard names the official MCP Python SDK's FastMCP with
streamable HTTP. The current SDK major version (2.x) renamed `FastMCP` to
`MCPServer` with the same decorator model. The service runs several replicas
behind a load balancer.

## Decision

- Use `mcp>=1.20` resolved to the 2.x line and `mcp.server.mcpserver.MCPServer`
  (the successor of FastMCP), mounted at `/mcp` inside the FastAPI app, its
  session manager started in the app lifespan.
- Run stateless with JSON responses: no MCP session affinity is needed, any
  replica serves any call, and tool calls work without a prior `initialize`.
- Authenticate per tool call from the HTTP `Authorization` header and reuse the
  application service, so MCP has exactly the same policy, validation and
  audit as REST. Tool listing is unauthenticated (it reveals schemas only).
- Host-header DNS-rebinding protection is left to the ingress; the service is
  never exposed on a browser-reachable localhost.

## Consequences

- No sticky sessions; horizontal scaling stays trivial.
- Server-initiated requests (sampling, elicitation) are not used; the tools are
  request/response.
- An SDK major upgrade is a dependency change with e2e tests over the wire
  format (`tests/e2e/test_protocols.py`).
