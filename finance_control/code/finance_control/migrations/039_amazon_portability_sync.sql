CREATE TABLE amazon_portability_queries (
 profile_alias TEXT NOT NULL CHECK(length(profile_alias) BETWEEN 1 AND 64),
 scope_id TEXT NOT NULL CHECK(scope_id IN ('portability-physical-orders', 'portability-physical-order-returns')),
 query_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL CHECK(status IN ('PENDING', 'COMPLETED', 'CANCELED', 'IMPORTED')),
 PRIMARY KEY(profile_alias, scope_id)
);
