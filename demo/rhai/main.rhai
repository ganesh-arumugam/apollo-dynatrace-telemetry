// Sets a context key so `router.direct.yaml` can demonstrate the
// `request_context` telemetry selector (see templates/spans.router.yaml).
//
// Plain index assignment, NOT context.upsert() — upsert() takes a resolver
// callback, not a plain value, and is the most common way this silently
// never sets anything.
//
// Set in router_service, not supergraph_service: a `router`-scoped span or
// event attribute reads request_context at request time, before the router
// hands off to supergraph_service. Live-verified — setting this same key in
// supergraph_service left the router span's attribute at its `default`
// (the key wasn't there yet when the router span read it), even though the
// key was demonstrably in context one hop later (visible from a subgraph
// event under the same trace).
fn router_service(service) {
    let request_callback = |request| {
        request.context["tenant_id"] = "acme-corp";
    };
    service.map_request(request_callback);
}
