# Gateway integration

local0 runs behind an LLM gateway. The reference integration targets Gravitee
APIM. The gateway reaches the router over container DNS at
http://router-service:8081, so the router and Qdrant join the gateway's docker
network.

The escalation contract is: the router returns HTTP 424, and a gateway response
policy reroutes on that 424 to the cloud provider. This policy is mandatory —
built-in gateway failover only retries on connection or transport failure and
ignores the HTTP status code, so a status-only signal needs an explicit policy.

When a cloud answer comes back, the gateway can call the router's learn endpoint
so the answer is cached locally and served directly next time. The learn callback
is guarded by an optional shared token and a tag filter to prevent cache
poisoning.

Deploying the integration registers two APIs on the gateway — the local router
and the big cloud model — plus the 424 reroute policy that ties them together.
