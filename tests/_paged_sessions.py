"""A fake `sessions.list` that pages like the Gateway (2026.10.10): 100 rows by default,
`totalCount`, `limit`/`offset` paging, `search` a key substring. Not a suite."""

DEFAULT_LIMIT = 100


class PagedSessions:
    def __init__(self, rows):
        self.rows = list(rows)      # newest first, as the Gateway sorts
        self.calls = []

    def list(self, params):
        self.calls.append(dict(params))
        rows = self.rows
        search = params.get("search")
        if search:
            rows = [r for r in rows if search in r.get("key", "")]
        offset = params.get("offset") or 0
        limit = params.get("limit") or DEFAULT_LIMIT
        return {"sessions": [dict(r) for r in rows[offset:offset + limit]], "totalCount": len(rows)}

    def full_reads(self):
        """The calls that name no key: neither `search` nor `spawnedBy`."""
        return [c for c in self.calls if not ({"search", "spawnedBy"} & set(c))]


def crowd(key, *, busy, before=120, after=10):
    """`before` sessions newer than `key` and `after` older ones, so `key` sits
    past the default page, like a long run."""
    idle = lambda n, tag: [{"key": f"agent:main:openwebui-user-{tag}{i:04d}", "hasActiveRun": False,
                            "status": "done"} for i in range(n)]
    return idle(before, "new") + [{"key": key, "hasActiveRun": busy, "status": "running" if busy else "done"}] + \
        idle(after, "old")
